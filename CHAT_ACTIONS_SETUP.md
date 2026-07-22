# Tier 3 — "Act from Google Chat" (Mark paid / Change stage)

Booking cards (posted by `daily_summary.py` via `booking_card.py`) can carry two
extra buttons — **✅ Mark paid** and **📋 Change stage** — that write straight to
the linked CRM opportunity. Tapping a button opens `webhook/OdooAction.gs` (an
Apps Script web app) which verifies an HMAC-signed link and updates Odoo.

- **Mark paid** → moves the booking's opportunity to stage **Booked** (6).
- **Change stage** → a page of buttons for New / Booked (Unpaid) / Booked / Won.
- Every write is logged as a chatter note on the opportunity.
- **Zero billable Odoo LoC** (the write happens externally, over the API).

The booking → opportunity link is `calendar.event.opportunity_id` (direct field).

## Data facts (this instance)
- Stages: New=1, **Booked (Unpaid)=5**, **Booked=6**, Won=4.
- Acts as Odoo user `mjnoone87@gmail.com` (uid 2) via the existing API key.

## Deploy steps
1. **Create the Apps Script** (under `admin@supremedetailing.co.nz`):
   script.google.com → New project → paste all of `webhook/OdooAction.gs`.
2. **Project Settings → Script properties** — add these five (values are handed
   over in chat, kept out of git):
   - `ODOO_URL` = `https://www.supremedetailing.co.nz`
   - `ODOO_DB` = `supremedetailing`
   - `ODOO_USER` = `mjnoone87@gmail.com`
   - `ODOO_API_KEY` = *(the key in `SupremeDetailing/.env`)*
   - `SHARED_SECRET` = *(the 64-char secret handed over in chat)*
3. **Deploy → New deployment → Web app** — *Execute as: Me*, *Who has access:
   Anyone*. Authorise. Copy the `…/exec` URL.
4. **Wire the poster** — set two values (GitHub secrets on the cron repo **and**
   local `.env` for testing):
   - `SD_ACTION_URL` = the `…/exec` URL from step 3
   - `SD_ACTION_SECRET` = the **same** `SHARED_SECRET` from step 2
5. Re-run `daily_summary.py` — cards now include the two action buttons. If
   `SD_ACTION_URL`/`SD_ACTION_SECRET` are unset, the buttons simply don't appear
   (graceful — Open in Odoo / Call / Directions still work).

## Security
- Links are **HMAC-SHA256 signed** (`action|event|stage|exp`) with `SHARED_SECRET`
  and **expire** (card buttons: 7 days; the stage-menu links: 1 hour).
- The signing string + secret must match on both sides
  (`booking_card._sign()` ⟷ `OdooAction.hmacHex_()`).
- Only whitelisted stages (1/5/6/4) can be set. The Chat space is private to the
  team, so anyone in it can tap — that's the intended trust boundary.
- Rotate: change `SHARED_SECRET` in Script Properties **and** `SD_ACTION_SECRET`
  together; old card links stop working immediately.

## Reschedule — deferred (see notes in chat)
Not built. Changing a booking's date/time is a multi-step move (calendar.event +
resource availability re-check + no double-book + customer notice) — a project in
its own right, best done as a follow-up.
