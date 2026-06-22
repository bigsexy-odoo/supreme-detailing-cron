"""
Enrich calendar events created by Odoo Appointments with vehicle, address,
services, and contact data pulled from linked booking records.

Writes the assembled info into the event's `description` (HTML) and `location`
fields so it syncs to Google Calendar / mobile notifications.

Usage:
    python enrich_calendar_events.py              # enrich unenriched future + recent events
    python enrich_calendar_events.py --dry-run    # preview without writing
    python enrich_calendar_events.py --force      # re-enrich ALL appointment events
    python enrich_calendar_events.py --event-id 2 # enrich a specific event

Designed to be safe for repeated runs (idempotent) and suitable for scheduling
via GitHub Actions cron or Windows Task Scheduler.
"""

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone

# Force UTF-8 stdout on Windows (avoid charmap codec errors with emoji)
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ENRICHED_MARKER = "[SD-enriched]"
NZST = timezone(timedelta(hours=12))

# Question IDs (from appointment.question)
Q_PHONE = 1       # Phone number (on combo/bundle types 8-19)
Q_VEHICLE = 2     # Vehicle make, model & colour (types 1-7)
Q_ADDRESS = 3     # Service street address (types 1-7)
Q_SUBURB = 4      # Suburb dropdown (types 1-7)


def parse_args():
    p = argparse.ArgumentParser(description="Enrich Supreme Detailing calendar events")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be written without actually writing")
    p.add_argument("--force", action="store_true",
                   help="Re-enrich events even if they already have a description")
    p.add_argument("--event-id", type=int, default=None,
                   help="Enrich a specific event ID only")
    return p.parse_args()


def get_active_langs(c: OdooClient) -> list[str]:
    """Return codes of all active en_* languages (Rule 1)."""
    langs = c.call("res.lang", "search_read",
                   [["active", "=", True]], fields=["code", "name"])
    # Write to all active languages, but at minimum en_AU
    codes = [l["code"] for l in langs]
    if not codes:
        codes = ["en_AU"]
    return codes


def fetch_answer_labels(c: OdooClient) -> dict[int, str]:
    """Build a lookup of appointment.answer id -> display name (for dropdowns)."""
    answers = c.call("appointment.answer", "search_read",
                     [], fields=["id", "name"])
    return {a["id"]: a["name"] for a in answers}


def fetch_resource_names(c: OdooClient) -> dict[int, str]:
    """Build a lookup of appointment.resource id -> name."""
    resources = c.call("appointment.resource", "search_read",
                       [], fields=["id", "name"])
    return {r["id"]: r["name"] for r in resources}


def find_events(c: OdooClient, args) -> list[dict]:
    """Find calendar events that need enrichment."""
    domain = [["appointment_type_id", "!=", False]]

    if args.event_id:
        domain.append(["id", "=", args.event_id])
    else:
        # Future events + last 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        domain.append(["start", ">=", cutoff])

    if not args.force and not args.event_id:
        # Only unenriched: description is empty/False
        domain.append("|")
        domain.append(["description", "=", False])
        domain.append(["description", "=", ""])

    fields = [
        "id", "name", "start", "stop", "description", "location",
        "appointment_type_id", "appointment_status", "appointment_booker_id",
        "appointment_answer_input_ids", "appointment_resource_ids",
        "booking_line_ids", "sale_order_line_ids", "partner_ids",
    ]
    events = c.call("calendar.event", "search_read", domain, fields=fields)
    return events


def gather_answers(c: OdooClient, answer_input_ids: list[int],
                   answer_labels: dict[int, str]) -> dict:
    """Read appointment.answer.input records and return structured data."""
    result = {"phone": None, "vehicle": None, "address": None, "suburb": None}
    if not answer_input_ids:
        return result

    inputs = c.call("appointment.answer.input", "read", answer_input_ids,
                    fields=["question_id", "question_type", "value_answer_id",
                            "value_text_box"])

    for inp in inputs:
        qid = inp["question_id"][0] if inp["question_id"] else None
        if qid == Q_PHONE:
            result["phone"] = (inp.get("value_text_box") or "").strip() or None
        elif qid == Q_VEHICLE:
            result["vehicle"] = (inp.get("value_text_box") or "").strip() or None
        elif qid == Q_ADDRESS:
            result["address"] = (inp.get("value_text_box") or "").strip() or None
        elif qid == Q_SUBURB:
            aid = inp.get("value_answer_id")
            if aid and isinstance(aid, list):
                result["suburb"] = answer_labels.get(aid[0], aid[1] if len(aid) > 1 else str(aid[0]))
            elif aid and isinstance(aid, int):
                result["suburb"] = answer_labels.get(aid, str(aid))

    return result


