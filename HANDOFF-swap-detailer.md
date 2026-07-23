# HANDOFF → one-tap "Swap detailer (Alex↔Kade)" on the Chat card

**From:** the /schedule-page session. **Goal:** a signed **🔁 Swap detailer** button on the
Google Chat booking card that flips a booking Alex↔Kade **consistently** (all 5 places), one tap.

**Why it's yours, not the /schedule page's:** the frontend QWeb `/schedule` page is sandboxed and
**cannot hold/read a secret** (confirmed: no config-param, no params, `t-esc` dropped) — so it can't
securely fire a write. The Chat card **can** (Python HMAC, exactly like the existing Mark-paid
button), and the endpoint + card + reassign tool all live in *your* files. Zero billable LoC.

## Architecture (reuse the proven tool — don't port it to JS)
`booking_card.py` (signed button) → `OdooAction.gs` `swap` action (HMAC-verify, resolve the OTHER
detailer) → **repository_dispatch** → a new `reassign.yml` workflow runs
**`reassign_detailer.py e<event> <Alex|Kade> --commit`** (already does all 5 places: lane,
booking-line hold, attendee/colour, title+description, SDBK1 source — idempotent).

Async (~15–30s for the Action to spin up) — fine for a detailer swap. Alternative if you want it
instant: do the 5 writes directly in `OdooAction.gs` via JSON-RPC (port `reassign_detailer.py`), but
that duplicates logic + the booking-line-model detection + SDBK1 order-line resolution — **dispatch
is cleaner.**

## The pieces

**1. `booking_card.py` — add a signed Swap button** (mirror `action_buttons()`; sign `swap|event||exp`):
```python
def swap_button(event_id, action_url, secret, ttl_days=7):
    if not (action_url and secret and event_id):
        return []
    exp = int(time.time()) + ttl_days * 86400
    sig = _sign(secret, "swap", event_id, "", exp)   # _sign already exists
    url = f"{action_url}?action=swap&event={event_id}&exp={exp}&sig={sig}"
    return [{"text": "🔁 Swap detailer", "onClick": {"openLink": {"url": url}}}]
```
Append its result to the card's button list next to Mark-paid / Change-stage.

**2. `OdooAction.gs` — add a `swap` action** (in `doGet`, verify already runs). It reads the event's
current resource, computes the OTHER detailer, dispatches, and shows a confirmation:
```javascript
if (p.action === 'swap') {
  var uid = login_();
  var ev = execKw_(uid, 'calendar.event', 'read', [[parseInt(p.event,10)], ['appointment_resource_ids']]);
  var cur = (ev && ev[0] && ev[0].appointment_resource_ids && ev[0].appointment_resource_ids[0]) || 0;
  var toName = (cur === 1) ? 'Kade' : 'Alex';           // 1=Alex,2=Kade -> the other
  dispatchReassign_(p.event, toName);                    // repository_dispatch (below)
  return page_('Swapping this booking to <b>' + toName + '</b> — done in a few seconds.', true);
}
```
Add a GH token Script Property (`GH_TOKEN`, a fine-grained PAT with **Actions: read/write** on
`bigsexy-odoo/supreme-detailing-cron`) and:
```javascript
function dispatchReassign_(eventId, detailer) {
  UrlFetchApp.fetch('https://api.github.com/repos/bigsexy-odoo/supreme-detailing-cron/dispatches', {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + PropertiesService.getScriptProperties().getProperty('GH_TOKEN'),
               Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ event_type: 'reassign-booking',
      client_payload: { event: String(eventId), detailer: detailer } }),
    muteHttpExceptions: true });
}
```
(Or route through the existing `OdooBookingRelay.gs`, which already holds `GH_TOKEN` + dispatches —
your call.)

**3. New workflow `.github/workflows/reassign.yml`:**
```yaml
name: Reassign booking detailer
on:
  repository_dispatch:
    types: [reassign-booking]
jobs:
  reassign:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - name: Reassign
        env:
          ODOO_URL: ${{ secrets.ODOO_URL }}
          ODOO_DB: ${{ secrets.ODOO_DB }}
          ODOO_USER: ${{ secrets.ODOO_USER }}
          ODOO_API_KEY: ${{ secrets.ODOO_API_KEY }}
        run: python reassign_detailer.py "e${{ github.event.client_payload.event }}" "${{ github.event.client_payload.detailer }}" --commit
```

## Notes / gotchas
- `reassign_detailer.py` is idempotent and resolves the order lines (for the SDBK1 fix) from the
  event via the `=E<id>` token on `client_order_ref` — make sure the sync writes that token (it does,
  `append_order_marker`).
- Resource→detailer: **1 = Alex (contact 69), 2 = Kade (contact 70)**.
- Verify like the other actions: `curl "$EXEC?action=swap&event=40&exp=$E&sig=$S"` (sign `swap|40||$E`)
  → should show "Swapping … to <other>" and fire the Action.
- The card's `action_url`/`secret` = the existing `SD_ACTION_URL` / `SD_ACTION_SECRET` (same as
  Mark-paid), so no new secret plumbing for the signing side — only the `GH_TOKEN` for dispatch.
