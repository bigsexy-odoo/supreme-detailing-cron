# Cart bookings → Odoo Appointments — deploy runbook

**Problem this solves:** a completed cart purchase used to record the *order* in Odoo but
**nothing about the booking** (date/time/suburb/detailer). The slot lived only in the
customer's browser, so it never became an appointment on Alex/Kade's calendar and the
customer never got a real booking confirmation.

**How it's fixed (purely additive — no UI change, variants/pricing/time-logic untouched):**
1. **Capture** — the cart attaches the booking to the order *line* as a hidden custom
   attribute value (`SDBK1|...`), the only channel that persists for a public visitor
   (same mechanism as the gift flow).
2. **Convert** — this external cron reads paid orders' `SDBK1` lines and creates the real
   `calendar.event` on the correct detailer + fires the native "Appointment Booked" email +
   reminders. Runs outside Odoo over the API → **zero billable Odoo LoC**.

`SDBK1` format (one per booked service line):
`SDBK1|<date>|<time24>|<durationHours>|<apptTypeId>|<resourceName>|<suburb>|<serviceLabel>`

---

## ✅ Already done + PROVEN (2026-07-08, autonomous)

- `sync_bookings.py`, `undo_booking_event.py`, `setup_booking_attribute.py`,
  `teardown_booking_attribute.py`, `.github/workflows/sync-bookings.yml` — written + committed.
- **End-to-end proven** on test order **S00088** (via the name-fallback test channel):
  created `calendar.event` on **Kade** (resource 2), start `2026-07-18 01:00 UTC` = 1pm NZST
  (DST-correct), 3h, type 3, `booked`, alarms [3,6], attendee = customer, capacity reserved,
  description carries the booking summary + bounded idempotency marker.
  Order got audit token `SDCAL:L366=E4`. **Re-run correctly SKIPPED** (no duplicate).
  Template 37 (booking email) sent OK to the owner's own address. Then fully **undone** —
  DB back to clean.
- **Two bugs found + fixed by testing:** (a) `res.partner` has no `mobile` field on this DB →
  use `phone`; (b) resource must be set via `booking_line_ids` (with `capacity_reserved`),
  not the `appointment_resource_ids` m2m directly.
- The scheduled workflow is **INERT** until you flip it on (see Step 3) — safe to have landed.

---

## ⏳ Remaining — do these AWAKE (they touch the live shop / send email)

### Step 1 — Capture attribute (live product config, ~5 min, reversible)
The only piece that writes product config. The script **aborts** if it would disturb any
variant or appointment link.

```bash
cd cloud-cron
python setup_booking_attribute.py --only-tmpl 2            # dry-run, read the 2 [PROOF] lines
python setup_booking_attribute.py --only-tmpl 2 --commit   # ONE template first
#   MUST print: [PROOF] variant ids + list_prices IDENTICAL  AND  appt.type->product links intact
#   Sanity: Odoo → appointment type "Exterior Detail" product is still [SD-PKG-EXT-CAR] (34).
python setup_booking_attribute.py --commit                 # roll the other four
#   Copy the printed SD_BOOKING_PTAV map — needed for the cart wiring.
```
Undo: `python teardown_booking_attribute.py --commit`.

> **Coverage decision:** `BOOKABLE_TMPLS = [2,3,4,5,7]` (Exterior/Interior/Supreme + 2 bundles).
> Add-on types (Paint Protection/Clay/Headlight/Pet Hair → tmpls 9/8/11/10) and the Combo
> packages are **not** captured. If any of those can be a booking's **primary** service in
> `/appointment`, add them to `BOOKABLE_TMPLS` and re-run. The cron fails safe on unknown types.

### Step 2 — Cart wiring + UI hide (live `ir.ui.view` 2421 — Michael + me together)
Additive JS only; **back up view 2421 first** (Rule 6). Two hide-blocks keep the `SDBK1`
string invisible on the product page and cart line (honours the no-UI-change constraint).
The exact edit depends on the `SD_BOOKING_PTAV` map from Step 1 — **I'll build + apply this
with you watching a real product/cart/checkout page.** Do NOT go live on the cart before the
product-page hide JS is in, or a stray "Booking" field shows on the 5 package pages.

Then place ONE real Kade order → confirm the `SDBK1` custom value landed on its
`sale.order.line` (dry-run `sync_bookings.py --only-order <that order>`).

### Step 3 — Turn the cron on (supervised first run)
```bash
# from the repo, once Steps 1–2 verified:
git push                                    # land the workflow on GitHub
# GitHub → Settings → Secrets and variables → Actions → Variables → New:
#   SYNC_BOOKINGS_ENABLED = true            # scheduled runs stay DRY-RUN until this is 'true'
# First supervised pass (watch the emails), throttled:
python sync_bookings.py --commit --max 3 --verbose
```

---

## CRM step (added 2026-07-09, live)

Every pass also runs `ensure_crm_opps()`: ONE opportunity per booked (eligible) order —
team by detailer (Alex→North 4, Kade→Central 5), `expected_revenue` = order total, stage
**Booked (Unpaid)** (5) vs **Booked** (6) by payment state, tag "Appointment Booking",
linked to the order (`opportunity_id`) and its calendar events. Because it runs every
cycle, a `sent→sale` transition **auto-promotes** the stage. Guardrails: only upgrades
stages currently at New/Booked (Unpaid) (never touches human-moved stages), leaves
ARCHIVED opps completely alone (archive = the way to hide test opps — deleting one would
make the cron recreate it), adopts a pre-existing event-linked opp instead of duplicating.
`appointment.type.lead_create` is now OFF everywhere (the thin $0 auto-opps are gone) —
rollback map in `odoo-rpc/backups/20260709-appointment-lead-create.json`.

Also every pass: the **blank-booking watchdog** — any eligible order whose hidden Booking
value is blank/malformed (a slot lost client-side) gets a one-time Google Chat alert
(`SDNOBK:L<line>` order-ref token = already alerted).

## Flags / safety (built in)
- `--commit` required to act; **dry-run is default**. `--no-email` = create event, no email.
- Only **paid** states (`sale`,`done`). `sent` quotes are UNPAID here → opt-in `--include-sent`.
- Past bookings skipped (no spam); `--include-past` = event only, no email/sms.
- SMS reminder OFF (burns IAP credits); `--with-sms` re-adds it when the booker has a mobile.
- Slot-overlap guard, qty>1 warning, `--max N` throttle, bounded idempotency (no dupes).

## Rollback
- One event: `python undo_booking_event.py --event-id N --commit`
- All from an order: `python undo_booking_event.py --order S000NN --commit`
- Capture attribute: `python teardown_booking_attribute.py --commit`
- Cart view: restore the view-2421 backup taken in Step 2.
- Cron: unset `SYNC_BOOKINGS_ENABLED` (or delete the variable) → scheduled runs go inert.
