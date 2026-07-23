"""
Daily job summary — posts tomorrow's bookings to Google Chat at 6pm NZST.

Reads enriched calendar events for the next day, groups by resource
(Alex → North space, Kade → Central space), and posts a formatted
summary card to each Google Chat webhook.

Usage:
    python daily_summary.py              # post tomorrow's summary
    python daily_summary.py --dry-run    # preview without posting
    python daily_summary.py --date 2026-07-05  # specific date

Environment:
    ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY  — Odoo RPC auth
    GCHAT_NORTH_WEBHOOK   — Google Chat webhook for SD North space
    GCHAT_CENTRAL_WEBHOOK — Google Chat webhook for SD Central space
"""

import argparse
import io
import os
import sys
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient
from chat_poster import post_payload
import enrich_calendar_events as E
import booking_card as BC

NZST = timezone(timedelta(hours=12))

# Resource ID → webhook env var mapping
# Alex (North Shore) = resource 1, Kade (Central) = resource 2
RESOURCE_WEBHOOK = {
    1: "GCHAT_NORTH_WEBHOOK",    # Alex
    2: "GCHAT_CENTRAL_WEBHOOK",  # Kade
}

RESOURCE_NAMES = {
    1: "Alex (North Shore)",
    2: "Kade (Central Auckland)",
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_args():
    p = argparse.ArgumentParser(description="Post daily job summary to Google Chat")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview without posting to Chat")
    p.add_argument("--date", type=str, default=None,
                   help="Target date YYYY-MM-DD (default: tomorrow)")
    return p.parse_args()


def get_target_date(date_str=None):
    """Return the target date as a string. Default = tomorrow NZ time."""
    if date_str:
        return date_str
    nz_now = datetime.now(NZST)
    tomorrow = nz_now + timedelta(days=1)
    return tomorrow.strftime("%Y-%m-%d")


def format_date_display(date_str):
    """'2026-07-05' → 'Saturday 5 July'"""
    parts = date_str.split("-")
    d = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{DAY_NAMES[d.weekday()]} {d.day} {months[d.month - 1]}"


def fetch_events_for_date(c, target_date):
    """Fetch all appointment calendar events for a specific NZ calendar date.

    `start` is stored in UTC, so convert the NZ-day bounds to UTC — otherwise a
    NZ-morning booking (9am NZ = 21:00 UTC the previous day) falls in the wrong
    UTC day and gets missed.
    """
    y, m, d = (int(x) for x in target_date.split("-"))
    nz_start = datetime(y, m, d, 0, 0, 0, tzinfo=NZST)
    nz_end = datetime(y, m, d, 23, 59, 59, tzinfo=NZST)
    start_of_day = nz_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_of_day = nz_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    domain = [
        ["appointment_type_id", "!=", False],
        ["start", ">=", start_of_day],
        ["start", "<=", end_of_day],
    ]

    fields = [
        "id", "name", "start", "stop", "description", "location",
        "appointment_type_id", "appointment_booker_id",
        "appointment_resource_ids", "sale_order_line_ids",
        "appointment_answer_input_ids",
    ]

    events = c.call("calendar.event", "search_read", domain, fields=fields)
    return sorted(events, key=lambda e: e.get("start", ""))


def parse_description(desc):
    """Extract structured data from the enriched description."""
    if not desc:
        return {}
    # Strip HTML tags
    import re
    text = re.sub(r"<[^>]+>", "\n", desc)
    result = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Match emoji-prefixed lines from the enrichment script
        if "Vehicle:" in line:
            result["vehicle"] = line.split("Vehicle:", 1)[1].strip()
        elif "Address:" in line:
            result["address"] = line.split("Address:", 1)[1].strip()
        elif "Suburb:" in line:
            result["suburb"] = line.split("Suburb:", 1)[1].strip()
        elif "Services:" in line:
            result["services"] = line.split("Services:", 1)[1].strip()
        elif "Contact:" in line:
            result["contact"] = line.split("Contact:", 1)[1].strip()
        elif "Detailer:" in line:
            result["detailer"] = line.split("Detailer:", 1)[1].strip()
    return result


def format_time_nz(utc_str):
    """Convert Odoo UTC datetime string to NZ time display."""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        nz = dt.astimezone(NZST)
        h = nz.hour
        m = nz.minute
        ampm = "am" if h < 12 else "pm"
        if h == 0:
            h = 12
        elif h > 12:
            h -= 12
        if m:
            return f"{h}:{m:02d}{ampm}"
        return f"{h}{ampm}"
    except (ValueError, TypeError):
        return utc_str


def event_duration_hours(event):
    """Calculate event duration in hours from start/stop."""
    try:
        start = datetime.strptime(event["start"], "%Y-%m-%d %H:%M:%S")
        stop = datetime.strptime(event["stop"], "%Y-%m-%d %H:%M:%S")
        return (stop - start).total_seconds() / 3600
    except (ValueError, TypeError, KeyError):
        return 0


def build_summary_message(resource_id, events, target_date):
    """Build a Google Chat text message for one resource's daily jobs."""
    resource_name = RESOURCE_NAMES.get(resource_id, f"Resource {resource_id}")
    date_display = format_date_display(target_date)

    lines = [
        f"📋 *{date_display} — {resource_name}*",
        f"_{len(events)} job{'s' if len(events) != 1 else ''} scheduled_",
        "",
    ]

    total_hours = 0

    for i, event in enumerate(events, 1):
        time_str = format_time_nz(event.get("start", ""))
        duration = event_duration_hours(event)
        total_hours += duration

        info = parse_description(event.get("description") or "")

        # Job header
        lines.append(f"*{i}. {time_str}* ({duration:.1f}h)")

        # Vehicle
        vehicle = info.get("vehicle")
        if vehicle:
            lines.append(f"  🚗 {vehicle}")

        # Services
        services = info.get("services")
        if services:
            lines.append(f"  🧽 {services}")
        else:
            # Fallback to event name
            appt_name = event.get("appointment_type_id")
            if isinstance(appt_name, (list, tuple)) and len(appt_name) == 2:
                lines.append(f"  🧽 {appt_name[1]}")

        # Address
        address = info.get("address")
        suburb = info.get("suburb")
        loc = address or suburb or event.get("location")
        if loc:
            lines.append(f"  📍 {loc}")

        # Contact
        contact = info.get("contact")
        if contact:
            lines.append(f"  📞 {contact}")
        else:
            booker = event.get("appointment_booker_id")
            if isinstance(booker, (list, tuple)) and len(booker) == 2:
                lines.append(f"  📞 {booker[1]}")

        lines.append("")

    # Footer summary
    lines.append(f"⏱️ *Total: {total_hours:.1f}h across {len(events)} job{'s' if len(events) != 1 else ''}*")

    return "\n".join(lines)


def build_no_jobs_message(resource_id, target_date):
    """Build a message for when there are no jobs."""
    resource_name = RESOURCE_NAMES.get(resource_id, f"Resource {resource_id}")
    date_display = format_date_display(target_date)
    return f"📋 *{date_display} — {resource_name}*\n_No jobs scheduled_ ✅"


def main():
    args = parse_args()
    target_date = get_target_date(args.date)
    mode = "DRY RUN" if args.dry_run else "LIVE"

    print(f"=== Supreme Detailing Daily Summary ({mode}) ===")
    print(f"    Time: {datetime.now(NZST).strftime('%Y-%m-%d %H:%M NZST')}")
    print(f"    Target date: {target_date} ({format_date_display(target_date)})")

    c = OdooClient()
    print(f"    Connected to {c.url} as uid={c.uid}")
    # "Open schedule" button target = the native Resource Bookings gantt (gantt-first,
    # Alex/Kade lanes = availability). Opens straight to the gantt on tap.
    SCHEDULE_URL = f"{c.url}/odoo/action-650"

    # Lookups for rich-card gathering (shared with enrich_calendar_events)
    answer_labels = E.fetch_answer_labels(c)
    resource_names = E.fetch_resource_names(c)

    events = fetch_events_for_date(c, target_date)
    print(f"    Found {len(events)} event(s) for {target_date}")

    # Group events by resource
    grouped = {}
    unassigned = []

    for event in events:
        resource_ids = event.get("appointment_resource_ids", [])
        if resource_ids:
            for rid in resource_ids:
                if rid not in grouped:
                    grouped[rid] = []
                grouped[rid].append(event)
        else:
            unassigned.append(event)

    if unassigned:
        print(f"    WARNING: {len(unassigned)} event(s) with no resource assigned")

    # Post a rich cardsV2 message to each resource's Chat space
    date_display = format_date_display(target_date)
    posted = 0
    for resource_id, webhook_env in RESOURCE_WEBHOOK.items():
        webhook_url = os.environ.get(webhook_env)
        resource_name = RESOURCE_NAMES.get(resource_id, f"Resource {resource_id}")
        title = f"{date_display} — {resource_name}"

        resource_events = grouped.get(resource_id, [])
        if resource_events:
            bookings = [BC.gather_booking(c, ev, answer_labels, resource_names)
                        for ev in resource_events]
            # Tier 3: attach signed Mark-paid / Change-stage buttons (only if configured)
            act_url = os.environ.get("SD_ACTION_URL")
            act_secret = os.environ.get("SD_ACTION_SECRET")
            if act_url and act_secret:
                for b in bookings:
                    b["action_buttons"] = BC.action_buttons(b["event_id"], act_url, act_secret)
            text = BC.summary_text(resource_name, date_display, bookings)
            payload = BC.day_message(text, bookings, c.url, schedule_url=SCHEDULE_URL)
        else:
            payload = BC.no_jobs_message(resource_name, date_display, schedule_url=SCHEDULE_URL)

        print(f"\n  --- {resource_name} ({len(resource_events)} jobs) ---")
        print(f"  {payload.get('text')}")

        if args.dry_run:
            print(f"  DRY RUN — would post cardsV2 to {webhook_env}")
            continue

        if not webhook_url:
            print(f"  SKIP — {webhook_env} not set")
            continue

        if post_payload(payload, webhook_url):
            print(f"  POSTED to {webhook_env}")
            posted += 1
        else:
            print(f"  FAILED to post to {webhook_env}")

    print(f"\n=== Done: {posted} message(s) posted ===")


if __name__ == "__main__":
    main()
