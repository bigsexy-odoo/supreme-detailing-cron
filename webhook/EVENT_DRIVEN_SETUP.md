# Event-driven booking sync — setup

Make a booking reserve its calendar slot **within seconds** of checkout instead of waiting up
to 15 min for the scheduled cron. Zero billable Odoo LoC (no-code automation + native webhook
action + external relay + external GitHub Actions).

```
checkout ─▶ sale.order 'sent'/'sale'
   └▶ Odoo Automation Rule (no-code) ─▶ Webhook action ─POST─▶ Apps Script relay
                                                                   └─repository_dispatch─▶ GitHub Actions ─▶ sync_bookings.py --commit
```

The 15-min cron stays as a safety net; the cron's slot-conflict guard stays as the ultimate
backstop (a genuine double-book is skipped, never duplicated).

## Your one-time steps

**1. Create a GitHub token**
GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate:
- Resource owner: `bigsexy-odoo`, Repository access: only `supreme-detailing-cron`
- Repository permissions → **Contents: Read and write** (this is what `repository_dispatch` needs)
- (or a classic token with the `repo` scope)
Copy the token.

**2. Deploy the relay** (`OdooBookingRelay.gs` in this folder)
- script.google.com → New project → paste `OdooBookingRelay.gs`.
- Project Settings → **Script Properties** → add two:
  - `GH_TOKEN` = the token from step 1
  - `RELAY_KEY` = any long random string (a shared secret)
- Deploy → New deployment → **Web app** → Execute as **Me**, Who has access **Anyone** → copy the `/exec` URL.

**3. Give Claude the `/exec` URL** — Claude runs `build_booking_webhook.py "<URL>?key=<RELAY_KEY>"`
to create the Odoo automation + webhook action pointing at your relay. Done.

## Test it
- Open the relay URL in a browser → should say *"SD booking relay live…"* (GET works).
- Do a test checkout → within seconds a **repository_dispatch** run appears in the repo's Actions
  tab, and the booked slot disappears from the shop's available times.

## Notes
- The relay only accepts POSTs carrying the correct `?key=` (so a random POST can't spin up runs).
- Every `sent`/`sale` transition fires one dispatch; the sync is idempotent, so extra fires are
  harmless (each is ~1 min of Actions time; well within the free tier for this volume).
- The event-driven run still commits ONLY when repo variable `SYNC_BOOKINGS_ENABLED == 'true'`.
