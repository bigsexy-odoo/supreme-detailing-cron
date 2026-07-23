# HANDOFF → sync_bookings.py: write the vehicle-size Q5 answer at event creation

**From:** the /schedule-page session. **Why:** the staff `/schedule` page (and Odoo's
own appointment views/emails) show the vehicle size via the **native "Vehicle size"
appointment answer (Q5)**. Existing booked events were **backfilled** by
`odoo-rpc/backfill_vehicle_size.py` (Will ev40 + ev42 = Car, live). This makes it
**permanent for new bookings** by writing the Q5 answer when the sync creates the event —
so no backfill/parse-the-note is needed going forward. **Zero billable LoC** (an answer is data).

## Decision (already validated live)
- Vehicle size is stored as the native **Q5 "Vehicle size"** answer, NOT a custom field
  (custom/Studio field risks the Maintenance-of-Customizations fee) and NOT a reused field.
- The size comes straight from `sdbk['service_label']` (the parenthesised token, e.g.
  `"Supreme Detail Package (Car)"` → `Car`). No note-parsing needed — you already have it.
- Q5 question id = **5**. Answer ids: **Car=16, Station Wagon(/SW)=17, SUV=18, Van=19,
  Truck / Ute=20**.

## The change (in `sync_bookings.py`)

**1. Add near the other helpers (module level):**
```python
Q_VEHICLE_SIZE = 5
SIZE_ANSWER = {"car": 16, "station wagon": 17, "sw": 17, "suv": 18,
               "van": 19, "truck": 20, "ute": 20, "truck / ute": 20}

def size_answer_cmd(service_label, appt_type_id):
    """(0,0,{...}) create-command for the Q5 vehicle-size answer, or None.
    Size = the parenthesised token in the service label ('... (Car)' -> Car)."""
    for tok in re.findall(r"\(([^)]+)\)", service_label or ""):
        aid = SIZE_ANSWER.get(tok.strip().lower())
        if aid:
            return (0, 0, {"question_id": Q_VEHICLE_SIZE, "value_answer_id": aid,
                           "question_type": "select", "appointment_type_id": appt_type_id})
    return None
```
(`re` is already imported.)

**2. In the event-create path (the `vals = { … }` block ~line 1102, right before
`C.call("calendar.event", "create", vals, …)` ~line 1133):**
```python
    size_cmd = size_answer_cmd(sdbk["service_label"], sdbk["appt_type_id"])
    vals = {
        ...                      # existing keys unchanged
    }
    if size_cmd:
        vals["appointment_answer_input_ids"] = [size_cmd]
    event_id = C.call("calendar.event", "create", vals, context=NOISE_OFF)
```

## Notes
- Create-only, so no idempotency concern (the answer is written once with the event).
- Proven working: creating `appointment.answer.input` via the event o2m with
  `{question_id, value_answer_id, question_type:'select', appointment_type_id}` succeeds
  (tested → event 40 answer id 50 = Car).
- After this ships, `backfill_vehicle_size.py` is only needed for pre-existing bookings
  (already run); new bookings self-populate.
- Related: /schedule page = `odoo-rpc/create_schedule_page.py`; page renders the size via
  a `t-foreach` over `appointment_answer_input_ids` where `question_id.id == 5`.
