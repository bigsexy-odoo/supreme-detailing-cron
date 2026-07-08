"""Create (idempotently) the Odoo no-code automation + native webhook action that fires the
event-driven booking sync the moment a customer checks out. Zero billable LoC: base.automation
(no-code trigger) + ir.actions.server state='webhook' (native, not billable per Rule 9).

Run AFTER the Apps Script relay is deployed:
    python build_booking_webhook.py "https://script.google.com/macros/s/XXXX/exec?key=<RELAY_KEY>"

Fires when a sale.order that CARRIES A BOOKING (an SDBK1 token baked into a line name) transitions
to 'sent' or 'sale' -> POSTs to the relay -> GitHub repository_dispatch -> sync reserves the slot.
Non-booking orders never fire (filter on order_line.name), so no wasted Actions runs.
"""
import sys
from odoo_client import OdooClient

MODEL_SALE_ORDER = 650      # ir.model id of sale.order (verified on this DB)
STATE_FIELD = 12905         # ir.model.fields id of sale.order.state
ACTION_NAME = "SD · Booking checkout → trigger sync"
AUTOMATION_NAME = "SD · Booking checkout webhook"
# Only booking orders: their lines carry Odoo's baked "Booking: Custom: SDBK1|..." text.
FILTER = "[('state','in',['sent','sale']),('order_line.name','ilike','SDBK1|')]"


def main():
    if len(sys.argv) < 2 or "http" not in sys.argv[1]:
        raise SystemExit('usage: python build_booking_webhook.py "<relay /exec URL incl. ?key=...>"')
    relay_url = sys.argv[1].strip()
    c = OdooClient()

    fids = [f["id"] for f in c.call("ir.model.fields", "search_read",
            [["model", "=", "sale.order"], ["name", "in", ["id", "name", "state"]]], fields=["id"])]

    # 1. native webhook action (idempotent)
    ex = c.call("ir.actions.server", "search_read", [["name", "=", ACTION_NAME]], fields=["id"])
    if ex:
        sa_id = ex[0]["id"]
        c.call("ir.actions.server", "write", [sa_id], {"webhook_url": relay_url})
        print(f"updated webhook action {sa_id} -> {relay_url[:60]}...")
    else:
        r = c.call("ir.actions.server", "create", [{
            "name": ACTION_NAME,
            "model_id": MODEL_SALE_ORDER,
            "state": "webhook",
            "webhook_url": relay_url,
            "webhook_field_ids": [(6, 0, fids)],
        }])
        sa_id = r[0] if isinstance(r, list) else r
        print(f"created webhook action {sa_id}")

    # 2. no-code automation rule: booking order -> sent/sale -> run the webhook action
    exa = c.call("base.automation", "search_read", [["name", "=", AUTOMATION_NAME]], fields=["id"])
    vals = {
        "name": AUTOMATION_NAME,
        "model_id": MODEL_SALE_ORDER,
        "trigger": "on_create_or_write",
        "trigger_field_ids": [(6, 0, [STATE_FIELD])],   # only when state changes
        "filter_domain": FILTER,
        "action_server_ids": [(6, 0, [sa_id])],
        "active": True,
    }
    if exa:
        c.call("base.automation", "write", [exa[0]["id"]], vals)
        print(f"updated automation {exa[0]['id']}")
    else:
        r = c.call("base.automation", "create", [vals])
        print(f"created automation {r[0] if isinstance(r, list) else r}")
    print("DONE -- a booking checkout now triggers the sync within seconds (relay -> repository_dispatch).")


if __name__ == "__main__":
    main()
