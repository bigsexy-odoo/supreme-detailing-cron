"""Reschedule a booking to a new date/time (and optionally a new detailer) — the front-end
gantt-drag write path. Moves the calendar.event start/stop (DST-correct NZ->UTC, duration
preserved), keeps the appointment.booking.line availability hold in step, rewrites the SDBK1
date/time on the order line(s) so the 15-min sync never drifts it back, and — if the drop
landed in the OTHER detailer's lane — reuses reassign_detailer's resource move too.

Fired by OdooAction.gs (the /schedule gantt drag) via repository_dispatch -> reschedule.yml,
or run by hand. DRY-RUN by default; pass --commit to write.

  python reschedule_booking.py e40 --start "2026-07-26 11:00"                     # dry-run
  python reschedule_booking.py e40 --start "2026-07-26 11:00" --commit
  python reschedule_booking.py e40 --start "2026-07-26 11:00" --detailer Alex --commit  # + lane change

Safety: past-date skip, overlap/double-book guard on the target resource (skips unless --force),
idempotent (re-running with the same start/detailer is a no-op).
"""
import sys, os, re, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo_client import OdooClient
import reassign_detailer as RA   # reuse constants + resolvers (don't duplicate the proven bits)

NZ = ZoneInfo("Pacific/Auckland")
UTC = ZoneInfo("UTC")
NOISE_OFF = RA.NOISE_OFF
FMT = "%Y-%m-%d %H:%M:%S"

# SDBK1|date|time|dur|apptType|resource|suburb|service  -> rewrite field 2 (date) + 3 (time)
SDBK1_DT = re.compile(r"(SDBK1\|)([^|]*)(\|)([^|]*)(\|)")


def nz_to_utc(nz_local):
    """'YYYY-MM-DD HH:MM' NZ local -> 'YYYY-MM-DD HH:MM:SS' UTC (DST-correct)."""
    dt = datetime.strptime(nz_local, "%Y-%m-%d %H:%M").replace(tzinfo=NZ)
    return dt.astimezone(UTC).strftime(FMT)


def utc_to_nz(utc_str):
    dt = datetime.strptime(utc_str, FMT).replace(tzinfo=UTC).astimezone(NZ)
    return dt


def sub_sdbk1_dt(s, date_str, time_str):
    return SDBK1_DT.sub(lambda m: m.group(1) + date_str + m.group(3) + time_str + m.group(5), s, count=1)


