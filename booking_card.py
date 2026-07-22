"""
booking_card.py — build rich Google Chat `cardsV2` for Supreme Detailing bookings.

A "large card" (Reel-helpdesk style) showing everything a detailer needs at a
glance: customer, mobile, address, vehicle, package + add-ons, start date/time,
duration, and the assigned detailer (Alex/Kade) — plus tappable buttons
(Open in Odoo · 📞 Call · 🧭 Directions).

Data is gathered from the calendar.event + linked records, reusing the same
readers as enrich_calendar_events.py (single source of truth). Stdlib-only →
runs unmodified on GitHub Actions.

    from booking_card import gather_booking, booking_section, day_message
    b = gather_booking(c, event, answer_labels, resource_names)
    payload = day_message("Sat 26 July — Alex (North Shore)", "1 job · 3.5h", [b], base_url)
    post_payload(payload, webhook_url)     # from chat_poster
"""

import hashlib
import hmac
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import enrich_calendar_events as E   # reuse gather_answers / gather_contact / gather_services / gather_resources

NZST = timezone(timedelta(hours=12))
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
def _nz(utc_str):
    return datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).astimezone(NZST)


def fmt_time(utc_str):
    try:
        nz = _nz(utc_str)
        h, m = nz.hour, nz.minute
        ampm = "am" if h < 12 else "pm"
        h = 12 if h == 0 else (h - 12 if h > 12 else h)
        return f"{h}:{m:02d}{ampm}"      # always h:mm (e.g. 9:00am)
    except (ValueError, TypeError):
        return utc_str or "?"


def fmt_date(utc_str):
    try:
        nz = _nz(utc_str)
        return f"{_DAYS[nz.weekday()]} {nz.day} {_MONTHS[nz.month - 1]}"
    except (ValueError, TypeError):
        return utc_str or "?"


def duration_h(event):
    try:
        s = datetime.strptime(event["start"], "%Y-%m-%d %H:%M:%S")
        e = datetime.strptime(event["stop"], "%Y-%m-%d %H:%M:%S")
        return (e - s).total_seconds() / 3600
    except (ValueError, TypeError, KeyError):
        return 0.0