def gather_answers_via_booking(c: OdooClient, event_id: int,
                                answer_labels: dict[int, str]) -> dict:
    """Fallback: find answers via calendar.booking linked to this event."""
    bookings = c.call("calendar.booking", "search_read",
                      [["calendar_event_id", "=", event_id]],
                      fields=["id", "appointment_answer_input_ids"])
    all_input_ids = []
    for b in bookings:
        all_input_ids.extend(b.get("appointment_answer_input_ids", []))

    if all_input_ids:
        return gather_answers(c, all_input_ids, answer_labels)
    return {"phone": None, "vehicle": None, "address": None, "suburb": None}


def gather_contact(c: OdooClient, booker_id) -> dict:
    """Read contact details from the booker partner."""
    result = {"name": None, "email": None, "phone": None}
    if not booker_id:
        return result

    pid = booker_id[0] if isinstance(booker_id, list) else booker_id
    partners = c.call("res.partner", "read", [pid],
                      fields=["name", "email", "phone"])
    if partners:
        p = partners[0]
        result["name"] = p.get("name") or None
        result["email"] = p.get("email") or None
        result["phone"] = p.get("phone") or None
    return result


def gather_services(c: OdooClient, sol_ids: list[int]) -> list[dict]:
    """Read sale order line details for service names and prices."""
    if not sol_ids:
        return []
    lines = c.call("sale.order.line", "read", sol_ids,
                   fields=["product_id", "name", "price_unit", "product_uom_qty",
                            "price_subtotal", "order_id"])
    services = []
    for ln in lines:
        product_name = ln["product_id"][1] if ln.get("product_id") else ln.get("name", "Unknown")
        # Strip internal reference prefix like [SD-PKG-EXT]
        display_name = product_name
        if display_name and display_name.startswith("["):
            bracket_end = display_name.find("] ")
            if bracket_end != -1:
                display_name = display_name[bracket_end + 2:]
        services.append({
            "name": display_name,
            "price": ln.get("price_subtotal") or ln.get("price_unit", 0),
            "qty": ln.get("product_uom_qty", 1),
            "order": ln["order_id"][1] if ln.get("order_id") else None,
        })
    return services


def gather_resources(c: OdooClient, resource_ids: list[int],
                     resource_names: dict[int, str]) -> list[str]:
    """Resolve resource IDs to names."""
    if not resource_ids:
        return []
    return [resource_names.get(rid, f"Resource {rid}") for rid in resource_ids]


def build_description(answers: dict, contact: dict, services: list[dict],
                       resources: list[str]) -> str:
    """Assemble the enriched description as HTML (calendar.event.description is html type).

    Uses simple HTML with <br/> for line breaks. Keeps it readable when HTML
    is stripped (e.g. Google Calendar plain-text view).
    """
    lines = []

    # Vehicle
    vehicle = answers.get("vehicle")
    if vehicle:
        lines.append(f"\U0001f697 Vehicle: {vehicle}")

    # Address + suburb
    address = answers.get("address")
    suburb = answers.get("suburb")
    if address and suburb:
        lines.append(f"\U0001f4cd Address: {address}, {suburb}")
    elif address:
        lines.append(f"\U0001f4cd Address: {address}")
    elif suburb:
        lines.append(f"\U0001f4cd Suburb: {suburb}")

    # Services
    if services:
        svc_parts = [s["name"] for s in services]
        total = sum(s["price"] for s in services)
        svc_str = " + ".join(svc_parts)
        lines.append(f"\U0001f9fd Services: {svc_str} — ${total:.2f}")

    # Contact
    contact_name = contact.get("name")
    contact_email = contact.get("email")
    contact_phone = contact.get("phone") or answers.get("phone")
    contact_parts = [p for p in [contact_name, contact_email, contact_phone] if p]
    if contact_parts:
        lines.append(f"\U0001f4de Contact: {', '.join(contact_parts)}")

    # Detailer
    if resources:
        lines.append(f"\U0001f464 Detailer: {', '.join(resources)}")

    if not lines:
        return ""

    lines.append("")
    lines.append(ENRICHED_MARKER)

    # Join with <br/> for HTML field, but keep it readable as plain text too
    return "<br/>\n".join(lines)


