"""One-off: purge the test-residue sale orders (+ their appointment events, opportunities,
and the duplicate 'Will Howson' contact #88) left over from 22-23 Jul booking testing.

KEEPS: S00100 (real Will order #79) and INV/26-27/0001 (real paid invoice). Their invoices
were already removed manually, so the accounting lock is released and the orders can go.

  python cleanup_test_orders.py           # dry-run: list exactly what would be deleted
  python cleanup_test_orders.py --commit   # do it
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo_client import OdooClient

COMMIT = "--commit" in sys.argv
JUNK_ORDERS = ["S00103", "S00104", "S00105", "S00106", "S00107", "S00108"]
KEEP_PARTNERS = {3, 4, 79}          # company, public user, real Will — never delete these contacts
DUP_CONTACTS = [88]                  # duplicate 'Will Howson' to remove
NOM = {"active_test": False, "tracking_disable": True, "mail_create_nolog": True,
       "mail_notify_author": False}
c = OdooClient()


def ids_of(model, domain):
    return c.call(model, "search", domain, context=NOM)


def main():
    orders = c.call("sale.order", "search_read", [["name", "in", JUNK_ORDERS]],
                    fields=["id", "name", "state", "opportunity_id", "partner_id"], context=NOM)
    order_ids = [o["id"] for o in orders]
    opp_ids = sorted({o["opportunity_id"][0] for o in orders if o.get("opportunity_id")})
    # appointment events tied to those opportunities (E43, E53, ...)
    ev = c.call("calendar.event", "search_read", [["opportunity_id", "in", opp_ids]],
                fields=["id", "name", "start", "appointment_booker_id"], context=NOM) if opp_ids else []
    ev_ids = [e["id"] for e in ev]

    print("=== ORDERS to delete ===")
    for o in orders:
        print(f"  {o['name']} ({o['state']}) partner={o['partner_id'][1]} opp={o.get('opportunity_id')}")
    print("=== APPOINTMENT EVENTS to delete ===")
    for e in ev:
        print(f"  E{e['id']} {e['name']} {e['start']} booker={e['appointment_booker_id']}")
    print("=== OPPORTUNITIES (crm.lead) to delete ===", opp_ids)
    print("=== DUPLICATE CONTACTS to delete ===", DUP_CONTACTS)

    if not COMMIT:
        print("\n(dry-run) re-run with --commit to delete")
        return

    # 1) cancel confirmed orders so they can be unlinked
    confirmed = [o["id"] for o in orders if o["state"] not in ("draft", "cancel")]
    if confirmed:
        c.call("sale.order", "action_cancel", confirmed)
        print(f"cancelled orders {confirmed}")
    # 2) delete appointment events (clear attendees first to avoid the calendar unlink path)
    for e in ev:
        att = ids_of("calendar.attendee", [["event_id", "=", e["id"]]])
        if att:
            c.call("calendar.attendee", "unlink", att, context=NOM)
        c.call("calendar.event", "unlink", [e["id"]], context=NOM)
    print(f"deleted events {ev_ids}")
    # 3) delete the orders
    if order_ids:
        c.call("sale.order", "unlink", order_ids, context=NOM)
        print(f"deleted orders {order_ids}")
    # 4) delete the junk opportunities
    if opp_ids:
        c.call("crm.lead", "unlink", opp_ids, context=NOM)
        print(f"deleted opportunities {opp_ids}")
    # 5) delete the duplicate contact(s) — guard against ever hitting a keep-partner
    dups = [p for p in DUP_CONTACTS if p not in KEEP_PARTNERS]
    if dups:
        c.call("res.partner", "unlink", dups, context=NOM)
        print(f"deleted contacts {dups}")

    # verify
    left = c.call("sale.order", "search", [["name", "in", JUNK_ORDERS]], context=NOM)
    print("\nJunk orders remaining:", left or "NONE ✓")


if __name__ == "__main__":
    main()