def _m2o(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else None


# ---------------------------------------------------------------------------
# gather one booking into a flat dict
# ---------------------------------------------------------------------------
def gather_booking(c, event, answer_labels, resource_names):
    """Return a flat dict of everything the card shows for one calendar.event."""
    answers = E.gather_answers(c, event.get("appointment_answer_input_ids", []), answer_labels)
    if not any(answers.values()):
        answers = E.gather_answers_via_booking(c, event["id"], answer_labels)
    contact = E.gather_contact(c, event.get("appointment_booker_id"))
    services = E.gather_services(c, event.get("sale_order_line_ids", []))
    resources = E.gather_resources(c, event.get("appointment_resource_ids", []), resource_names)

    # package headline: the booked appointment type (always present on native + cart bookings)
    package = _m2o(event.get("appointment_type_id"))
    # everything the customer actually bought (cart bookings only) — packages + add-ons
    line_names = [s["name"] for s in services]

    # vehicle TYPE / size (Car / Station Wagon / SUV / Van / Truck / Ute)
    QSIZE = 5   # appointment.question "Vehicle size" (select)
    vtype = None
    for inp in (c.call("appointment.answer.input", "read",
                       event.get("appointment_answer_input_ids", []),
                       fields=["question_id", "value_answer_id"])
                if event.get("appointment_answer_input_ids") else []):
        qid = inp["question_id"][0] if inp.get("question_id") else None
        if qid == QSIZE and inp.get("value_answer_id"):
            aid = inp["value_answer_id"]
            vtype = answer_labels.get(aid[0] if isinstance(aid, (list, tuple)) else aid)
    if not vtype:   # fallback: cart bookings encode the size as the variant name suffix
        for ln in line_names:
            for s in ("Station Wagon", "Truck", "SUV", "Van", "Ute", "SW", "Car"):
                if ln.rstrip().endswith(s):
                    vtype = s
                    break
            if vtype:
                break

    # --- Full address ---------------------------------------------------------
    # The street address can live in EITHER the appointment answer (Q3, cart/appt
    # bookings) OR on the booker's partner record (checkout-captured). Prefer the
    # answer, then the partner address, then the suburb/location fallback.
    booker = event.get("appointment_booker_id")
    pid = booker[0] if isinstance(booker, (list, tuple)) else booker
    paddr = {}
    if pid:
        pr = c.call("res.partner", "read", [pid],
                    fields=["street", "street2", "city", "zip"])
        if pr:
            paddr = pr[0]
    partner_line = ", ".join(p for p in [
        ", ".join(x for x in [paddr.get("street"), paddr.get("street2")] if x),
        " ".join(x for x in [paddr.get("city"), paddr.get("zip")] if x),
    ] if p)

    ans_addr = answers.get("address")
    suburb = answers.get("suburb")
    if ans_addr:
        loc = ", ".join(x for x in [ans_addr, suburb] if x)
    elif partner_line:
        loc = partner_line
    else:
        loc = suburb or (event.get("location") or "").replace(", Auckland", "") or None
    maps_q = (f"{loc}, Auckland" if loc and "auckland" not in loc.lower() else loc) or event.get("location")

    return {
        "event_id": event["id"],
        "start": event.get("start"),
        "time": fmt_time(event.get("start")),
        "date": fmt_date(event.get("start")),
        "duration": duration_h(event),
        "customer": contact.get("name") or _m2o(event.get("appointment_booker_id")) or "—",
        "phone": contact.get("phone") or answers.get("phone"),
        "email": contact.get("email"),
        "vehicle": answers.get("vehicle"),
        "vtype": vtype,
        "location": loc,
        "suburb": suburb or paddr.get("city") or None,
        "maps_query": maps_q,
        "package": package,
        "line_names": line_names,
        "detailer": ", ".join(resources) if resources else None,
    }


# ---------------------------------------------------------------------------
# cardsV2 builders
# ---------------------------------------------------------------------------
def _row(label, text):
    return {"decoratedText": {"topLabel": label, "text": text, "wrapText": True}}


def _two_col(l1, t1, l2, t2):
    """Two labelled fields side by side, spaced across the row (Chat Columns widget)."""
    def col(label, text):
        return {"horizontalSizeStyle": "FILL_AVAILABLE_SPACE",
                "widgets": [{"decoratedText": {"topLabel": label, "text": text, "wrapText": True}}]}
    return {"columns": {"columnItems": [col(l1, t1), col(l2, t2)]}}


_SIZE_RE = re.compile(r"\s*[-–]\s*(Station Wagon|Truck|SUV|Van|Ute|SW|Car)\s*$", re.I)


def _strip_size(s):
    """Drop a trailing ' - <Size>' variant suffix (size already shows in the Type row)."""
    return _SIZE_RE.sub("", s or "").strip()


def _short_detailer(d):
    """'Kade (Central Auckland), Alex (North Shore)' -> 'Kade, Alex' (region dropped)."""
    if not d:
        return None
    names = [p.split("(")[0].strip() for p in d.split(",")]
    return ", ".join(n for n in names if n) or None


def _addons(b):
    """Booked lines other than the main package (size suffix stripped)."""
    pkg_l = (b.get("package") or "").lower()
    out = []
    for ln in (b.get("line_names") or []):
        clean = _strip_size(ln)
        cl = clean.lower()
        if not cl or cl == pkg_l or cl in pkg_l or (pkg_l and pkg_l in cl):
            continue
        out.append(clean)
    return out


def _heading_pkg(b):
    """Package label for headings/notification lines, with '+ add-ons' when applicable."""
    pkg = b.get("package") or ""
    return (pkg + " + add-ons") if (pkg and _addons(b)) else pkg


# ---------------------------------------------------------------------------
# Tier 3 — signed "act from Chat" buttons (Mark paid / Change stage)
# The button opens the OdooAction.gs web app, which does the Odoo write.
# HMAC-signed + time-limited so only our cards can trigger an action.
# Signing convention MUST match OdooAction.gs: msg = "action|event|stage|exp".
# ---------------------------------------------------------------------------
def _sign(secret, action, event, stage, exp):
    msg = f"{action}|{event}|{stage}|{exp}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def action_buttons(event_id, action_url, secret, ttl_days=7):
    """Signed Mark-paid + Change-stage buttons. Empty list if unconfigured."""
    if not (action_url and secret and event_id):
        return []
    exp = int(time.time()) + ttl_days * 86400

    def link(action, stage=""):
        sig = _sign(secret, action, event_id, stage, exp)
        url = f"{action_url}?action={action}&event={event_id}&exp={exp}&sig={sig}"
        if stage != "":
            url += f"&stage={stage}"
        return url

    return [
        {"text": "✅ Mark paid", "onClick": {"openLink": {"url": link("paid")}}},
        {"text": "📋 Change stage", "onClick": {"openLink": {"url": link("menu")}}},
    ]


def booking_card(b, base_url=""):
    """A full cardsV2 card for ONE booking.

    Heading (as requested): title = time · date · suburb;
    subtitle = package · duration · detailer. Body = labelled rows + buttons.
    """
    dur = f"{b['duration']:.1f}h" if b["duration"] else ""
    pkg = b["package"] or ""
    addons = _addons(b)
    heading_pkg = _heading_pkg(b)
    det = _short_detailer(b["detailer"])
    # MAIN HEADING: suburb · date · time · package(+add-ons) · duration · (detailer)
    title = " · ".join(x for x in (b["suburb"], b["date"], b["time"],
                                   heading_pkg, dur) if x) or "Booking"
    if det:
        title += f" · ({det})"

    # Customer | Mobile side by side (each label above its value), spaced across the row
    cust = f"<b>{b['customer']}</b>"
    if b["phone"]:
        widgets = [_two_col("Customer", cust, "Mobile", b["phone"])]
    else:
        widgets = [_row("Customer", cust)]
    if b["location"]:
        widgets.append(_row("Address", b["location"]))
    if addons:
        # Type | Package share one row -> lets Add-ons come up a row
        if b["vtype"]:
            widgets.append(_two_col("Type", b["vtype"], "Package", pkg))
        else:
            widgets.append(_row("Package", pkg))
        widgets.append(_row("Add-ons", " + ".join(addons)))
    elif b["vtype"]:
        widgets.append(_row("Type", b["vtype"]))
    if b["detailer"]:
        widgets.append(_row("Detailer", b["detailer"]))   # repeated in body, as requested

    buttons = []
    if base_url and b["event_id"]:
        url = f"{base_url}/web#id={b['event_id']}&model=calendar.event&view_type=form"
        buttons.append({"text": "Open in Odoo", "onClick": {"openLink": {"url": url}}})
    if b["phone"]:
        tel = re.sub(r"[^\d+]", "", b["phone"])
        buttons.append({"text": "📞 Call", "onClick": {"openLink": {"url": f"tel:{tel}"}}})
    if b["maps_query"]:
        q = urllib.parse.quote(b["maps_query"])
        buttons.append({"text": "🧭 Directions",
                        "onClick": {"openLink": {"url": f"https://www.google.com/maps/search/?api=1&query={q}"}}})
    for extra in (b.get("action_buttons") or []):   # Tier 3 buttons (Mark paid / Change stage)
        buttons.append(extra)
    if buttons:
        widgets.append({"buttonList": {"buttons": buttons}})

    return {
        "cardId": f"booking-{b['event_id']}",
        "card": {"header": {"title": title},
                 "sections": [{"widgets": widgets}]},
    }


# ---------------------------------------------------------------------------
# Notification (collapsed) text lines — the "📋 …" line shown above the card,
# in the notification popup and the inbox preview row.
# ---------------------------------------------------------------------------
def booking_led_text(b):
    """Single NEW booking: suburb · date · package(+add-ons) · duration · detailer."""
    dur = f"{b['duration']:.1f}h" if b.get("duration") else ""
    parts = [b.get("suburb"), b.get("date"), _heading_pkg(b), dur, _short_detailer(b.get("detailer"))]
    return "📋 " + " · ".join(x for x in parts if x)


def summary_text(detailer_name, date_display, bookings):
    """Daily summary: <Detailer> — Summary for <date> · N jobs · Xh · from <earliest> · <first suburb>."""
    det = _short_detailer(detailer_name) or detailer_name
    bk = sorted(bookings, key=lambda x: x.get("start") or "")
    n = len(bk)
    total = sum((x.get("duration") or 0) for x in bk)
    bits = [f"{n} job" + ("s" if n != 1 else ""), f"{total:.1f}h"]
    if bk and bk[0].get("time"):
        bits.append(f"from {bk[0]['time']}")
    if bk and bk[0].get("suburb"):
        bits.append(bk[0]["suburb"])
    return f"📋 {det} — Summary for {date_display} · " + " · ".join(bits)


def day_message(text, bookings, base_url=""):
    """One Chat message: a pre-built notification line + one card PER booking."""
    return {
        "text": text,
        "cardsV2": [booking_card(b, base_url) for b in bookings],
    }


def no_jobs_message(detailer_name, date_display):
    det = _short_detailer(detailer_name) or detailer_name
    title = f"{det} — {date_display}"
    return {
        "text": f"📋 {det} — Summary for {date_display} · No jobs scheduled ✅",
        "cardsV2": [{"cardId": re.sub(r"[^a-zA-Z0-9]+", "-", title)[:60],
                     "card": {"header": {"title": title, "subtitle": "No jobs scheduled ✅"},
                              "sections": [{"widgets": [{"textParagraph": {"text": "Enjoy the day off. 🚗✨"}}]}]}}],
    }