def build_location(answers: dict) -> str | None:
    """Build the location string (syncs to Google Calendar -> Google Maps)."""
    address = answers.get("address")
    suburb = answers.get("suburb")
    if address and suburb:
        return f"{address}, {suburb}, Auckland"
    elif address:
        return f"{address}, Auckland"
    elif suburb:
        return f"{suburb}, Auckland"
    return None


def enrich_event(c: OdooClient, event: dict, answer_labels: dict[int, str],
                 resource_names: dict[int, str], langs: list[str],
                 dry_run: bool) -> bool:
    """Enrich a single calendar event. Returns True if enrichment was applied."""
    eid = event["id"]
    ename = event.get("name", f"Event {eid}")
    estart = event.get("start", "unknown")

    # Skip already-enriched unless --force
    desc = event.get("description") or ""
    if ENRICHED_MARKER in desc:
        print(f"  SKIP {eid} '{ename}' ({estart}) — already enriched")
        return False

    print(f"\n  Processing event {eid}: '{ename}' ({estart})")

    # --- Gather data ---

    # Answers: try direct event link first, then via booking
    answer_input_ids = event.get("appointment_answer_input_ids", [])
    answers = gather_answers(c, answer_input_ids, answer_labels)

    # If we got nothing useful, try via calendar.booking
    if not any(answers.values()):
        answers_via_booking = gather_answers_via_booking(c, eid, answer_labels)
        # Merge: prefer direct, fill gaps from booking
        for key in answers:
            if not answers[key] and answers_via_booking.get(key):
                answers[key] = answers_via_booking[key]

    # Contact
    contact = gather_contact(c, event.get("appointment_booker_id"))

    # Services
    services = gather_services(c, event.get("sale_order_line_ids", []))

    # Resources
    resources = gather_resources(c, event.get("appointment_resource_ids", []),
                                  resource_names)

    # --- Log what we found ---
    print(f"    Vehicle:  {answers.get('vehicle') or '(none)'}")
    print(f"    Address:  {answers.get('address') or '(none)'}")
    print(f"    Suburb:   {answers.get('suburb') or '(none)'}")
    print(f"    Contact:  {contact.get('name') or '(none)'} / {contact.get('email') or '(none)'} / {contact.get('phone') or answers.get('phone') or '(none)'}")
    print(f"    Services: {', '.join(s['name'] for s in services) if services else '(none)'}")
    print(f"    Detailer: {', '.join(resources) if resources else '(none)'}")

    # --- Build output ---
    description = build_description(answers, contact, services, resources)
    location = build_location(answers)

    if not description:
        print(f"    SKIP — no data found to enrich with")
        return False

    print(f"    Location: {location or '(none)'}")
    print(f"    Description:")
    for line in description.replace("<br/>\n", "\n").split("\n"):
        print(f"      {line}")

    if dry_run:
        print(f"    DRY RUN — would write description + location")
        return True

    # --- Write to all active languages (Rule 1) ---
    vals = {"description": description}
    if location:
        vals["location"] = location

    for lang in langs:
        try:
            c.call("calendar.event", "write", [eid], vals,
                   context={"lang": lang})
        except Exception as e:
            print(f"    WARNING: write failed for lang={lang}: {e}")

    print(f"    ENRICHED (wrote to {len(langs)} lang(s): {', '.join(langs)})")
    return True


def main():
    args = parse_args()
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"=== Supreme Detailing Calendar Event Enrichment ({mode}) ===")
    print(f"    Time: {datetime.now(NZST).strftime('%Y-%m-%d %H:%M NZST')}")

    c = OdooClient()
    print(f"    Connected to {c.url} as uid={c.uid}")

    # Pre-fetch lookups
    answer_labels = fetch_answer_labels(c)
    resource_names = fetch_resource_names(c)
    langs = get_active_langs(c)
    print(f"    Active languages: {', '.join(langs)}")
    print(f"    Answer labels: {len(answer_labels)} suburb options loaded")
    print(f"    Resources: {len(resource_names)} detailers loaded")

    # Find events
    events = find_events(c, args)
    print(f"\n  Found {len(events)} event(s) to process")

    if not events:
        print("  Nothing to do.")
        return

    enriched = 0
    skipped = 0
    errors = 0

    for event in events:
        try:
            if enrich_event(c, event, answer_labels, resource_names, langs, args.dry_run):
                enriched += 1
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            print(f"    ERROR on event {event['id']}: {e}")

    print(f"\n=== Done: {enriched} enriched, {skipped} skipped, {errors} errors ===")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
