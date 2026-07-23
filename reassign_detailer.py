"""One-command detailer reassign for a booking. Updates EVERY place a detailer lives so the
calendar lane, the availability hold, the colour, the description, and the sync's source-of-truth
all agree — instead of the fiddly 3-place manual dance:

  1. calendar.event.appointment_resource_ids        (the LANE)
  2. the appointment booking-line appointment_resource_id  (the AVAILABILITY hold)
  3. the attendee contact  (the COLOUR — add the new detailer, strip the old)
  4. the event tile title letter + the "Detailer:" line in the description
  5. the SDBK1 capture on the sale-order line(s) — custom value + line name — so the 15-min
     sync never drifts the detailer back

Idempotent (safe to re-run). DRY-RUN by default; pass --commit to write.

  python reassign_detailer.py S00100 Kade            # dry-run (order → all its events)
  python reassign_detailer.py S00100 Kade --commit
  python reassign_detailer.py e40 Alex --commit      # a single event id

Long-term home: this lives in cloud-cron/ next to sync_bookings.py (shares odoo_client + the
Alex/Kade constants). With the hardened sync, routine swaps can also just be a gantt drag
(Appointments → Resources Bookings) — the sync then keeps colour/attendee in step; this script is
for a one-shot, fully-consistent swap (incl. the SDBK1 source + description) or scripted/bulk use.
"""
import sys, os, re, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo_client import OdooClient

# MUST match sync_bookings.py
RESOURCE_NAME = {1: "Alex (North Shore)", 2: "Kade (Central Auckland)"}
RESOURCE_PARTNER = {1: 69, 2: 70}   # resource -> detailer CONTACT (res.partner)
NOISE_OFF = {"no_mail_to_attendees": True, "mail_create_nolog": True,
             "mail_create_nosubscribe": True, "mail_notify_author": False, "tracking_disable": True}
# SDBK1|date|time|dur|apptType|RESOURCE|suburb|service  -> swap field 6 (the resource name)
SDBK1_RES = re.compile(r"(SDBK1\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|)[^|]*(\|)")


def resolve_resource(tok):
    t = tok.strip().lower()
    if t in ("1", "a", "alex", "alex (north shore)", "north", "north shore"):
        return 1
    if t in ("2", "k", "kade", "kade (central auckland)", "central", "central auckland"):
        return 2
    raise SystemExit(f"Unknown detailer {tok!r} — use Alex/Kade (or 1/2).")


def bl_model(c):
    """The model behind calendar.event.booking_line_ids (varies by Odoo build)."""
    for m in ("appointment.booking.line", "calendar.booking.line"):
        try:
            c.call(m, "fields_get", [], {"attributes": ["string"]})
            return m
        except Exception:
            continue
    return None


def resolve_targets(c, tgt):
    """Return (event_ids, order_line_ids). Accepts eNN, an order name (S00100), or a sale id."""
    tgt = tgt.strip()
    if re.fullmatch(r"e\d+", tgt, re.I):
        eid = int(tgt[1:])
        # find the order that owns this event (SDCAL:Lxx=Eyy token on client_order_ref)
        orders = c.call("sale.order", "search_read", [["client_order_ref", "ilike", f"=E{eid}"]],
                        fields=["order_line"])
        lines = orders[0]["order_line"] if orders else []
        return [eid], lines
    dom = [["id", "=", int(tgt)]] if tgt.isdigit() else [["name", "=", tgt]]
    orders = c.call("sale.order", "search_read", dom, fields=["id", "name", "client_order_ref", "order_line"])
    if not orders:
        raise SystemExit(f"No sale.order matching {tgt!r}.")
    o = orders[0]
    events = sorted({int(m) for m in re.findall(r"=E(\d+)", o.get("client_order_ref") or "")})
    return events, o["order_line"]


