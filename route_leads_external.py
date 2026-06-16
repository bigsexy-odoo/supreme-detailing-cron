"""
EXTERNAL port of Odoo server action #715 "SD - Route web leads & orders".
Runs OUTSIDE Odoo (GitHub Actions cron) via XML-RPC so the code carries no
billable LoC inside Odoo. Behaviour is a faithful port of
`server_action_code_sd.py` (3 jobs), with debug output kept in (never strip).

Jobs:
  1. Web-form leads (crm.lead, no contact yet, "Suburb:" in description)
        matched suburb -> Opportunity in North(4)/Central(5)
        out-of-area    -> Lead (triage) in Sales(1), tag "Review area"
        + newsletter opt-in -> subscribe to Newsletter list
  2. Completed web orders (sale.order, confirmed, no opportunity yet)
        route order.team_id by suburb + create/link an Opportunity
  3. Appointment bookings (crm.lead w/ calendar event, untagged "Booking")
        route by booked resource (Alex->North, Kade->Central) else Suburb

Usage:
  python route_leads_external.py [--dry-run] [--verbose]
Creds: odoo_client.cfg() reads env vars first (GitHub secrets) then .env.
"""

import argparse
import re
import sys

from odoo_client import OdooClient, cfg
from chat_poster import lead_to_message, post_to_chat

# ----- CONFIG (Supreme Detailing) - identical to the in-Odoo action -----
REGION_STATE = 517      # res.country.state Auckland (AUK)
COUNTRY = 170           # res.country New Zealand
TEAM_NORTH = 4
TEAM_CENTRAL = 5
TEAM_TRIAGE = 1         # Sales (out-of-area leads)
NEWSLETTER_LIST = 1     # mailing.list "Newsletter"
NORTH = {'milford', 'takapuna', 'castor bay', 'mairangi bay', 'murrays bay',
         'forest hill', 'campbells bay'}
CENTRAL = {'onehunga', 'royal oak', 'mount eden', 'mount roskill', 'epsom'}
TEAM_NAME = {TEAM_NORTH: 'North', TEAM_CENTRAL: 'Central', TEAM_TRIAGE: 'Triage'}
# Job 4 replaces server action #562: website-enquiry contacts -> Chat alert.
ENQUIRY_TAGS = {1: 'Cart Save', 2: 'Website Enquiry', 3: 'Subscription Interest'}
ENQUIRY_PARAM = 'sd.enquiry.last_partner'   # high-water-mark of alerted partner id
# ------------------------------------------------------------------------

ARGS = None
C = None
CHAT_HOOK = None


def log(msg):
    print(msg, flush=True)


def vlog(msg):
    if ARGS and ARGS.verbose:
        print("  . " + msg, flush=True)


# ---- thin RPC helpers (honour OdooClient arg conventions) ----
def search(model, domain, **kw):
    return C.call(model, "search", domain, **kw)


def search_read(model, domain, fields, **kw):
    return C.call(model, "search_read", domain, fields=fields, **kw)


def read(model, ids, fields):
    if not ids:
        return []
    return C.call(model, "read", ids, fields=fields)


def create(model, vals):
    res = C.call(model, "create", vals)
    return res[0] if isinstance(res, (list, tuple)) else res


def write(model, ids, vals):
    if not isinstance(ids, (list, tuple)):
        ids = [ids]
    if ARGS and ARGS.dry_run:
        vlog(f"DRY write {model} {ids} <- {vals}")
        return True
    return C.call(model, "write", ids, vals)


def do_create(model, vals):
    if ARGS and ARGS.dry_run:
        vlog(f"DRY create {model} <- {vals}")
        return -1  # sentinel id in dry-run
    return create(model, vals)


def get_param(key, default="0"):
    return C.call("ir.config_parameter", "get_param", key, default)


def set_param(key, value):
    if ARGS and ARGS.dry_run:
        vlog(f"DRY set_param {key}={value}")
        return True
    return C.call("ir.config_parameter", "set_param", key, value)


def chat_alert(d):
    """Best-effort Google Chat card. Never breaks routing; no-op without a hook."""
    if not CHAT_HOOK:
        return
    label = d.get("contact_name") or d.get("name") or d.get("email_from") or "?"
    if ARGS and ARGS.dry_run:
        vlog(f"DRY chat alert: {label}")
        return
    try:
        ok = post_to_chat(lead_to_message(d, C.url), CHAT_HOOK)
        vlog(f"chat posted={ok} ({label})")
    except Exception as e:  # noqa: BLE001 - alerts must never fail the job
        log(f"  [chat] skipped ({label}): {e}")


