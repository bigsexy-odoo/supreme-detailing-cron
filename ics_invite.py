"""Minimal, dependency-free iCalendar (.ics) builder for Supreme Detailing bookings.

A reschedule attaches one of these to the customer email so their own calendar
(Google/Apple/Outlook) MOVES the existing entry instead of showing the old time.
The update only lands on the SAME entry if the UID matches the original invite, so
we use our own STABLE uid `sd-<event_id>@supremedetailing.co.nz` for the whole
lifecycle (confirm + every reschedule) and bump SEQUENCE each time.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def _esc(s):
    """Escape per RFC 5545 (backslash, semicolon, comma, newline)."""
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _z(dt_str):
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> iCal UTC stamp '20260726T230000Z'."""
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%dT%H%M%SZ")


def sequence_now():
    """Monotonic-increasing SEQUENCE (epoch minutes) — always higher than the
    confirmation's SEQUENCE 0, so every reschedule supersedes the last."""
    return int(datetime.now(UTC).timestamp()) // 60


def build_ics(event_id, summary, start_utc, stop_utc, location,
              sequence, org_email, org_name, att_email, att_name, cancel=False):
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    uid = f"sd-{event_id}@supremedetailing.co.nz"
    method = "CANCEL" if cancel else "REQUEST"
    status = "CANCELLED" if cancel else "CONFIRMED"
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Supreme Detailing//Booking//EN",
        "VERSION:2.0",
        f"METHOD:{method}",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_z(start_utc)}",
        f"DTEND:{_z(stop_utc)}",
        f"SUMMARY:{_esc(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    lines += [
        f"ORGANIZER;CN={_esc(org_name)}:mailto:{org_email}",
        f"ATTENDEE;CN={_esc(att_name)};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{att_email}",
        f"STATUS:{status}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


if __name__ == "__main__":  # smoke test
    print(build_ics(40, "Supreme Detailing — Supreme Detail (Full)",
                    "2026-07-25 23:00:00", "2026-07-26 02:30:00", "Mount Eden, Auckland",
                    sequence_now(), "admin@supremedetailing.co.nz", "Supreme Detailing",
                    "will@example.com", "Will Howson"))