def main():
    ap = argparse.ArgumentParser(description="Reassign a booking's detailer consistently")
    ap.add_argument("target", help="order name (S00100), sale-order id, or eNN (calendar.event id)")
    ap.add_argument("detailer", help="Alex | Kade | 1 | 2")
    ap.add_argument("--commit", action="store_true", help="write (default is dry-run)")
    args = ap.parse_args()
    dry = not args.commit
    c = OdooClient()

    rid = resolve_resource(args.detailer)
    rname = RESOURCE_NAME[rid]
    new_pid = RESOURCE_PARTNER[rid]
    letter = rname[0]
    old_pids = [p for r, p in RESOURCE_PARTNER.items() if r != rid]
    events, line_ids = resolve_targets(c, args.target)
    BLM = bl_model(c)
    print(f"=== reassign {args.target} -> {rname} (resource {rid}, contact {new_pid}) "
          f"[{'DRY-RUN' if dry else 'COMMIT'}] ===")
    print(f"  events: {events or '(none)'}   order lines: {line_ids or '(none)'}   booking-line model: {BLM}")
    if not events:
        raise SystemExit("No calendar events resolved. Pass eNN directly, or check the order ref.")

    def W(model, ids, vals, **kw):
        if not dry:
            c.call(model, "write", ids if isinstance(ids, list) else [ids], vals, **kw)

    for eid in events:
        ev = c.call("calendar.event", "read", [eid],
                    fields=["name", "appointment_resource_ids", "booking_line_ids", "partner_ids", "description"])
        if not ev:
            print(f"\n  event {eid}: NOT FOUND — skip")
            continue
        ev = ev[0]
        print(f"\n  event {eid} '{ev['name']}'  resource={ev.get('appointment_resource_ids')}  attendees={ev['partner_ids']}")

        # 1+2) RESOURCE via the booking-line (the capacity carrier + availability hold).
        # Writing the event's appointment_resource_ids m2m DIRECTLY errors
        # "Missing required value ... capacity_reserved" (the same lesson sync_bookings' create
        # documents) — so update the booking line (carrying capacity) and the m2m follows.
        bls = ev.get("booking_line_ids", [])
        if bls and BLM:
            for bl in bls:
                cap = c.call(BLM, "read", [bl], fields=["capacity_reserved", "capacity_used"])
                cap = cap[0] if cap else {}
                W(BLM, bl, {"appointment_resource_id": rid,
                            "capacity_reserved": cap.get("capacity_reserved") or 1,
                            "capacity_used": cap.get("capacity_used") or 1})
                print(f"    [1] {BLM} {bl}.appointment_resource_id -> {rid} (+capacity){' (would)' if dry else ''}")
        elif (ev.get("appointment_resource_ids") or []) != [rid]:
            W("calendar.event", eid, {"appointment_resource_ids": [(6, 0, [rid])]}, context=NOISE_OFF)
            print(f"    [1] resource m2m -> [{rid}] (no booking line){' (would)' if dry else ''}")
        else:
            print("    [1] resource already correct")

        # 3) attendee / colour  (add new detailer, strip the other)
        drop = [p for p in old_pids if p in ev["partner_ids"]]
        cmds = ([(4, new_pid)] if new_pid not in ev["partner_ids"] else []) + [(3, p) for p in drop]
        if cmds:
            W("calendar.event", eid, {"partner_ids": cmds}, context=NOISE_OFF)
            print(f"    [3] attendees: +{new_pid} -{drop}{' (would)' if dry else ''}")
        else:
            print("    [3] attendee already correct")

        # 4) title letter + description "Detailer:" line
        newname = re.sub(r"^(\W*)?[AK](\s)", lambda m: (m.group(1) or "") + letter + m.group(2), ev["name"], count=1)
        if newname != ev["name"]:
            W("calendar.event", eid, {"name": newname}, context=NOISE_OFF)
            print(f"    [4] title '{ev['name']}' -> '{newname}'{' (would)' if dry else ''}")
        desc = ev.get("description") or ""
        newdesc = re.sub(r"(Detailer:\s*)([^<\n]+)", lambda m: m.group(1) + rname, desc, count=1)
        if newdesc != desc:
            W("calendar.event", eid, {"description": newdesc}, context=NOISE_OFF)
            print(f"    [4] description 'Detailer:' -> {rname}{' (would)' if dry else ''}")

    # 5) SDBK1 capture on the order line(s) so the sync won't drift it back
    for L in c.call("sale.order.line", "read", line_ids,
                    fields=["id", "name", "product_custom_attribute_value_ids"]) if line_ids else []:
        nm = L.get("name") or ""
        if "SDBK1|" in nm:
            newnm = SDBK1_RES.sub(lambda m: m.group(1) + rname + m.group(2), nm)
            if newnm != nm:
                W("sale.order.line", L["id"], {"name": newnm})
                print(f"\n  [5] SOL {L['id']} name SDBK1 detailer -> {rname}{' (would)' if dry else ''}")
        for cv in L.get("product_custom_attribute_value_ids", []):
            v = c.call("product.attribute.custom.value", "read", [cv], fields=["custom_value"])[0]
            cvv = v.get("custom_value") or ""
            if cvv.startswith("SDBK1"):
                newcv = SDBK1_RES.sub(lambda m: m.group(1) + rname + m.group(2), cvv)
                if newcv != cvv:
                    W("product.attribute.custom.value", cv, {"custom_value": newcv})
                    print(f"  [5] custom-val {cv} SDBK1 detailer -> {rname}{' (would)' if dry else ''}")

    print(f"\nDONE{' (dry-run — pass --commit)' if dry else ' (committed)'}")


if __name__ == "__main__":
    main()