# ---- parsing/routing (faithful, but free to use re externally) ----
def norm(s):
    return ' '.join((s or '').lower().split())


def region(suburb):
    s = norm(suburb)
    if s in NORTH:
        return 'north'
    if s in CENTRAL:
        return 'central'
    return None


def route(reg):
    if reg == 'north':
        return TEAM_NORTH, 'opportunity', ['Web form', 'North']
    if reg == 'central':
        return TEAM_CENTRAL, 'opportunity', ['Web form', 'Central']
    return TEAM_TRIAGE, 'lead', ['Web form', 'Review area']


def strip_tags(s):
    return re.sub(r'<[^>]*>', '', s or '')


_TAG_CACHE = {}


def goc(name):
    """get-or-create crm.tag by name (cached per run)."""
    if name in _TAG_CACHE:
        return _TAG_CACHE[name]
    found = search("crm.tag", [["name", "=", name]], limit=1)
    tid = found[0] if found else do_create("crm.tag", {"name": name})
    _TAG_CACHE[name] = tid
    return tid


def parse_blob(html):
    raw = re.sub(r'<br\s*/?>|</p>|</div>', '\n', html or '')
    blob = {}
    for line in raw.split('\n'):
        line = strip_tags(line).strip()
        if ': ' in line:
            k, v = line.split(': ', 1)
            k, v = k.strip().lower(), v.strip()
            if k and v:
                blob[k] = v
    return blob


def parse_appt(html):
    raw = re.sub(r'<br\s*/?>|</span>|</p>|</div>', '\n', html or '')
    lines = [strip_tags(x).strip() for x in raw.split('\n')]
    lines = [x for x in lines if x]
    blob = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        k = v = None
        if ' - ' in line:
            k, v = line.split(' - ', 1)
        elif line.endswith(' -'):
            k, v = line[:-2], ''
        if k is not None:
            k, v = k.strip().lower(), v.strip()
            nxt = lines[i + 1] if i + 1 < len(lines) else ''
            if not v and nxt and ' - ' not in nxt and not nxt.endswith(' -'):
                v = nxt.strip()
                i += 1
            blob[k] = v
        i += 1
    return blob


# ---------- JOB 1: web-form leads ----------
def job_leads():
    leads = search_read(
        "crm.lead",
        [["partner_id", "=", False], ["email_from", "!=", False]],
        ["id", "contact_name", "email_from", "phone", "description"],
    )
    vlog(f"job1: {len(leads)} contactless leads to inspect")
    n = 0
    for lead in leads:
        blob = parse_blob(lead.get("description"))
        suburb = blob.get("suburb")
        if not suburb:
            continue  # not one of our form leads
        reg = region(suburb)
        team_id, ltype, tagnames = route(reg)
        street = blob.get("address") or blob.get("street address") or blob.get("street")
        geo = ({"city": suburb, "state_id": REGION_STATE, "country_id": COUNTRY}
               if reg else {})
        if street:
            geo["street"] = street

        person = ((lead.get("contact_name") or "").strip()
                  or (lead.get("email_from") or "Contact").split("@")[0])
        pvals = {"name": person, "email": lead["email_from"], "phone": lead.get("phone")}
        pvals.update(geo)
        existing = search("res.partner", [["email", "=", lead["email_from"]]], limit=1)
        if existing:
            partner_id = existing[0]
            write("res.partner", partner_id,
                  {k: v for k, v in pvals.items() if v and k != "name"})
        else:
            partner_id = do_create("res.partner", pvals)

        tag_ids = [goc(x) for x in tagnames]
        lv = {"type": ltype, "team_id": team_id, "partner_id": partner_id,
              "contact_name": person, "tag_ids": [(6, 0, tag_ids)]}
        lv.update(geo)
        write("crm.lead", lead["id"], lv)

        if blob.get("newsletter"):
            mc = search("mailing.contact", [["email", "=", lead["email_from"]]], limit=1)
            if mc:
                write("mailing.contact", mc[0], {"list_ids": [(4, NEWSLETTER_LIST)]})
            else:
                do_create("mailing.contact",
                          {"name": person, "email": lead["email_from"],
                           "list_ids": [(4, NEWSLETTER_LIST)]})
            log(f"sd-routing: subscribed {lead['email_from']} to Newsletter")

        chat_alert({"id": lead["id"], "_model": "crm.lead", "_kind": f"{tagnames[-1]} web lead",
                    "contact_name": person, "email_from": lead["email_from"],
                    "phone": lead.get("phone"), "city": suburb if reg else None,
                    "team_id": [team_id, TEAM_NAME.get(team_id, "—")]})
        log(f"sd-routing: lead {lead['id']} -> {ltype} ({reg or 'other'}) team {team_id}")
        n += 1
    return n


