"""
Undo a booking created by sync_bookings.py — for cleaning up test runs / rollback.

Given an ORDER (name or id) or an EVENT id, this:
  1. finds the calendar.event(s) that sync created (by the SDBK1:L<line> marker,
     or the explicit --event-id),
  2. unlinks them, and
  3. strips the matching SDCAL:L..=E.. token from the order's client_order_ref.

DRY-RUN by default (it deletes records — you must pass --commit to act).

Usage:
    python undo_booking_event.py --order S00088             # preview
    python undo_booking_event.py --order S00088 --commit    # delete its synced events + clear markers
    python undo_booking_event.py --event-id 42 --commit     # delete one specific event + clear its order marker
"""

import argparse
import io
import re
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

NOISE_OFF = {"mail_create_nolog": True, "tracking_disable": True}


def log(m):
    print(m, flush=True)


def resolve_order(c, ident):
    """Return the sale.order dict for a name (S00088) or numeric id, else None."""
    ident = ident.strip()
    domain = [["id", "=", int(ident)]] if ident.isdigit() else [["name", "=", ident]]
    hits = c.call("sale.order", "search_read", domain,
                  fields=["id", "name", "client_order_ref"], limit=1)
    return hits[0] if hits else None


def events_for_order(c, order):
    """Find synced events for an order: by client_order_ref E<id> tokens, and by
    the per-line SDBK1 markers of the order's lines (covers a wiped ref)."""
    ids = set()

    # (a) from client_order_ref audit tokens: SDCAL:L<line>=E<event>
    ref = order.get("client_order_ref") or ""
    for m in re.finditer(r"SDCAL:L\d+=E(\d+)", ref):
        ids.add(int(m.group(1)))

    # (b) from the order's own lines' SDBK1 markers in event descriptions
    line_ids = c.call("sale.order.line", "search",
                      [["order_id", "=", order["id"]]])
    for lid in line_ids:
        # BOUNDED marker (full HTML comment) so L12 can't match L123.
        hit = c.call("calendar.event", "search",
                     [["description", "ilike", f"<!-- SDBK1:L{lid} -->"]])
        ids.update(hit)
    return sorted(ids)


def strip_ref_tokens(c, order, event_ids, commit):
    """Remove SDCAL:...=E<id> tokens for the deleted events from client_order_ref."""
    ref = order.get("client_order_ref") or ""
    if not ref:
        return
    keep = []
    for tok in ref.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"SDCAL:L\d+=E(\d+)", tok)
        if m and int(m.group(1)) in event_ids:
            continue   # drop it
        keep.append(tok)
    new_ref = ";".join(keep)
    if new_ref == ref:
        return
    log(f"    client_order_ref: {ref!r} -> {new_ref!r}")
    if commit:
        c.call("sale.order", "write", [order["id"]], {"client_order_ref": new_ref},
               context=NOISE_OFF)


def order_for_event(c, event_id):
    """Best-effort: find the order whose client_order_ref references this event."""
    hits = c.call("sale.order", "search_read",
                  [["client_order_ref", "ilike", f"=E{event_id}"]],
                  fields=["id", "name", "client_order_ref"], limit=1)
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser(description="Undo a synced booking event")
    ap.add_argument("--order", help="order name (S00088) or numeric id")
    ap.add_argument("--event-id", type=int, help="delete one specific calendar.event id")
    ap.add_argument("--commit", action="store_true", help="actually unlink (default: dry-run)")
    args = ap.parse_args()
    if not args.order and not args.event_id:
        ap.error("give --order or --event-id")

    mode = "LIVE" if args.commit else "DRY-RUN"
    log(f"=== SD booking undo [{mode}] ===")
    c = OdooClient()
    log(f"    connected uid={c.uid} db={c.db}")

    if args.event_id:
        event_ids = [args.event_id]
        order = order_for_event(c, args.event_id)
    else:
        order = resolve_order(c, args.order)
        if not order:
            log(f"    order {args.order!r} not found")
            sys.exit(1)
        event_ids = events_for_order(c, order)

    if order:
        log(f"    order {order['name']} (id {order['id']}) ref={order.get('client_order_ref')!r}")
    if not event_ids:
        log("    no synced events found — nothing to undo")
        return

    ev = c.call("calendar.event", "read", event_ids, fields=["id", "name", "start"])
    for e in ev:
        log(f"    event {e['id']}: {e['name']} @ {e['start']}")

    if not args.commit:
        log(f"    DRY-RUN — would unlink {len(event_ids)} event(s) and clear their markers")
        if order:
            strip_ref_tokens(c, order, set(event_ids), commit=False)
        return

    c.call("calendar.event", "unlink", event_ids, context=NOISE_OFF)
    log(f"    unlinked {len(event_ids)} event(s): {event_ids}")
    if order:
        strip_ref_tokens(c, order, set(event_ids), commit=True)
    log("=== done ===")


if __name__ == "__main__":
    main()
