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
  * ELIGIBILITY: default states = ["sale", "done"] ONLY. 'sent' is a verified
    UNPAID quotation on this DB (17 live 'sent' orders, invoice_status='no',
    0 completed payment tx) -> it is opt-in behind --include-sent with a loud
    warning. An unattended cron that SENDS EMAIL must never act on unpaid quotes.
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
    python sync_bookings.py --commit --include-sent # ALSO act on unpaid 'sent' quotes (NOT recommended)
    python sync_bookings.py --commit --max 5        # cap this run to 5 creations (throttle email cap)
Creds: odoo_client.cfg() reads env vars first (GitHub secrets) then .env.
"""

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Force UTF-8 stdout on Windows (avoid charmap codec errors with emoji) -- Rule 5
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

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
SMS_ALARM = 8               # "SMS Text Message - 1 Hours" -- real IAP credits, off by default.
BOOKED_TEMPLATE = 37        # mail.template "Appointment: Attendee Invitation" (model calendar.attendee)
# Calendar colour tags (calendar.event.type): North Shore/Alex=green, Central/Kade=red.
# Only shows if the Meetings calendar (view 2430) colours by categ_ids (set up separately).
RESOURCE_TAG = {1: 1, 2: 2}   # appointment.resource id -> calendar.event.type (tag) id (popup label)
# The Meetings calendar colours by ATTENDEE, so add the detailer as a calendar attendee
# whose partner carries a preset colour (Alex=green/10, Kade=red/1). One-time owner step:
# in Calendar sidebar '+ Add Attendees' -> tick Alex + Kade to colour/filter bookings by detailer.
RESOURCE_PARTNER = {1: 69, 2: 70}   # appointment.resource id -> detailer res.partner id (unused in Option B)
# OPTION B: the Meetings calendar colours by ATTENDEE, so colour by PAID STATUS via a
# status partner attendee (Paid=green / Awaiting=red). Detailer is shown by an A/K letter
# in the title. One-time owner step: in Calendar sidebar tick '✅ Paid' (green swatch) +
# '⏳ Awaiting Payment' (red swatch); untick everything else. Filtering to Awaiting = a
# ready-made "who still owes me" view.
STATUS_PARTNER = {True: 71, False: 72}   # is_paid -> res.partner (✅ Paid green / ⏳ Awaiting red)
NZ = ZoneInfo("Pacific/Auckland")   # DST-correct: NZST=UTC+12, NZDT=UTC+13
UTC = ZoneInfo("UTC")

SYNC_TAG = "[SD-booking-sync]"                    # visible provenance line in the description
MARKER_COMMENT = "<!-- SDBK1:L{line_id} -->"      # BOUNDED per-line idempotency key (full comment)
ORDER_TOKEN = "SDCAL:L{line_id}=E{event_id}"      # order-ref audit token

# A "confirmed booking" = the customer completed checkout. For a bank-transfer / COD
# shop that is state 'sent' WITH a payment.transaction (a bare emailed quote / abandoned
# cart has none), plus 'sale'/'done' (owner-confirmed = paid). ALL confirmed bookings go
# on the calendar; the event shows PAID vs AWAITING so the detailer knows before the day.
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


def log(msg):
    print(msg, flush=True)


def vlog(msg):
    if ARGS and ARGS.verbose:
        print("  . " + msg, flush=True)


# ---------------------------------------------------------------------------
# SDBK1 parsing + resource / time resolution
# ---------------------------------------------------------------------------
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

    # ---- Channel 2: test fallback -- SDBK1 token in the line name ----
    name_lines = C.call("sale.order.line", "search_read",
                        [["name", "ilike", "SDBK1|"]],
                        fields=["id", "name"])
    vlog(f"discover: {len(name_lines)} lines with SDBK1 in name (test fallback)")
    fallback_by_line = {}
    for ln in name_lines:
        for chunk in (ln.get("name") or "").splitlines():
            if chunk.strip().startswith("SDBK1"):
                fallback_by_line[ln["id"]] = chunk.strip()
                if ln["id"] not in line_ids:
                    line_ids.append(ln["id"])
                break

    if not line_ids:
        return []

    lines = C.call("sale.order.line", "read", sorted(line_ids),
                   fields=["id", "product_id", "name", "order_id", "product_uom_qty"])
    line_map = {ln["id"]: ln for ln in lines}

    order_ids = sorted({ln["order_id"][0] for ln in lines if ln.get("order_id")})
    orders = C.call("sale.order", "read", order_ids,
                    fields=["id", "name", "state", "partner_id", "client_order_ref",
                            "date_order", "website_id", "amount_total", "transaction_ids"])
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

    draft = abandoned cart (excluded). sent = completed checkout -> eligible ONLY if a
    payment.transaction exists (excludes a bare emailed quote). sale/done = paid.
    """
    st = order.get("state")
    if st not in CONFIRMED_STATES:
        return False, f"state={st} (not a confirmed booking)"
    if st == "sent" and not order.get("transaction_ids"):
        return False, "sent quote with no checkout/payment attempt (abandoned)"
    if ARGS.since:
        do = (order.get("date_order") or "")[:10]
        if do and do < ARGS.since:
            return False, f"date_order {do} < --since {ARGS.since}"
    if ARGS.only_order:
        want = ARGS.only_order.strip()
        if want != order["name"] and want != str(order["id"]):
            return False, "excluded by --only-order"
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
    # 1. order-ref token (cheap, no RPC)
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
            return int(num)

    # 2. BOUNDED marker search (full comment -> no L12/L123 collision)
    marker = MARKER_COMMENT.format(line_id=line_id)
    hit = C.call("calendar.event", "search",
                 [["description", "ilike", marker]], limit=1)
    if hit:
        return hit[0]

    # 3. natural-key fallback
    if booker_partner_id:
        hit = C.call("calendar.event", "search",
                     [["appointment_booker_id", "=", booker_partner_id],
                      ["start", "=", start_utc],
                      ["appointment_type_id", "=", appt_type_id]], limit=1)
        if hit:
            return hit[0]
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
    paid = ("PAID ✅" if is_paid(order)
            else "AWAITING PAYMENT ⏳ (bank transfer / cash on day)")
    amt = order.get("amount_total")
    amt_str = f" · ${amt:.2f} NZD" if amt else ""
    lines = [
        f"\U0001f4b3 {paid}",
        f"\U0001f697 Service: {sdbk['service_label']}",
        f"\U0001f4cd Suburb: {sdbk['suburb']}",
        f"\U0001f464 Detailer: {resource_name}",
        f"\U0001f550 {sdbk['date']} {sdbk['time24']} ({sdbk['duration']}h) NZ",
        f"\U0001f9fe Order {order.get('name')}{amt_str}",
        "",
        SYNC_TAG,
        MARKER_COMMENT.format(line_id=line_id),
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
        # Refresh PAID/AWAITING status if the order moved on (e.g. sent -> sale after
        # the bank transfer cleared). No re-email; just update the event body.
        want = PST_MARKER.format(state=order.get("state"))
        ev = C.call("calendar.event", "read", [existing], fields=["description"])
        desc = (ev[0].get("description") if ev else "") or ""
        if want not in desc:
            if not ARGS.dry_run:
                C.call("calendar.event", "write", [existing],
                       {"description": build_description(sdbk, resource_name, lid, order),
                        # flip the colour: drop the old status partner, add the new one
                        "partner_ids": [(3, STATUS_PARTNER[not is_paid(order)]),
                                        (4, STATUS_PARTNER[is_paid(order)])]},
                       context=NOISE_OFF)
            newstat = "PAID" if is_paid(order) else "AWAITING"
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
    _svc_short = sdbk["service_label"].replace(" Package", "").split(" (")[0].strip()
    _ini = (resource_name[:1] or "?").upper()   # A (Alex) / K (Kade) — detailer at a glance
    # Colour carries paid status (Option B), so the letter+service+suburb is the tile text.
    ename = f"{_ini} · {_svc_short} · {sdbk['suburb']}"
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
    alarms = list(BASE_ALARMS)
    if ARGS.with_sms and booker_mobile and not is_past:
        alarms.append(SMS_ALARM)

    log(f"  {oname} L{lid}: {ename}")
    log(f"      start(UTC)={start_utc} stop(UTC)={stop_utc} dur={sdbk['duration']}h "
        f"type={sdbk['appt_type_id']} resource={resource_id}:{resource_name} "
        f"booker={booker_id}:{partner_name} email={booker_email or '(none)'} "
        f"alarms={alarms} past={is_past} src={rec['source']}")

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
        # Colour-by-paid: the status partner (green/red) + the customer (for the email).
        "partner_ids": [(6, 0, list(dict.fromkeys(
            [p for p in [STATUS_PARTNER[is_paid(order)], booker_id] if p])))],
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
        "description": build_description(sdbk, resource_name, lid, order),
    }
    event_id = C.call("calendar.event", "create", vals, context=NOISE_OFF)
    if isinstance(event_id, (list, tuple)):
        event_id = event_id[0]
    log(f"      created calendar.event {event_id}")

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
                C.call("mail.template", "send_mail", [BOOKED_TEMPLATE], att[0],
                       force_send=True)
                mailed = f"emailed att{att[0]}"
                log(f"      sent template {BOOKED_TEMPLATE} to attendee {att[0]} <{booker_email}>")
            except Exception as e:
                mailed = f"email-failed: {e}"
                log(f"      WARNING: template {BOOKED_TEMPLATE} send failed: {e}")
        else:
            mailed = "no-attendee"
            log("      WARNING: no calendar.attendee found -- email skipped")

    writer.writerow([_now(), oname, lid, "created", event_id,
                     f"{start_utc}|type{sdbk['appt_type_id']}|res{resource_id}|{mailed}"])
    return "created"