def main():
    ap = argparse.ArgumentParser(description="Reschedule a booking (time, and optionally detailer)")
    ap.add_argument("event", help="calendar.event id, e.g. e40 or 40")
    ap.add_argument("--start", required=True, help="new NZ-local start 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--detailer", default="", help="Alex | Kade | 1 | 2  (only if the lane changed)")
    ap.add_argument("--commit", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--force", action="store_true", help="reschedule even if it overlaps another job")
    args = ap.parse_args()
    dry = not args.commit
    c = OdooClient()

    m = re.fullmatch(r"e?(\d+)", args.event.strip(), re.I)
    if not m:
        raise SystemExit(f"bad event id {args.event!r}")
    eid = int(m.group(1))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", args.start.strip()):
        raise SystemExit(f"--start must be 'YYYY-MM-DD HH:MM', got {args.start!r}")

    ev = c.call("calendar.event", "read", [eid],
                fields=["name", "start", "stop", "duration", "appointment_resource_ids", "booking_line_ids"])
    if not ev:
        raise SystemExit(f"event {eid} not found")
    ev = ev[0]
    dur_h = ev.get("duration") or ((datetime.strptime(ev["stop"], FMT) - datetime.strptime(ev["start"], FMT)).total_seconds() / 3600)
    cur_rid = (ev.get("appointment_resource_ids") or [0])[0]

    new_start_utc = nz_to_utc(args.start.strip())
    new_start_dt = datetime.strptime(new_start_utc, FMT)
    new_stop_dt = new_start_dt + timedelta(hours=dur_h)
    new_stop_utc = new_stop_dt.strftime(FMT)

    # target detailer (default = unchanged)
    rid = RA.resolve_resource(args.detailer) if args.detailer.strip() else cur_rid
    lane_change = bool(args.detailer.strip()) and rid != cur_rid

    print(f"=== reschedule event {eid} '{ev['name']}'  [{'DRY-RUN' if dry else 'COMMIT'}] ===")
    print(f"  duration: {dur_h:g}h   current resource: {cur_rid} ({RA.RESOURCE_NAME.get(cur_rid,'?')})")
    print(f"  FROM (UTC) {ev['start']} -> {ev['stop']}")
    print(f"  TO   (NZ)  {args.start}   =>  (UTC) {new_start_utc} -> {new_stop_utc}")
    if lane_change:
        print(f"  + lane change: {cur_rid} -> {rid} ({RA.RESOURCE_NAME.get(rid)})")

    # idempotency: already there?
    if ev["start"] == new_start_utc and ev["stop"] == new_stop_utc and not lane_change:
        print("  already at this time/lane — nothing to do.")
        print("\nDONE (no change)")
        return

    # past-date guard (NZ now)
    if new_start_dt.replace(tzinfo=UTC) < datetime.now(UTC):
        print("  WARNING: new start is in the PAST.")
        if not args.force:
            raise SystemExit("  refusing to reschedule into the past (pass --force to override).")

    # overlap / double-book guard on the TARGET resource (exclude this event)
    clash = c.call("calendar.event", "search_read",
                   [["id", "!=", eid], ["appointment_resource_ids", "in", [rid]],
                    ["start", "<", new_stop_utc], ["stop", ">", new_start_utc]],
                   fields=["id", "name", "start", "stop"])
    if clash:
        print(f"  CONFLICT on resource {rid}: {[(x['id'], x['name'], x['start']) for x in clash]}")
        if not args.force:
            raise SystemExit("  refusing to double-book (pass --force to override).")

    def W(model, ids, vals, **kw):
        if not dry:
            c.call(model, "write", ids if isinstance(ids, list) else [ids], vals, **kw)

    # 1) move the event time (booking-line event_start/event_stop are related -> they follow)
    W("calendar.event", eid, {"start": new_start_utc, "stop": new_stop_utc}, context=NOISE_OFF)
    print(f"  [1] calendar.event.start/stop -> {new_start_utc} / {new_stop_utc}{' (would)' if dry else ''}")

    # 2) resolve the owning order line(s) via the SDCAL token, rewrite SDBK1 date+time (+resource if lane changed)
    _, line_ids = RA.resolve_targets(c, f"e{eid}")
    new_date = args.start[:10]
    new_time = args.start[11:16]
    for L in (c.call("sale.order.line", "read", line_ids,
                     fields=["id", "name", "product_custom_attribute_value_ids"]) if line_ids else []):
        nm = L.get("name") or ""
        if "SDBK1|" in nm:
            newnm = sub_sdbk1_dt(nm, new_date, new_time)
            if lane_change:
                newnm = RA.SDBK1_RES.sub(lambda mm: mm.group(1) + RA.RESOURCE_NAME[rid] + mm.group(2), newnm)
            if newnm != nm:
                W("sale.order.line", L["id"], {"name": newnm})
                print(f"  [2] SOL {L['id']} SDBK1 date/time -> {new_date} {new_time}{' (+resource)' if lane_change else ''}{' (would)' if dry else ''}")
        for cv in L.get("product_custom_attribute_value_ids", []):
            v = c.call("product.attribute.custom.value", "read", [cv], fields=["custom_value"])[0]
            cvv = v.get("custom_value") or ""
            if cvv.startswith("SDBK1"):
                newcv = sub_sdbk1_dt(cvv, new_date, new_time)
                if lane_change:
                    newcv = RA.SDBK1_RES.sub(lambda mm: mm.group(1) + RA.RESOURCE_NAME[rid] + mm.group(2), newcv)
                if newcv != cvv:
                    W("product.attribute.custom.value", cv, {"custom_value": newcv})
                    print(f"  [2] custom-val {cv} SDBK1 date/time -> {new_date} {new_time}{' (would)' if dry else ''}")

    # 3) lane change (resource hold + colour/attendee + title letter) — reuse the proven bits
    if lane_change:
        BLM = RA.bl_model(c)
        new_pid = RA.RESOURCE_PARTNER[rid]
        old_pids = [p for r, p in RA.RESOURCE_PARTNER.items() if r != rid]
        letter = RA.RESOURCE_NAME[rid][0]
        full = c.call("calendar.event", "read", [eid], fields=["name", "partner_ids", "description"])[0]
        for bl in ev.get("booking_line_ids", []):
            cap = (c.call(BLM, "read", [bl], fields=["capacity_reserved", "capacity_used"]) or [{}])[0]
            W(BLM, bl, {"appointment_resource_id": rid,
                        "capacity_reserved": cap.get("capacity_reserved") or 1,
                        "capacity_used": cap.get("capacity_used") or 1})
            print(f"  [3] {BLM} {bl}.appointment_resource_id -> {rid}{' (would)' if dry else ''}")
        drop = [p for p in old_pids if p in full["partner_ids"]]
        cmds = ([(4, new_pid)] if new_pid not in full["partner_ids"] else []) + [(3, p) for p in drop]
        if cmds:
            W("calendar.event", eid, {"partner_ids": cmds}, context=NOISE_OFF)
            print(f"  [3] attendees +{new_pid} -{drop}{' (would)' if dry else ''}")
        newname = re.sub(r"^(\W*)?[AK](\s)", lambda mm: (mm.group(1) or "") + letter + mm.group(2), full["name"], count=1)
        if newname != full["name"]:
            W("calendar.event", eid, {"name": newname}, context=NOISE_OFF)
            print(f"  [3] title '{full['name']}' -> '{newname}'{' (would)' if dry else ''}")
        desc = full.get("description") or ""
        newdesc = re.sub(r"(Detailer:\s*)([^<\n]+)", lambda mm: mm.group(1) + RA.RESOURCE_NAME[rid], desc, count=1)
        if newdesc != desc:
            W("calendar.event", eid, {"description": newdesc}, context=NOISE_OFF)
            print(f"  [3] description Detailer -> {RA.RESOURCE_NAME[rid]}{' (would)' if dry else ''}")

    print(f"\nDONE{' (dry-run — pass --commit)' if dry else ' (committed)'}")


if __name__ == "__main__":
    main()
