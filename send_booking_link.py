"""Re-send the booking confirmation email (which carries the customer's self-service
'manage it online' link = view / RESCHEDULE / cancel) for a booking.

  python send_booking_link.py S00100          # dry-run: show who + the reschedule link
  python send_booking_link.py S00100 --send    # actually email the customer
  python send_booking_link.py e40 --send        # by calendar.event id

Reschedule is allowed up to `appointment.type.min_cancellation_hours` before the start
(currently 1h). The customer's reschedule fires the same Chat card + reschedule email as any
other path. Uses the native confirmation template 37 (has the manage/reschedule link).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo_client import OdooClient
import reassign_detailer as RA   # reuse resolve_targets (order name / eNN -> event ids)

CONFIRM_TEMPLATE_ID = 37   # "Appointment: Attendee Invitation" — carries the manage/reschedule link
BASE = "https://www.supremedetailing.co.nz"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    target = sys.argv[1]
    send = "--send" in sys.argv
    c = OdooClient()
    events, _ = RA.resolve_targets(c, target)
    if not events:
        raise SystemExit(f"No calendar events resolved for {target!r} (pass eNN or an order name).")
    for eid in events:
        ev = c.call("calendar.event", "read", [eid], fields=["name", "start", "appointment_booker_id", "active"])
        if not ev or not ev[0].get("active", True):
            continue  # stale SDCAL token — event deleted or archived/superseded
        ev = ev[0]
        booker = ev.get("appointment_booker_id") or [None, "?"]
        # the booker's attendee (the confirmation is a calendar.attendee template) + their token
        atts = c.call("calendar.attendee", "search_read",
                      [["event_id", "=", eid], ["partner_id", "=", booker[0]]],
                      fields=["id", "access_token"]) if booker[0] else []
        email = ""
        if booker[0]:
            email = (c.call("res.partner", "read", [booker[0]], fields=["email"])[0].get("email")) or ""
        link = f"{BASE}/calendar/meeting/view?token={(atts[0]['access_token'] if atts else '')}&id={eid}"
        print(f"\nevent {eid} '{ev['name']}'  start={ev['start']}")
        print(f"  customer : {booker[1]} <{email or 'NO EMAIL'}>")
        print(f"  reschedule link: {link}")
        if not atts:
            print("  [skip] booker has no attendee on this event")
            continue
        if not send:
            print("  (dry-run) would re-send the confirmation email — add --send")
            continue
        if not email:
            print("  [skip] no customer email")
            continue
        c.call("mail.template", "send_mail", [CONFIRM_TEMPLATE_ID], atts[0]["id"], force_send=True)
        print("  -> confirmation email re-sent (with the manage/reschedule link)")

    print("\nDone." + ("" if send else "  (dry-run — add --send to actually email)"))


if __name__ == "__main__":
    main()