# ---------- JOB 2: completed web orders ----------
def job_orders():
    orders = search_read(
        "sale.order",
        [["website_id", "!=", False], ["state", "in", ["sale", "done"]],
         ["opportunity_id", "=", False]],
        ["id", "name", "partner_id", "partner_shipping_id", "amount_total"],
    )
    vlog(f"job2: {len(orders)} unlinked web orders")
    n = 0
    for o in orders:
        ship = o.get("partner_shipping_id") or o.get("partner_id")
        cust_id = ship[0] if ship else None
        cust = read("res.partner", [cust_id], ["city", "street", "name", "email", "phone"])[0] if cust_id else {}
        suburb = cust.get("city")
        reg = region(suburb)
        team_id, _ltype, tagnames = route(reg)
        write("sale.order", o["id"], {"team_id": team_id})

        pid = o["partner_id"][0] if o.get("partner_id") else False
        opp = search("crm.lead",
                     [["partner_id", "=", pid], ["team_id", "=", team_id],
                      ["type", "=", "opportunity"], ["active", "=", True]], limit=1)
        if opp:
            opp_id = opp[0]
        else:
            tag_ids = [goc(x) for x in (tagnames + ["Online order"])]
            opp_id = do_create("crm.lead", {
                "name": f"Order {o['name']} - {(o['partner_id'][1] if o.get('partner_id') else '')}",
                "type": "opportunity", "team_id": team_id, "partner_id": pid,
                "contact_name": o["partner_id"][1] if o.get("partner_id") else False,
                "email_from": cust.get("email"), "phone": cust.get("phone"),
                "street": cust.get("street"), "city": suburb or False,
                "state_id": REGION_STATE, "country_id": COUNTRY,
                "expected_revenue": o.get("amount_total"),
                "tag_ids": [(6, 0, tag_ids)],
            })
        write("sale.order", o["id"], {"opportunity_id": opp_id})
        chat_alert({"id": opp_id, "_model": "crm.lead", "_kind": f"web order {o['name']}",
                    "contact_name": o["partner_id"][1] if o.get("partner_id") else None,
                    "email_from": cust.get("email"), "phone": cust.get("phone"),
                    "city": suburb, "team_id": [team_id, TEAM_NAME.get(team_id, "—")],
                    "expected_revenue": o.get("amount_total")})
        log(f"sd-routing: order {o['name']} -> team {team_id} opp {opp_id} ({reg or 'other'})")
        n += 1
    return n


