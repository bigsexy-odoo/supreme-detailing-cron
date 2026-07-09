"""
Sync custom-cart bookings -> Odoo Appointments.

A completed cart purchase carries a per-line booking string (SDBK1|...) captured
as a product.attribute.custom.value on the sale.order.line (the ONLY public
channel that persists -- proven by the gift flow). This external cron reads those
strings, creates the matching calendar.event on the right detailer's calendar
(mirroring the one known-good event id 2), fires the native "Appointment Booked"
email (mail.template 37) + native reminders, and marks the line idempotently so
it is never reprocessed.

ZERO billable Odoo LoC: this runs OUTSIDE Odoo over XML-RPC (Rule 9). No server
actions, no automation rules, no Studio.

SDBK1 capture format (one custom-attribute-value string per booked line):
    SDBK1|<date>|<time24>|<durationHours>|<apptTypeId>|<resourceName>|<suburb>|<serviceLabel>
    e.g. SDBK1|2026-07-11|09:00|1.5|1|Kade (Central Auckland)|Royal Oak|Exterior Package (Car)

------------------------------------------------------------------------------
SAFETY / REVIEW FIXES folded in (do NOT silently regress these):
  * ELIGIBILITY: the shop takes bank-transfer / cash-on-the-day, so a genuine
    booking often stays 'sent' (AWAITING PAYMENT) -- those DO go on the calendar
    (owner's Phase-2 decision), shown as awaiting vs paid. But 'sent' is eligible
    ONLY with a payment intent that is not failed/cancelled (a pending transfer /
    authorized / done tx); a bare quote or a failed-card abandonment is excluded,
    so the unattended cron never emails an unpaid non-booking. sale/done = paid.
  * SINGLE-INSTANCE LOCK: a --commit run takes an O_EXCL lock so a manual backfill
    and the scheduled cron can't overlap and double-create / double-email.
  * DETAILER-COLOUR EMAIL GUARD: the detailer contacts (RESOURCE_PARTNER) MUST
    carry an email or Odoo reaps them from the attendee list ~80s later and the
    colour vanishes. A --commit run HARD-STOPS if either is missing.
  * EMAIL VIA QUEUE: template 37 is queued (force_send=False) so a daily-cap trip
    leaves it 'outgoing' for Odoo to retry, never lost. --max defaults to 25 so a
    naive --commit self-throttles under the cap (re-run to continue; --max 0=all).
  * BOUNDED IDEMPOTENCY MARKER: the per-line marker is searched as the FULL
    delimited HTML comment  <!-- SDBK1:L<id> -->  so line 12 can never match
    line 123 (an unbounded ilike 'SDBK1:L12' would). Plus an order-ref audit
    token (SDCAL:L<line>=E<event>) and a natural-key fallback.
  * PAST-DATE GUARD: a booking whose start is already in the past is skipped by
    default (alarms can't fire; emailing it is pure spam). --include-past creates
    the event with NO email and NO SMS, for calendar completeness only.
  * EMAIL-PRESENCE GUARD: template 37 is only sent if the booker partner actually
    has an email; otherwise logged as no-email-address (Rule 7, no silent skip).
  * SMS OFF BY DEFAULT: alarm 8 = a real IAP SMS (paid credits). Bulk/backfill
    sync uses BASE_ALARMS=[3,6] (notification + email). --with-sms re-adds alarm 8
    only when the booker has a mobile number.
  * SLOT-COLLISION GUARD: before create, search for any existing event that
    overlaps [start,stop] on the SAME appointment_resource -> skip + flag for
    manual (a client-side localStorage slot has no server hold). --allow-overlap
    to force.
  * QTY GUARD: a merged line with product_uom_qty>1 is flagged loudly (one event
    is created; N vehicles at one slot+resource can't be N real appointments).
  * APPT-TYPE VALIDATION: an SDBK1 apptTypeId that is not a live appointment.type
    is skipped + logged (covers combo/add-on types the capture side may not cover).
  * --max N bounds a run (throttle the first backfill under the SaaS email cap).

Discovery (two channels, de-duped by line id):
  1. PRODUCTION: product.attribute.custom.value.custom_value LIKE 'SDBK1%'
     (its sale_order_line_id back-links to the paid line).
  2. TEST FALLBACK: any 'SDBK1|...' token in the sale.order.line `name` text --
     lets the owner test on S00088 by editing the line description in the backend
     WITHOUT configuring a custom product attribute first.

Usage:
    python sync_bookings.py                        # DRY-RUN (default) -- reads + logs, writes nothing
    python sync_bookings.py --commit               # create events + send template 37 (future, emailed)
    python sync_bookings.py --commit --no-email    # create events but DON'T send template 37 (safe test)
    python sync_bookings.py --only-order S00088     # restrict to one order (name or id)
    python sync_bookings.py --since 2026-07-01      # only orders with date_order >= this
    python sync_bookings.py --commit --with-sms     # also schedule the SMS reminder (alarm 8)
    python sync_bookings.py --commit --include-past # backfill past bookings (event only, no email/sms)
    python sync_bookings.py --commit --max 5        # cap this run to 5 creations (default 25; 0=all)
Creds: odoo_client.cfg() reads env vars first (GitHub secrets) then .env.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Force UTF-8 stdout on Windows (avoid charmap codec errors with emoji) -- Rule 5
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient, cfg

# ---------------------------------------------------------------------------
# Constants -- mirrored from the one known-good event (calendar.event id 2)
# ---------------------------------------------------------------------------
ORGANIZER_PARTNER = 3       # partner_id on event 2 (Supreme Detailing company)
ORGANIZER_USER = 2          # user_id on event 2. NOTE: appointment.resource carries NO
                            # user/employee link on this DB (both resolve to a
                            # resource.resource with user_id=False), so BOTH detailers'
                            # events land on user 2; detailer separation is expressed
                            # ONLY via appointment_resource_ids. Confirm with the owner
                            # that resource-only separation is acceptable.
BASE_ALARMS = [3, 6]        # Notification 1h + Email 3h. SMS (id 8) added only with --with-sms.
EMAIL_ALARM = 6             # "Email 3h" -- event-level, so it emails EVERY attendee (booker AND
                            # the detailer contact). Dropped under --no-email so a 'safe test'
                            # sends NO mail at all. NB: detailer 69/70 thus get a job-reminder
                            # email per booking -- intended (see DEPLOY doc cap math).
SMS_ALARM = 8               # "SMS Text Message - 1 Hours" -- real IAP credits, off by default.
                            # Event-level too: will also text the detailer if 69/70 carry a
                            # phone. Keep 69/70 phone BLANK unless you want to pay for that.
BOOKED_TEMPLATE = 37        # mail.template "Appointment: Attendee Invitation" (model calendar.attendee)
# Calendar colour tags (calendar.event.type): North Shore/Alex=green, Central/Kade=red.
# Only shows if the Meetings calendar (view 2430) colours by categ_ids (set up separately).
RESOURCE_TAG = {1: 1, 2: 2}   # appointment.resource id -> calendar.event.type (tag) id (popup label)
# COLOUR-BY-DETAILER. Alex/Kade are BOTH the appointment RESOURCE (lanes/availability via
# booking_line_ids) AND a PARTICIPANT contact added to each event. The main Meetings calendar
# (attendee_calendar js_class) colours by ATTENDEE, so once the detailer contact is a
# participant the booking auto-colours by detailer (green North Shore / red Central) and
# shows for anyone who ticks that detailer in the Calendar 'Attendees' sidebar.
#
# *** CRITICAL (root cause of the "colour vanishes ~1 min later" bug, verified 2026-07-08):
# Odoo's calendar attendee sync REAPS any attendee whose res.partner has NO email, ~60-80s
# after the event is created/modified. The emailless detailer contact gets silently stripped
# and the colour disappears. FIX: the detailer contacts MUST carry an email. ensure the
# owner sets a real one; ensure_detailer_attendee() warns loudly if it's missing. ***
RESOURCE_PARTNER = {1: 69, 2: 70}   # appointment.resource id -> detailer CONTACT (res.partner, MUST have email)
# Contacts, not Odoo user accounts -> no paid seats. Set the sidebar swatches green
# (Alex/North Shore) / red (Kade/Central) once. Paid status is shown by a ✅ tick in the
# TITLE (not by colour), so a glance gives detailer (colour) + paid (tick) + type + suburb.
NZ = ZoneInfo("Pacific/Auckland")   # DST-correct: NZST=UTC+12, NZDT=UTC+13
UTC = ZoneInfo("UTC")

SYNC_TAG = "[SD-booking-sync]"                    # visible provenance line in the description
MARKER_COMMENT = "<!-- SDBK1:L{line_id} -->"      # BOUNDED per-line idempotency key (full comment)
ORDER_TOKEN = "SDCAL:L{line_id}=E{event_id}"      # order-ref audit token
# Blank-booking watchdog (added 2026-07-09 after the S00075 lost-booking incident):
BOOKING_PTAVS = [54, 55, 56, 57, 58]  # hidden "Booking" is_custom PTAV per bookable template (tmpl 2/3/4/5/7)
NOBK_TOKEN = "SDNOBK:L{line_id}"      # order-ref token: this blank line has been alerted already

# ---- CRM step (bookings -> pipeline), added 2026-07-09 ----
# One opportunity per booked ORDER (a multi-line order = one visit = one deal):
# team by detailer, expected_revenue = order total, stage by payment state, linked to
# the order (sale.order.opportunity_id) AND its calendar events (event.opportunity_id,
# which lets route_leads_external.job_bookings tag/route + Chat-alert it).
# Replaces the OBSOLETE route_bookings_to_crm.py (read calendar.booking, which the
# custom cart never creates) and the appointment_crm lead_create auto-opps (thin $0
# "New" opps -- being turned OFF; existing ones are ADOPTED via their event link).
CRM_STAGE_NEW = 1              # crm.stage "New" (safe to upgrade from)
CRM_STAGE_BOOKED_UNPAID = 5    # crm.stage "Booked (Unpaid)"
CRM_STAGE_BOOKED = 6           # crm.stage "Booked" (paid)
CRM_TEAM_BY_RESOURCE = {1: 4, 2: 5}   # appointment.resource -> crm.team (Alex->North, Kade->Central)
CRM_TEAM_FALLBACK = 1                 # Sales (unknown/mixed detailer)
CRM_TAG_BOOKING = 6            # crm.tag "Appointment Booking"
CRM_REGION_TAG = {4: 2, 5: 3}  # crm.team -> crm.tag (North, Central)

# A "confirmed booking" = the customer completed checkout. For a bank-transfer / COD
# shop that is state 'sent' WITH a payment.transaction (a bare emailed quote / abandoned
# cart has none), plus 'sale'/'done' (owner-confirmed = paid). ALL confirmed bookings go
# on the calendar. The TILE shows paid via the title's 💰 (unpaid = no marker); the event
# DESCRIPTION/email still spells out PAID vs AWAITING PAYMENT so the customer knows to pay.
CONFIRMED_STATES = ["sent", "sale", "done"]
PAID_STATES = ["sale", "done"]
PST_MARKER = "<!-- PST:{state} -->"   # payment-state marker -> lets a re-run refresh status

# Suppress ALL mail/log noise on the create; we send template 37 explicitly afterwards.
# `no_mail_to_attendees` stops Odoo's generic .ics calendar invite so we don't double-mail.
NOISE_OFF = {
    "no_mail_to_attendees": True,
    "mail_create_nolog": True,
    "mail_create_nosubscribe": True,
    "mail_notify_author": False,
    "tracking_disable": True,
}

C = None
ARGS = None
_RESOURCES = {}     # id -> name, loaded once
_APPT_TYPES = set()  # valid appointment.type ids, loaded once
_CREATED = 0        # (would-)creations this run -- the --max cap counts against this


def log(msg):
    print(msg, flush=True)


def vlog(msg):
    if ARGS and ARGS.verbose:
        print("  . " + msg, flush=True)


# ---------------------------------------------------------------------------
# SDBK1 parsing + resource / time resolution
# ---------------------------------------------------------------------------
def type_code(service_label):
    """2-letter booking-type code for the calendar tile:
    EP Exterior · IP Interior · SD Supreme · PO Pet Owner · RS Re-Sell (Sell-Ready)."""
    l = (service_label or "").lower()
    if "exterior" in l:
        return "EP"
    if "interior" in l:
        return "IP"
    if "supreme" in l:
        return "SD"
    if "pet" in l:
        return "PO"
    if "sell" in l or "resell" in l or "re-sell" in l:
        return "RS"
    return (service_label[:2] or "??").upper()


def tile_title(sdbk, order, resource_name):
    """[💰 if paid] [A/K detailer] CODE Suburb  e.g. '💰 K SD Onehunga' or 'A EP Milford'.
    The Meetings calendar can't colour by detailer on a single login (Odoo colours every
    event in the logged-in user's colour), so the A/K LETTER is the detailer signal, not
    colour. 💰 = paid (gold, reads on any tile); blank = unpaid. EP/IP/SD/PO/RS = type."""
    paid = "💰 " if is_paid(order) else ""
    ini = (resource_name[:1] or "").upper()   # A (Alex) / K (Kade)
    return f"{paid}{ini} {type_code(sdbk['service_label'])} {sdbk['suburb']}"


def detailer_partner_for(resource_id):
    """The detailer CONTACT (res.partner) that mirrors this appointment.resource.
    Returns (partner_id, has_email). The contact MUST carry an email or Odoo's calendar
    attendee sync reaps it ~60-80s later (the 'colour vanishes' root cause)."""
    pid = RESOURCE_PARTNER.get(resource_id)
    if not pid:
        return None, False
    p = C.call("res.partner", "read", [pid], fields=["email"])
    has_email = bool(p and (p[0].get("email") or "").strip())
    return pid, has_email


def ensure_detailer_attendee(event_id, resource_id):
    """Attach the detailer contact as a calendar attendee (idempotent, self-healing).
    Makes the booking show on the main calendar + colour by detailer. Re-runs safely: only
    writes if the detailer is currently missing, so a later run repairs any strip (manual
    edit, or a residual reap if the contact's email was blank at create time). Warns loudly
    when the detailer contact has no email, because Odoo will then reap it ~80s later."""
    pid, has_email = detailer_partner_for(resource_id)
    if not pid:
        return
    if not has_email:
        log(f"      WARNING: detailer contact {pid} (resource {resource_id}) has NO email "
            f"-> Odoo reaps emailless attendees ~80s later and the detailer colour vanishes. "
            f"Set an email on that contact to fix the colour.")
    cur = C.call("calendar.event", "read", [event_id], fields=["partner_ids"])
    if not cur:
        log(f"      WARNING: event {event_id} not readable -> detailer colour self-heal "
            f"skipped (event may have been deleted).")
        return
    if pid not in cur[0]["partner_ids"]:
        if ARGS.dry_run:
            vlog(f"[dry-run] would attach detailer {pid} to event {event_id}")
        else:
            C.call("calendar.event", "write", [event_id],
                   {"partner_ids": [(4, pid)]}, context=NOISE_OFF)
            vlog(f"detailer {pid} attached to event {event_id}")


def parse_sdbk1(raw):
    """Parse an SDBK1 pipe string into a dict, or return None if not one/malformed."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("SDBK1"):
        return None
    parts = raw.split("|")
    # SDBK1 | date | time24 | durationHours | apptTypeId | resourceName | suburb | serviceLabel
    if len(parts) < 8:
        vlog(f"SDBK1 too short ({len(parts)} fields): {raw!r}")
        return None
    try:
        return {
            "date": parts[1].strip(),
            "time24": parts[2].strip(),
            "duration": float(parts[3].strip()),
            "appt_type_id": int(parts[4].strip()),
            "resource_name": parts[5].strip(),
            "suburb": parts[6].strip(),
            "service_label": parts[7].strip(),
            "raw": raw,
        }
    except (ValueError, IndexError) as e:
        vlog(f"SDBK1 parse error ({e}): {raw!r}")
        return None


def resolve_resource(name):
    """Match a booked resource NAME to an appointment.resource id.

    Exact match first, then a NON-CROSSING first-name substring
    (Alex -> 1 North Shore, Kade -> 2 Central Auckland).
    """
    if not name:
        return None
    low = name.strip().lower()
    for rid, rname in _RESOURCES.items():
        if rname.strip().lower() == low:
            return rid
    for rid, rname in _RESOURCES.items():
        first = rname.split("(")[0].strip().lower()   # "Alex (North Shore)" -> "alex"
        if first and (first in low or low in rname.strip().lower()):
            return rid
    return None


def nz_to_utc(date_str, time24):
    """Convert an NZ-local date + 'HH:MM' to a naive UTC 'YYYY-MM-DD HH:MM:SS' string.

    zoneinfo applies the correct offset for the date (NZST +12 / NZDT +13), so
    DST transitions (~early Apr, late Sep) are handled automatically.
    """
    local = datetime.strptime(f"{date_str} {time24}", "%Y-%m-%d %H:%M").replace(tzinfo=NZ)
    return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def utc_stop(start_utc_str, duration_hours):
    """stop = start + duration (in UTC, so DST never double-counts)."""
    start = datetime.strptime(start_utc_str, "%Y-%m-%d %H:%M:%S")
    return (start + timedelta(hours=duration_hours)).strftime("%Y-%m-%d %H:%M:%S")


def now_utc_str():
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover_bookings():
    """Return a list of {order, line, sdbk, source} for every SDBK1 line.

    Two channels merged and de-duped by line id: the production custom-value
    channel and the test fallback (SDBK1 token in the line name).
    """
    # ---- Channel 1: product.attribute.custom.value (the real capture) ----
    cav = C.call("product.attribute.custom.value", "search_read",
                 [["custom_value", "=like", "SDBK1%"]],
                 fields=["id", "custom_value", "sale_order_line_id"])
    vlog(f"discover: {len(cav)} SDBK1 custom-values")
    line_ids = sorted({v["sale_order_line_id"][0] for v in cav if v.get("sale_order_line_id")})
    cav_by_line = {}
    for v in cav:
        if v.get("sale_order_line_id"):
            cav_by_line[v["sale_order_line_id"][0]] = v["custom_value"]

    # ---- Channel 2: SDBK1 token in the line NAME. Serves BOTH the manual test fallback
    #      AND a production safety net: Odoo bakes the custom value into the line name as
    #      "Booking: Custom: SDBK1|..." at add-to-cart, so the token survives on the name
    #      even if the product.attribute.custom.value record is later missing. Match
    #      "SDBK1|" ANYWHERE in the chunk (not startswith) so the "Booking: Custom: " render
    #      prefix doesn't hide it. ----
    name_lines = C.call("sale.order.line", "search_read",
                        [["name", "ilike", "SDBK1|"]],
                        fields=["id", "name"])
    vlog(f"discover: {len(name_lines)} lines with SDBK1 in name")
    fallback_by_line = {}
    for ln in name_lines:
        txt = ln.get("name") or ""
        pos = txt.find("SDBK1|")
        if pos == -1:
            continue
        # take from SDBK1 to the end of that physical line (drop any leading render prefix)
        chunk = txt[pos:].splitlines()[0].strip()
        fallback_by_line[ln["id"]] = chunk
        if ln["id"] not in line_ids:
            line_ids.append(ln["id"])

    if not line_ids:
        return []

    lines = C.call("sale.order.line", "read", sorted(line_ids),
                   fields=["id", "product_id", "name", "order_id", "product_uom_qty"])
    line_map = {ln["id"]: ln for ln in lines}

    order_ids = sorted({ln["order_id"][0] for ln in lines if ln.get("order_id")})
    orders = C.call("sale.order", "read", order_ids,
                    fields=["id", "name", "state", "partner_id", "client_order_ref",
                            "date_order", "website_id", "amount_total", "transaction_ids",
                            "opportunity_id"])
    order_map = {o["id"]: o for o in orders}

    records = []
    for lid in line_ids:
        ln = line_map.get(lid)
        if not ln or not ln.get("order_id"):
            continue
        order = order_map.get(ln["order_id"][0])
        if not order:
            continue
        raw = cav_by_line.get(lid) or fallback_by_line.get(lid)
        source = "custom_value" if lid in cav_by_line else "name_fallback"
        sdbk = parse_sdbk1(raw)
        if not sdbk:
            log(f"  {order['name']} L{lid}: malformed SDBK1 {raw!r} -- skipped")
            continue
        records.append({"order": order, "line": ln, "sdbk": sdbk, "source": source})
    return records


def is_paid(order):
    return order.get("state") in PAID_STATES


def is_eligible(order):
    """Confirmed-booking gate + optional --since / --only-order filters.

    The shop takes bank-transfer / cash-on-the-day, so a genuine booking often stays in
    state 'sent' (awaiting payment) rather than 'sale'. We WANT those on the calendar as
    AWAITING PAYMENT (owner's Phase-2 decision). But a 'sent' order can also be a bare
    emailed quote or a FAILED-card abandonment, and an unattended cron that emails the
    customer must never act on those. So 'sent' is eligible ONLY when it carries a payment
    intent that is not failed/cancelled (a pending bank-transfer / authorized / done tx).
      draft = abandoned cart (excluded). sale/done = paid. sent = awaiting a live payment.
    """
    st = order.get("state")
    if st not in CONFIRMED_STATES:
        return False, f"state={st} (not a confirmed booking)"
    # Cheap local filters FIRST -> skip the payment.transaction RPC for excluded orders.
    if ARGS.since:
        do = (order.get("date_order") or "")[:10]
        if do and do < ARGS.since:
            return False, f"date_order {do} < --since {ARGS.since}"
    if ARGS.only_order:
        want = ARGS.only_order.strip()
        if want != order["name"] and want != str(order["id"]):
            return False, "excluded by --only-order"
    if st == "sent":
        txids = order.get("transaction_ids") or []
        if not txids:
            return False, "sent quote with no checkout/payment attempt (abandoned)"
        # transaction_ids includes FAILED/CANCELLED attempts; existence != booking. Require
        # at least one tx that isn't in a dead state (a live bank-transfer/COD intent = a
        # real booking awaiting payment; error/cancel only = a failed-card abandonment).
        txs = C.call("payment.transaction", "search_read",
                     [["id", "in", txids]], fields=["state"])
        live = [t for t in txs if (t.get("state") or "") not in ("cancel", "error")]
        if not live:
            return False, "sent quote: all payment attempts failed/cancelled (abandoned)"
    return True, "ok"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def already_synced(order, line_id, booker_partner_id, start_utc, appt_type_id):
    """Deterministic search-before-create. Returns an existing event id or None.

    1. Order-ref audit token (SDCAL:L<line>=E<event>) -- survives on the order,
       untouched by enrich_calendar_events.py.
    2. BOUNDED per-line marker (full HTML comment) in the event description.
    3. Natural key (booker partner + exact start + appointment type).
    """
    # 1. order-ref token (cheap, one existence check). The token can outlive its event if
    #    someone deletes the event in the backend -> verify the id still exists, else fall
    #    through to re-create (otherwise every run would error writing to a ghost id).
    ref = order.get("client_order_ref") or ""
    tok_prefix = f"SDCAL:L{line_id}=E"
    idx = ref.find(tok_prefix)
    if idx != -1:
        tail = ref[idx + len(tok_prefix):]
        num = ""
        for ch in tail:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            if C.call("calendar.event", "search", [["id", "=", int(num)]], limit=1):
                return int(num)
            vlog(f"L{line_id}: order-ref token points to deleted event {num} -> re-create")

    # 2. BOUNDED marker search (full comment -> no L12/L123 collision)
    marker = MARKER_COMMENT.format(line_id=line_id)
    hit = C.call("calendar.event", "search",
                 [["description", "ilike", marker]], limit=1)
    if hit:
        return hit[0]

    # 3. natural-key fallback (booker + exact start + type). NOT line-scoped, so guard
    #    against collapsing a DIFFERENT line's event into this one (two vehicles on one
    #    order sharing booker+start+type): if the candidate already carries another line's
    #    bounded marker, it's not ours -> fall through to create this line its own event.
    if booker_partner_id:
        hit = C.call("calendar.event", "search",
                     [["appointment_booker_id", "=", booker_partner_id],
                      ["start", "=", start_utc],
                      ["appointment_type_id", "=", appt_type_id]], limit=1)
        if hit:
            cand = C.call("calendar.event", "read", [hit[0]], fields=["description"])
            cdesc = (cand[0].get("description") if cand else "") or ""
            other = "<!-- SDBK1:L" in cdesc and marker not in cdesc
            if not other:
                return hit[0]
            vlog(f"L{line_id}: natural-key hit {hit[0]} belongs to another line -> create own")
    return None


def slot_conflict(resource_id, start_utc, stop_utc):
    """Return existing event ids that overlap [start,stop] on the SAME resource."""
    dom = [["appointment_resource_ids", "in", [resource_id]],
           ["start", "<", stop_utc],
           ["stop", ">", start_utc]]
    return C.call("calendar.event", "search", dom)


def build_description(sdbk, resource_name, line_id, order):
    """Human-readable event body + the hidden BOUNDED per-line idempotency marker.

    Kept non-empty on purpose so enrich_calendar_events.py leaves it alone
    (that script only fills events whose description is empty) -- the marker
    therefore never gets clobbered in normal (non --force) operation.

    The order line makes the PAID status visible on the calendar: this event only
    exists because the order reached a paid/confirmed state (sale/done -> the eligibility
    gate), and the order number + total let a detailer cross-check payment in one click.
    """
    # The calendar TILE shows paid via the title's 💰, but this description flows into the
    # customer's booking EMAIL (mail.template 37 renders object.description), so keep the
    # payment status here: paid = 💰 PAID, unpaid = ⏳ AWAITING PAYMENT + how to pay. The
    # owner's "drop awaiting" note was about the calendar tile (title), not the email.
    paid = ("\U0001f4b0 PAID"
            if is_paid(order)
            else "⏳ AWAITING PAYMENT (pay by bank transfer or cash on the day)")
    amt = order.get("amount_total")
    amt_str = f" · ${amt:.2f} NZD" if amt else ""
    lines = [
        paid,
        f"\U0001f697 Service: {sdbk['service_label']}",
        f"\U0001f4cd Suburb: {sdbk['suburb']}",
        f"\U0001f464 Detailer: {resource_name}",
        f"\U0001f550 {sdbk['date']} {sdbk['time24']} ({sdbk['duration']}h) NZ",
        f"\U0001f9fe Order {order.get('name')}{amt_str}",
        "",
        SYNC_TAG,
        MARKER_COMMENT.format(line_id=line_id),
        # hidden payment-state marker -> lets a re-run detect sent->sale and flip 💰
        PST_MARKER.format(state=order.get("state")),
    ]
    return "<br/>\n".join(lines)


def append_order_marker(order, line_id, event_id):
    """Append the audit token to sale.order.client_order_ref (idempotent)."""
    ref = order.get("client_order_ref") or ""
    token = ORDER_TOKEN.format(line_id=line_id, event_id=event_id)
    if token in ref:
        return
    new_ref = (ref + ";" if ref and not ref.endswith(";") else ref) + token
    C.call("sale.order", "write", [order["id"]], {"client_order_ref": new_ref},
           context=NOISE_OFF)
    order["client_order_ref"] = new_ref  # keep in-memory copy fresh


# ---------------------------------------------------------------------------
# Blank-booking watchdog
# ---------------------------------------------------------------------------
def _chat_hook():
    hook = os.environ.get("GCHAT_WEBHOOK_URL")
    if hook:
        return hook
    try:
        return cfg("GCHAT_WEBHOOK_URL")
    except Exception:
        return ""


def _chat_alert(text):
    """Best-effort plain-text post to the Google Chat space. Never raises."""
    hook = _chat_hook()
    if not hook:
        return False
    try:
        req = urllib.request.Request(
            hook, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 - alerts must never fail the run
        log(f"    Chat alert failed: {type(e).__name__}: {e}")
        return False


def check_blank_bookings(writer):
    """WATCHDOG (2026-07-09, after S00075): a service line on an ELIGIBLE order whose hidden
    Booking custom value is blank/garbage means the customer's chosen slot was LOST client-side
    (pre-fix swapLine wiped it; pricing-modal adds never captured one). The main pass cannot
    see those lines (discovery needs 'SDBK1%'), so without this check a confirmed job silently
    never reaches the calendar. Loud log + one-time Google Chat alert per line (NOBK token on
    the order marks it as alerted; dry-run only reports)."""
    domain = ["&",
              ["custom_product_template_attribute_value_id", "in", BOOKING_PTAVS],
              "|", ["custom_value", "=", False], ["custom_value", "not like", "SDBK1|"]]
    cavs = C.call("product.attribute.custom.value", "search_read", domain,
                  fields=["id", "custom_value", "sale_order_line_id"])
    vlog(f"blank-booking watchdog: {len(cavs)} blank/malformed Booking value(s)")
    line_ids = sorted({v["sale_order_line_id"][0] for v in cavs if v.get("sale_order_line_id")})
    if not line_ids:
        return 0
    lines = C.call("sale.order.line", "read", line_ids,
                   fields=["id", "name", "order_id", "product_uom_qty"])
    order_ids = sorted({l["order_id"][0] for l in lines if l.get("order_id")})
    orders = C.call("sale.order", "read", order_ids,
                    fields=["id", "name", "state", "partner_id", "client_order_ref",
                            "date_order", "amount_total", "transaction_ids"])
    omap = {o["id"]: o for o in orders}
    alerted = 0
    for ln in lines:
        if not ln.get("order_id"):
            continue
        order = omap.get(ln["order_id"][0])
        if not order:
            continue
        ok, why = is_eligible(order)
        if not ok:
            vlog(f"    blank-booking L{ln['id']} {order['name']}: ineligible ({why})")
            continue
        token = NOBK_TOKEN.format(line_id=ln["id"])
        if token in (order.get("client_order_ref") or ""):
            vlog(f"    blank-booking L{ln['id']} {order['name']}: already alerted")
            continue
        svc = (ln.get("name") or "").splitlines()[0][:70]
        pname = order["partner_id"][1] if order.get("partner_id") else "?"
        msg = (f"⚠️ BOOKING MISSING — order {order['name']} ({pname}, "
               f"${order.get('amount_total')}) line ‘{svc}’ has no appointment time "
               f"attached. The customer's chosen slot was lost before checkout — contact "
               f"them and book it manually: {C.url}/odoo/sales/{order['id']}")
        log(f"    *** {msg}")
        writer.writerow([_now(), order["name"], ln["id"], "blank-booking", "", svc])
        alerted += 1
        if ARGS.commit:
            posted = _chat_alert(msg)
            log(f"    blank-booking alert posted to Chat: {posted}")
            # Mark as alerted once it reached Chat (or when no hook is configured at all, so
            # log-only setups don't re-log forever). A failed post retries next run.
            if posted or not _chat_hook():
                ref = order.get("client_order_ref") or ""
                new_ref = (ref + ";" if ref and not ref.endswith(";") else ref) + token
                C.call("sale.order", "write", [order["id"]], {"client_order_ref": new_ref},
                       context=NOISE_OFF)
                order["client_order_ref"] = new_ref
    return alerted


# ---------------------------------------------------------------------------
# CRM: one opportunity per booked order
# ---------------------------------------------------------------------------
def _order_event_ids(order):
    """Event ids recorded in the order's SDCAL audit tokens (fresh in-memory copy)."""
    ref = order.get("client_order_ref") or ""
    return [int(m.group(1)) for m in re.finditer(r"SDCAL:L\d+=E(\d+)", ref)]


def ensure_crm_opp(order, recs, writer):
    """Create/upgrade THE opportunity for a booked order. Returns an outcome string.

    - Idempotent via sale.order.opportunity_id; if unset, ADOPTS an existing opp already
      linked to one of the order's calendar events (the appointment_crm lead_create era).
    - Stage: Booked (paid) vs Booked (Unpaid); only ever moves a stage that is currently
      New or Booked (Unpaid) -- never touches a stage a human advanced, never downgrades.
    - An ARCHIVED linked opp is left completely alone (deliberate hide).
    """
    paid = is_paid(order)
    target_stage = CRM_STAGE_BOOKED if paid else CRM_STAGE_BOOKED_UNPAID
    ev_ids = _order_event_ids(order)

    # resolve team from the first line's detailer
    rid = resolve_resource(recs[0]["sdbk"]["resource_name"]) if recs else None
    team_id = CRM_TEAM_BY_RESOURCE.get(rid, CRM_TEAM_FALLBACK)
    tag_ids = [CRM_TAG_BOOKING] + ([CRM_REGION_TAG[team_id]] if team_id in CRM_REGION_TAG else [])

    opp_id = order["opportunity_id"][0] if order.get("opportunity_id") else None
    adopted = False
    if not opp_id and ev_ids:
        evs = C.call("calendar.event", "read", ev_ids, fields=["opportunity_id"])
        for e in evs:
            if e.get("opportunity_id"):
                opp_id = e["opportunity_id"][0]
                adopted = True
                break

    if opp_id:
        opp = C.call("crm.lead", "read", [opp_id],
                     fields=["id", "name", "stage_id", "expected_revenue", "tag_ids", "active"],
                     context={"active_test": False})
        opp = opp[0] if opp else None
        if not opp:
            opp_id = None          # ghost link (opp deleted) -> recreate below
        elif not opp.get("active"):
            vlog(f"    CRM {order['name']}: opp {opp_id} archived -- leaving alone")
            return "crm-skip"
        else:
            vals = {}
            cur_stage = opp["stage_id"][0] if opp.get("stage_id") else None
            if cur_stage in (CRM_STAGE_NEW, CRM_STAGE_BOOKED_UNPAID) and cur_stage != target_stage:
                vals["stage_id"] = target_stage
            if not opp.get("expected_revenue"):
                vals["expected_revenue"] = order.get("amount_total") or 0.0
            missing_tags = [t for t in tag_ids if t not in (opp.get("tag_ids") or [])]
            if missing_tags:
                vals["tag_ids"] = [(4, t) for t in missing_tags]
            link_order = adopted or not order.get("opportunity_id")
            unlinked_evs = []
            if ev_ids:
                evs = C.call("calendar.event", "read", ev_ids, fields=["opportunity_id"])
                unlinked_evs = [e["id"] for e in evs if not e.get("opportunity_id")]
            if not vals and not link_order and not unlinked_evs:
                vlog(f"    CRM {order['name']}: opp {opp_id} current -- nothing to do")
                return "crm-current"
            if ARGS.dry_run:
                log(f"    CRM {order['name']}: DRY-RUN would update opp {opp_id} "
                    f"{vals} link_order={link_order} link_events={unlinked_evs}")
                return "crm-would-update"
            if vals:
                C.call("crm.lead", "write", [opp_id], vals, context=NOISE_OFF)
            if link_order:
                C.call("sale.order", "write", [order["id"]], {"opportunity_id": opp_id},
                       context=NOISE_OFF)
                order["opportunity_id"] = [opp_id, opp["name"]]
            if unlinked_evs:
                C.call("calendar.event", "write", unlinked_evs, {"opportunity_id": opp_id},
                       context=NOISE_OFF)
            log(f"    CRM {order['name']}: opp {opp_id} updated {vals or ''}"
                f"{' +adopted' if adopted else ''}{' +events' + str(unlinked_evs) if unlinked_evs else ''}")
            writer.writerow([_now(), order["name"], "", "crm-updated", opp_id, str(vals)])
            return "crm-updated"

    # ---- create ----
    pname = order["partner_id"][1] if order.get("partner_id") else "?"
    dates = sorted(r["sdbk"]["date"] for r in recs)
    desc_lines = ["<div>" + ("PAID ✅" if paid else "AWAITING PAYMENT ⏳ (bank transfer / cash on day)")]
    for r in recs:
        s = r["sdbk"]
        desc_lines.append(f"🚗 {s['service_label']} — {s['date']} {s['time24']} "
                          f"({s['duration']}h) — {s['resource_name']} — {s['suburb']}")
    desc_lines.append(f"🧾 {order['name']} ${order.get('amount_total')}</div>")
    vals = {
        "name": f"Booking {order['name']} - {pname}",
        "type": "opportunity",
        "partner_id": order["partner_id"][0] if order.get("partner_id") else False,
        "team_id": team_id,
        "stage_id": target_stage,
        "expected_revenue": order.get("amount_total") or 0.0,
        "tag_ids": [(6, 0, tag_ids)],
        "date_deadline": dates[0] if dates else False,
        "description": "<br/>".join(desc_lines),
    }
    if ARGS.dry_run:
        log(f"    CRM {order['name']}: DRY-RUN would create opp "
            f"[team {team_id}, stage {target_stage}, ${vals['expected_revenue']}]")
        return "crm-would-create"
    res = C.call("crm.lead", "create", vals, context=NOISE_OFF)
    opp_id = res[0] if isinstance(res, (list, tuple)) else res   # Rule 10a: create returns [id]
    C.call("sale.order", "write", [order["id"]], {"opportunity_id": opp_id}, context=NOISE_OFF)
    order["opportunity_id"] = [opp_id, vals["name"]]
    if ev_ids:
        C.call("calendar.event", "write", ev_ids, {"opportunity_id": opp_id}, context=NOISE_OFF)
    log(f"    CRM {order['name']}: created opp {opp_id} [team {team_id}, "
        f"stage {'Booked' if paid else 'Booked (Unpaid)'}, ${vals['expected_revenue']}] "
        f"events={ev_ids}")
    writer.writerow([_now(), order["name"], "", "crm-created", opp_id,
                     f"team{team_id}|stage{target_stage}|{vals['expected_revenue']}"])
    return "crm-created"


def ensure_crm_opps(records, writer):
    """Group SDBK1 records by order and ensure each ELIGIBLE order has its opportunity.
    Runs every pass, so a sent->sale transition auto-promotes Booked (Unpaid) -> Booked."""
    by_order = {}
    for rec in records:
        by_order.setdefault(rec["order"]["id"], []).append(rec)
    tallies = {}
    for oid, recs in sorted(by_order.items()):
        order = recs[0]["order"]
        try:
            ok, why = is_eligible(order)
            if not ok:
                vlog(f"    CRM {order['name']}: ineligible ({why})")
                continue
            outcome = ensure_crm_opp(order, recs, writer)
            tallies[outcome] = tallies.get(outcome, 0) + 1
        except Exception as e:  # noqa: BLE001 - one bad order never aborts the pass
            tallies["crm-error"] = tallies.get("crm-error", 0) + 1
            log(f"    CRM ERROR {order['name']}: {type(e).__name__}: {e}")
            writer.writerow([_now(), order["name"], "", "crm-error", "",
                             f"{type(e).__name__}: {e}"])
    return tallies


# ---------------------------------------------------------------------------
# Core: process one booking line
# ---------------------------------------------------------------------------
def process(rec, writer):
    order = rec["order"]
    line = rec["line"]
    sdbk = rec["sdbk"]
    oname = order["name"]
    lid = line["id"]

    partner = order.get("partner_id")
    booker_id = partner[0] if partner else None
    partner_name = partner[1] if partner else "?"

    # --- Validate appointment type (covers combo/add-on types capture may miss) ---
    if sdbk["appt_type_id"] not in _APPT_TYPES:
        log(f"  {oname} L{lid}: appt_type {sdbk['appt_type_id']} not a live appointment.type -- skipped")
        writer.writerow([_now(), oname, lid, "error", "",
                         f"unknown appt_type {sdbk['appt_type_id']}"])
        return "error"

    # --- Resolve time + resource ---
    try:
        start_utc = nz_to_utc(sdbk["date"], sdbk["time24"])
        stop_utc = utc_stop(start_utc, sdbk["duration"])
    except Exception as e:
        log(f"  {oname} L{lid}: BAD datetime {sdbk['date']} {sdbk['time24']} ({e}) -- skipped")
        writer.writerow([_now(), oname, lid, "error", "", f"bad datetime: {e}"])
        return "error"

    resource_id = resolve_resource(sdbk["resource_name"])
    if not resource_id:
        log(f"  {oname} L{lid}: resource {sdbk['resource_name']!r} not matched -- skipped")
        writer.writerow([_now(), oname, lid, "error", "", f"unmatched resource {sdbk['resource_name']}"])
        return "error"
    resource_name = _RESOURCES[resource_id]

    # --- Idempotency: already synced? ---
    existing = already_synced(order, lid, booker_id, start_utc, sdbk["appt_type_id"])
    if existing:
        # Self-heal the detailer colour: re-attach the detailer participant if anything
        # stripped it since last run (manual edit, or an emailless-attendee reap). Idempotent
        # -- only writes when actually missing.
        ensure_detailer_attendee(existing, resource_id)
        # Refresh PAID/AWAITING status if the order moved on (e.g. sent -> sale after
        # the bank transfer cleared). No re-email; just update the event body.
        want = PST_MARKER.format(state=order.get("state"))
        ev = C.call("calendar.event", "read", [existing], fields=["description"])
        desc = (ev[0].get("description") if ev else "") or ""
        if want not in desc:
            newstat = "PAID 💰" if is_paid(order) else "unpaid"
            if ARGS.dry_run:
                log(f"  {oname} L{lid}: event {existing} status -> {newstat} (would-update)")
                writer.writerow([_now(), oname, lid, "would-update", existing, f"state={order.get('state')}"])
                return "would-update"
            # paid status now lives in the TITLE (✅ tick), so refresh the title + body
            C.call("calendar.event", "write", [existing],
                   {"name": tile_title(sdbk, order, resource_name),
                    "description": build_description(sdbk, resource_name, lid, order)},
                   context=NOISE_OFF)
            log(f"  {oname} L{lid}: event {existing} status -> {newstat} (updated)")
            writer.writerow([_now(), oname, lid, "updated", existing, f"state={order.get('state')}"])
            return "updated"
        log(f"  {oname} L{lid}: already synced -> event {existing} (status current) -- skip")
        writer.writerow([_now(), oname, lid, "skip", existing, "already synced"])
        return "skip"

    # --- Past-date guard ---
    is_past = start_utc <= now_utc_str()
    if is_past and not ARGS.include_past:
        log(f"  {oname} L{lid}: start {start_utc} UTC is in the PAST -- skipped "
            f"(use --include-past to backfill event-only)")
        writer.writerow([_now(), oname, lid, "skip-past", "", f"start {start_utc} in past"])
        return "skip"

    # --- Qty guard (merged/duplicate line) ---
    qty = line.get("product_uom_qty") or 1
    if qty and qty > 1:
        log(f"  {oname} L{lid}: WARNING product_uom_qty={qty} -- creating ONE event; "
            f"verify manually if this represents multiple vehicles")

    # --- Slot-collision guard ---
    conflicts = slot_conflict(resource_id, start_utc, stop_utc)
    # (existing was None, so any hit is a genuine other-booking overlap)
    if conflicts and not ARGS.allow_overlap:
        log(f"  {oname} L{lid}: SLOT CONFLICT on {resource_name} "
            f"[{start_utc}..{stop_utc}] with event(s) {conflicts} -- skipped "
            f"(use --allow-overlap to force)")
        writer.writerow([_now(), oname, lid, "conflict", "",
                         f"overlaps {conflicts} on res{resource_id}"])
        return "conflict"

    # Tile-friendly title: paid marker + customer + short service + suburb (time shows
    # automatically on the calendar tile; detailer shows via colour/description).
    ename = tile_title(sdbk, order, resource_name)
    location = f"{sdbk['suburb']}, Auckland" if sdbk["suburb"] else "Auckland"

    # --- Email eligibility (present address + future + not --no-email) ---
    booker_email = None
    booker_mobile = None
    if booker_id:
        # NB: res.partner on this SaaS has NO 'mobile' field (Odoo 19) -> use phone only.
        pinfo = C.call("res.partner", "read", [booker_id],
                       fields=["email", "phone"])
        if pinfo:
            booker_email = (pinfo[0].get("email") or "").strip()
            booker_mobile = (pinfo[0].get("phone") or "").strip()

    # --- Alarms: base always; SMS only with --with-sms AND a mobile, AND not past ---
    # --no-email means NO mail at all -> also drop the event-level Email alarm (id 6), else
    # a 'safe test' future booking still emails booker + detailer at T-3h.
    alarms = list(BASE_ALARMS)
    if ARGS.no_email:
        alarms = [a for a in alarms if a != EMAIL_ALARM]
    if ARGS.with_sms and booker_mobile and not is_past:
        alarms.append(SMS_ALARM)

    log(f"  {oname} L{lid}: {ename}")
    log(f"      start(UTC)={start_utc} stop(UTC)={stop_utc} dur={sdbk['duration']}h "
        f"type={sdbk['appt_type_id']} resource={resource_id}:{resource_name} "
        f"booker={booker_id}:{partner_name} email={booker_email or '(none)'} "
        f"alarms={alarms} past={is_past} src={rec['source']}")

    # --- Creation cap (--max): applies ONLY to would-be creations. Already-synced lines
    #     returned skip/update above and are never throttled, so colour self-heal + status
    #     refresh always run. Counted in dry-run too, so a --max preview matches --commit. ---
    global _CREATED
    if ARGS.max and _CREATED >= ARGS.max:
        log(f"  {oname} L{lid}: --max {ARGS.max} reached -- deferred to next run")
        writer.writerow([_now(), oname, lid, "deferred", "", f"--max {ARGS.max} reached"])
        return "deferred"
    _CREATED += 1

    if ARGS.dry_run:
        log("      DRY-RUN -- would create calendar.event"
            + ("" if ARGS.no_email or is_past else " + send template 37"))
        writer.writerow([_now(), oname, lid, "would-create", "",
                         f"{start_utc}|type{sdbk['appt_type_id']}|res{resource_id}|past{int(is_past)}"])
        return "would-create"

    # --- Create the event (mirror event 2) ---
    vals = {
        "name": ename,
        "start": start_utc,
        "stop": stop_utc,
        "duration": sdbk["duration"],
        "partner_id": ORGANIZER_PARTNER,
        "user_id": ORGANIZER_USER,
        # Attendees = the customer AND the org partner, so the booking shows on the
        # single Supreme Detailing calendar (Odoo Calendar filters to "my" attendees;
        # detailer separation is via appointment_resource_ids + the description).
        "partner_ids": [(6, 0, [booker_id])] if booker_id else [(6, 0, [])],
        "appointment_booker_id": booker_id,
        "appointment_type_id": sdbk["appt_type_id"],
        # Assign the detailer via a booking line (carries capacity) -- NOT the m2m
        # directly, which errors "Missing required value ... capacity_reserved".
        "booking_line_ids": [(0, 0, {
            "appointment_resource_id": resource_id,
            "capacity_reserved": 1,
            "capacity_used": 1,
        })],
        "alarm_ids": [(6, 0, alarms)],
        "appointment_status": "booked",
        # colour tag by detailer (green North Shore / red Central)
        "categ_ids": [(6, 0, [RESOURCE_TAG[resource_id]])] if resource_id in RESOURCE_TAG else [],
        "location": location,
        # Mobile detailing has no video call -> suppress the auto Google Meet link so it
        # never appears in the booking email (belt-and-braces; the appointment types also
        # have event_videocall_source cleared).
        "videocall_location": False,
        "description": build_description(sdbk, resource_name, lid, order),
    }
    event_id = C.call("calendar.event", "create", vals, context=NOISE_OFF)
    if isinstance(event_id, (list, tuple)):
        event_id = event_id[0]
    log(f"      created calendar.event {event_id}")

    # The appointment create strips partner_ids to the booker, so add the DETAILER contact
    # (Alex/Kade) as a participant. This makes the detailer BOTH the resource (lanes/
    # availability) AND a participant -> the booking shows on the main Meetings calendar and
    # auto-colours by detailer. REQUIRES the detailer contact to have an email (else Odoo
    # reaps it ~80s later); ensure_detailer_attendee() warns if it doesn't. Contacts, not
    # Odoo user accounts -> no paid seats.
    ensure_detailer_attendee(event_id, resource_id)

    # --- Order-level audit token ---
    append_order_marker(order, lid, event_id)

    # --- Fire the native "Appointment Booked" email (template 37 -> attendee) ---
    mailed = "not-sent"
    if is_past:
        mailed = "past-no-email"
    elif ARGS.no_email:
        mailed = "no-email-flag"
        log("      --no-email -- template 37 not sent")
    elif not booker_id:
        mailed = "no-booker"
    elif not booker_email:
        mailed = "no-email-address"
        log("      WARNING: booker has no email -- template 37 skipped")
    else:
        att = C.call("calendar.attendee", "search",
                     [["event_id", "=", event_id], ["partner_id", "=", booker_id]], limit=1)
        if att:
            try:
                # force_send=False -> queue via mail.mail. If the SaaS daily email cap is hit
                # mid-backfill the message stays 'outgoing' and Odoo's mail cron RETRIES it
                # automatically (force_send=True would raise synchronously and the confirmation
                # would be lost forever, since a re-run finds the event already synced and
                # never re-attempts the send).
                C.call("mail.template", "send_mail", [BOOKED_TEMPLATE], att[0],
                       force_send=False)
                mailed = f"queued att{att[0]}"
                log(f"      queued template {BOOKED_TEMPLATE} to attendee {att[0]} <{booker_email}>")
            except Exception as e:
                mailed = f"email-failed: {e}"
                log(f"      WARNING: template {BOOKED_TEMPLATE} queue failed: {e}")
        else:
            mailed = "no-attendee"
            log("      WARNING: no calendar.attendee found -- email skipped")

    writer.writerow([_now(), oname, lid, "created", event_id,
                     f"{start_utc}|type{sdbk['appt_type_id']}|res{resource_id}|{mailed}"])
    return "created"


def _now():
    return datetime.now(NZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Single-instance lock: search-before-create is not atomic over RPC, so two overlapping
# runs (a manual --commit backfill firing while the scheduled cron runs, or two scheduled
# runs) could BOTH see a line as unsynced and both create the event + both send template 37.
# A cross-platform O_EXCL lock file prevents that. A stale lock (crashed run) is stolen.
# ---------------------------------------------------------------------------
LOCK_PATH = Path(__file__).resolve().parent / ".sync_bookings.lock"
LOCK_STALE_SECONDS = 1800   # 30 min -> assume the holder crashed and reclaim


def acquire_lock():
    # At most ONE steal attempt -> never recurse unboundedly if a stale lock is undeletable.
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                age = 0
            if age <= LOCK_STALE_SECONDS:
                return False            # held by a live run
            log(f"    lock {LOCK_PATH.name} is stale ({int(age)}s old) -- stealing it")
            try:
                LOCK_PATH.unlink()
            except OSError:
                return False            # can't steal (perms / open handle) -> give up, no recursion
            # loop once more to re-create; if we lose the create race, the retry returns False
    return False


def release_lock():
    # Only unlink OUR lock: if a long run's lock was stolen (age > stale) by a newer run,
    # the file now belongs to the stealer -> deleting it would drop live mutual exclusion.
    try:
        if LOCK_PATH.exists():
            owner = LOCK_PATH.read_text().split(" ", 1)[0]
            if owner == str(os.getpid()):
                LOCK_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
def main():
    global C, ARGS, _RESOURCES, _APPT_TYPES, _CREATED
    _CREATED = 0
    ap = argparse.ArgumentParser(description="Sync custom-cart bookings to Odoo Appointments")
    ap.add_argument("--commit", action="store_true",
                    help="actually create events + send email (default is dry-run)")
    ap.add_argument("--no-email", action="store_true",
                    help="create events but do NOT send template 37 (safe testing)")
    ap.add_argument("--with-sms", action="store_true",
                    help="also schedule the SMS reminder (alarm 8) when the booker has a mobile")
    ap.add_argument("--include-past", action="store_true",
                    help="create events for past-dated bookings (event only, no email/sms)")
    ap.add_argument("--allow-overlap", action="store_true",
                    help="create even if the slot overlaps an existing event on the same resource")
    ap.add_argument("--only-order", default=None,
                    help="restrict to one order (name like S00088 or numeric id)")
    ap.add_argument("--since", default=None,
                    help="only orders with date_order >= YYYY-MM-DD")
    ap.add_argument("--max", type=int, default=25,
                    help="cap (would-)creations this run so a naive --commit self-throttles under "
                         "the SaaS daily email cap; re-run to continue. Pass --max 0 for unlimited.")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()
    ARGS.dry_run = not ARGS.commit   # dry-run is the DEFAULT

    mode = "LIVE" if ARGS.commit else "DRY-RUN"
    log(f"=== SD booking sync [{mode}] {_now()} NZ ===")

    # Single-instance guard (only a real write run needs it; a dry-run makes no changes).
    if ARGS.commit and not acquire_lock():
        log(f"    another sync run holds {LOCK_PATH.name} -- exiting (not an error).")
        return
    try:
        _run(mode)
    finally:
        if ARGS.commit:
            release_lock()


def _run(mode):
    global C, _RESOURCES, _APPT_TYPES
    C = OdooClient()
    log(f"    connected uid={C.uid} db={C.db}  confirmed-states={CONFIRMED_STATES} "
        f"(sent needs a payment tx)  email={'OFF' if ARGS.no_email else 'ON'}  "
        f"sms={'ON' if ARGS.with_sms else 'OFF'}")

    _RESOURCES = {r["id"]: r["name"] for r in
                  C.call("appointment.resource", "search_read", [], fields=["id", "name"])}
    _APPT_TYPES = {t["id"] for t in
                   C.call("appointment.type", "search_read", [], fields=["id"])}
    log(f"    resources: {_RESOURCES}")

    # Detailer-contact email preflight: an emailless detailer contact gets reaped from the
    # attendee list ~80s after each event write, so the colour vanishes and every run churns
    # a futile re-attach. Under --commit this is a HARD STOP (re-attaching is pointless until
    # the email exists); a dry-run only warns so it can still preview. Rule 7: no silent skip.
    _missing = []
    for _rid, _pid in RESOURCE_PARTNER.items():
        _p = C.call("res.partner", "read", [_pid], fields=["name", "email"])
        _em = (_p[0].get("email") or "").strip() if _p else ""
        if _em:
            log(f"    detailer contact res{_rid} -> {_p[0]['name']} <{_em}> (colour OK)")
        else:
            _missing.append((_rid, _pid))
            log(f"    *** WARNING: detailer contact res{_rid} (partner {_pid}) has NO EMAIL "
                f"-> its calendar colour will be reaped ~80s after each sync. Set an email.")
    if _missing and ARGS.commit:
        log(f"    ABORT: {len(_missing)} detailer contact(s) have no email; a --commit run "
            f"would churn futile re-attaches. Set the email(s) and re-run. (partners "
            f"{[p for _, p in _missing]})")
        sys.exit(2)

    # Timestamped CSV log
    logdir = Path(__file__).resolve().parent / "logs"
    logdir.mkdir(exist_ok=True)
    logpath = logdir / f"sync_bookings_{datetime.now(NZ).strftime('%Y%m%d-%H%M%S')}.csv"

    records = discover_bookings()
    log(f"    discovered {len(records)} SDBK1 line(s)")

    counts = {"created": 0, "would-create": 0, "updated": 0, "would-update": 0,
              "skip": 0, "conflict": 0, "error": 0, "ineligible": 0, "deferred": 0}
    # NB: the --max cap is enforced INSIDE process() (right before a create), so already-
    # synced lines still self-heal the colour + refresh status regardless of the cap.
    with open(logpath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "order", "line_id", "action", "event_id", "detail"])
        for rec in records:
            # is_eligible() now issues an RPC (payment.transaction) for 'sent' orders, so it
            # lives INSIDE the try -> one bad/permission-denied order is logged + skipped, it
            # never aborts the whole run.
            try:
                ok, why = is_eligible(rec["order"])
                if not ok:
                    vlog(f"    {rec['order']['name']} L{rec['line']['id']} ineligible: {why}")
                    writer.writerow([_now(), rec["order"]["name"], rec["line"]["id"],
                                     "ineligible", "", why])
                    counts["ineligible"] += 1
                    continue
                outcome = process(rec, writer)
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as e:
                counts["error"] += 1
                log(f"    ERROR {rec['order']['name']} L{rec['line']['id']}: {type(e).__name__}: {e}")
                writer.writerow([_now(), rec["order"]["name"], rec["line"]["id"],
                                 "error", "", f"{type(e).__name__}: {e}"])

        # CRM: one opportunity per booked order (stage by payment state). Runs every
        # pass -> sent->sale transitions auto-promote Booked (Unpaid) -> Booked.
        try:
            crm = ensure_crm_opps(records, writer)
            counts.update(crm)
        except Exception as e:  # noqa: BLE001
            log(f"    CRM step failed (non-fatal): {type(e).__name__}: {e}")

        # Watchdog: eligible orders whose Booking value is blank (lost client-side). Never
        # aborts the run -- alerting is best-effort on top of the main sync.
        try:
            counts["blank-alert"] = check_blank_bookings(writer)
        except Exception as e:  # noqa: BLE001
            log(f"    blank-booking watchdog failed (non-fatal): {type(e).__name__}: {e}")

    log(f"    CRM: " + (", ".join(f"{k.replace('crm-', '')}={v}" for k, v in sorted(counts.items())
                                  if k.startswith("crm-")) or "nothing to do"))
    log(f"    blank-booking alerts: {counts.get('blank-alert', 0)}")
    log(f"=== done [{mode}] created={counts['created']} would-create={counts['would-create']} "
        f"updated={counts['updated']} would-update={counts['would-update']} "
        f"deferred={counts['deferred']} skip={counts['skip']} conflict={counts['conflict']} "
        f"ineligible={counts['ineligible']} error={counts['error']} ===")
    if counts["deferred"]:
        log(f"    NOTE: {counts['deferred']} line(s) deferred by --max {ARGS.max}; re-run to continue.")
    log(f"    log: {logpath}")
    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