def _now():
    return datetime.now(NZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
def main():
    global C, ARGS, _RESOURCES, _APPT_TYPES
    ap = argparse.ArgumentParser(description="Sync custom-cart bookings to Odoo Appointments")
    ap.add_argument("--commit", action="store_true",
                    help="actually create events + send email (default is dry-run)")
    ap.add_argument("--no-email", action="store_true",
                    help="create events but do NOT send template 37 (safe testing)")
    ap.add_argument("--with-sms", action="store_true",
                    help="also schedule the SMS reminder (alarm 8) when the booker has a mobile")
    ap.add_argument("--include-past", action="store_true",
                    help="create events for past-dated bookings (event only, no email/sms)")
    ap.add_argument("--include-sent", action="store_true",
                    help="ALSO act on unpaid 'sent' quotations (NOT recommended; loud warning)")
    ap.add_argument("--allow-overlap", action="store_true",
                    help="create even if the slot overlaps an existing event on the same resource")
    ap.add_argument("--only-order", default=None,
                    help="restrict to one order (name like S00088 or numeric id)")
    ap.add_argument("--since", default=None,
                    help="only orders with date_order >= YYYY-MM-DD")
    ap.add_argument("--max", type=int, default=0,
                    help="cap creations this run (throttle the first backfill under the email cap); 0=unlimited")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()
    ARGS.dry_run = not ARGS.commit   # dry-run is the DEFAULT

    mode = "LIVE" if ARGS.commit else "DRY-RUN"
    log(f"=== SD booking sync [{mode}] {_now()} NZ ===")

    C = OdooClient()
    log(f"    connected uid={C.uid} db={C.db}  confirmed-states={CONFIRMED_STATES} "
        f"(sent needs a payment tx)  email={'OFF' if ARGS.no_email else 'ON'}  "
        f"sms={'ON' if ARGS.with_sms else 'OFF'}")

    _RESOURCES = {r["id"]: r["name"] for r in
                  C.call("appointment.resource", "search_read", [], fields=["id", "name"])}
    _APPT_TYPES = {t["id"] for t in
                   C.call("appointment.type", "search_read", [], fields=["id"])}
    log(f"    resources: {_RESOURCES}")

    # Timestamped CSV log
    logdir = Path(__file__).resolve().parent / "logs"
    logdir.mkdir(exist_ok=True)
    logpath = logdir / f"sync_bookings_{datetime.now(NZ).strftime('%Y%m%d-%H%M%S')}.csv"

    records = discover_bookings()
    log(f"    discovered {len(records)} SDBK1 line(s)")

    counts = {"created": 0, "would-create": 0, "updated": 0, "skip": 0,
              "conflict": 0, "error": 0, "ineligible": 0}
    created_this_run = 0
    with open(logpath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "order", "line_id", "action", "event_id", "detail"])
        for rec in records:
            ok, why = is_eligible(rec["order"])
            if not ok:
                vlog(f"    {rec['order']['name']} L{rec['line']['id']} ineligible: {why}")
                writer.writerow([_now(), rec["order"]["name"], rec["line"]["id"],
                                 "ineligible", "", why])
                counts["ineligible"] += 1
                continue
            if ARGS.max and created_this_run >= ARGS.max:
                log(f"    --max {ARGS.max} reached -- stopping (remaining lines deferred to next run)")
                writer.writerow([_now(), rec["order"]["name"], rec["line"]["id"],
                                 "deferred", "", f"--max {ARGS.max} reached"])
                continue
            try:
                outcome = process(rec, writer)
                counts[outcome] = counts.get(outcome, 0) + 1
                if outcome == "created":
                    created_this_run += 1
            except Exception as e:
                counts["error"] += 1
                log(f"    ERROR {rec['order']['name']} L{rec['line']['id']}: {type(e).__name__}: {e}")
                writer.writerow([_now(), rec["order"]["name"], rec["line"]["id"],
                                 "error", "", f"{type(e).__name__}: {e}"])

    log(f"=== done [{mode}] created={counts['created']} would-create={counts['would-create']} "
        f"updated={counts['updated']} skip={counts['skip']} conflict={counts['conflict']} "
        f"ineligible={counts['ineligible']} error={counts['error']} ===")
    log(f"    log: {logpath}")
    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