# ---------- JOB 3: appointment bookings ----------
def job_bookings():
    booking_tag = goc("Booking")
    opps = search_read(
        "crm.lead",
        [["calendar_event_ids", "!=", False], ["tag_ids", "not in", [booking_tag]]],
        ["id", "description", "partner_id", "calendar_event_ids"],
    )
    vlog(f"job3: {len(opps)} untagged booking opps")
    n = 0
    for opp in opps:
        ev_ids = opp.get("calendar_event_ids") or []
        if not ev_ids:
            continue
        ev = read("calendar.event", [ev_ids[0]], ["description", "appointment_resource_ids"])[0]
        res_ids = ev.get("appointment_resource_ids") or []
        names = [r.get("name") or "" for r in
                 (read("appointment.resource", res_ids, ["name"]) if res_ids else [])]
        reg = None
        for nm in names:
            low = nm.lower()
            if "north" in low:
                reg = "north"
            elif "central" in low:
                reg = "central"
        blob = parse_appt(ev.get("description") or opp.get("description"))
        suburb = street = None
        for k, v in blob.items():
            if "suburb" in k:
                suburb = v
            elif "address" in k:
                street = v
        if reg is None:
            reg = region(suburb)
        if reg == "north":
            team_id, regtag = TEAM_NORTH, "North"
        elif reg == "central":
            team_id, regtag = TEAM_CENTRAL, "Central"
        else:
            team_id, regtag = TEAM_TRIAGE, "Review area"

        pgeo = {}
        if street:
            pgeo["street"] = street
        if suburb and reg:
            pgeo.update({"city": suburb, "state_id": REGION_STATE, "country_id": COUNTRY})
        if opp.get("partner_id") and pgeo:
            write("res.partner", opp["partner_id"][0], pgeo)

        tag_ids = [booking_tag, goc(regtag)]
        lv = {"team_id": team_id, "tag_ids": [(4, t) for t in tag_ids]}
        lv.update(pgeo)
        write("crm.lead", opp["id"], lv)
        pname = (read("res.partner", [opp["partner_id"][0]], ["name"])[0].get("name")
                 if opp.get("partner_id") else None)
        chat_alert({"id": opp["id"], "_model": "crm.lead", "_kind": "booking",
                    "contact_name": pname, "city": suburb,
                    "team_id": [team_id, TEAM_NAME.get(team_id, "—")]})
        log(f"sd-routing: booking opp {opp['id']} -> team {team_id} ({reg or 'other'}) street={street}")
        n += 1
    return n


# ---------- JOB 4: website-enquiry contacts -> Chat (replaces action #562) ----------
def job_enquiries():
    if not CHAT_HOOK:
        vlog("job4: no GCHAT_WEBHOOK_URL — skipping enquiry alerts")
        return 0
    tags = list(ENQUIRY_TAGS)
    raw = get_param(ENQUIRY_PARAM, "")
    if raw in ("", False, None):
        # First ever run: baseline to the current max so we DON'T flood the space
        # with every historical enquiry contact. Only future ones alert.
        newest = search("res.partner", [["category_id", "in", tags]],
                        order="id desc", limit=1)
        base = newest[0] if newest else 0
        set_param(ENQUIRY_PARAM, str(base))
        log(f"sd-routing: job4 baselined {ENQUIRY_PARAM}={base} (no historical alerts)")
        return 0
    last = int(raw or "0")
    parts = search_read(
        "res.partner",
        [["category_id", "in", tags], ["id", ">", last]],
        ["id", "name", "email", "phone", "city", "category_id"],
        order="id asc",
    )
    vlog(f"job4: {len(parts)} new enquiry contacts (id > {last})")
    maxid = last
    n = 0
    for p in parts:
        if p["id"] > maxid:
            maxid = p["id"]
        cats = [t for t in (p.get("category_id") or []) if t in ENQUIRY_TAGS]
        label = " / ".join(ENQUIRY_TAGS[t] for t in cats) or "Website enquiry"
        chat_alert({"id": p["id"], "_model": "res.partner", "_kind": label,
                    "contact_name": p.get("name"), "email_from": p.get("email"),
                    "phone": p.get("phone"), "city": p.get("city")})
        log(f"sd-routing: enquiry {p['id']} ({label}) -> Chat")
        n += 1
    if maxid > last:
        set_param(ENQUIRY_PARAM, str(maxid))
        vlog(f"advanced {ENQUIRY_PARAM} {last} -> {maxid}")
    return n


def main():
    global ARGS, C, CHAT_HOOK
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read + log, write nothing")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()

    C = OdooClient()
    try:
        CHAT_HOOK = cfg("GCHAT_WEBHOOK_URL")
    except Exception:  # noqa: BLE001 - Chat is optional
        CHAT_HOOK = None
    mode = "DRY-RUN" if ARGS.dry_run else "LIVE"
    log(f"sd-routing [{mode}] connected uid={C.uid} db={C.db} chat={'on' if CHAT_HOOK else 'off'}")
    try:
        a = job_leads()
        b = job_orders()
        d = job_bookings()
        e = job_enquiries()
    except Exception as exc:  # noqa: BLE001 - surface, never swallow
        log(f"sd-routing ERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)
    log(f"sd-routing done [{mode}] leads={a} orders={b} bookings={d} enquiries={e} errors=0")


if __name__ == "__main__":
    main()
