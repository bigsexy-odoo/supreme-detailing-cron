# Supreme Detailing — cloud cron

Externalised Odoo automation that used to live in Odoo as `Execute Code` server
actions (billable "Maintenance of Customizations" LoC). Moved here so Odoo carries
**zero** billable custom code. Runs on **GitHub Actions** via the Odoo external API.

See `../MIGRATION_billable-loc.md` for the full plan, and the global
`odoo-chat-alerts` / `odoo-webform-crm` skills for the patterns.

## What runs here

| Script | Replaces | Schedule | Does |
|---|---|---|---|
| `route_leads_external.py` | server action #715 + cron 56 | every 15 min, NZ 9am–9pm | route web leads / orders / bookings to North(4)/Central(5)/Triage(1); newsletter opt-in; (Chat alert per lead once `GCHAT_WEBHOOK_URL` is set) |
| `loyalty_maintenance_external.py` | server action #760 + cron 62 | daily 13:00 UTC | 12-month card expiry + membership credit per posted membership invoice |
| `chat_poster.py` | (part of #562 replacement) | imported | Google Chat poster (`lead_to_message` / `post_to_chat`) |
| `odoo_client.py` | — | imported | XML-RPC client; reads creds from env (secrets) then `.env` |

Stdlib-only — **no `pip install`** on the runner.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `ODOO_URL` | `https://www.supremedetailing.co.nz` |
| `ODOO_DB` | `supremedetailing` |
| `ODOO_USER` | the API-key user's login (admin) |
| `ODOO_API_KEY` | Odoo → My Profile → Account Security → New API Key |
| `GCHAT_WEBHOOK_URL` | the Google Chat space incoming-webhook URL (for alerts) |

⚠️ Set secrets by **piping**, never `--body -` (that stores the literal `"-"`):
```bash
printf '%s' "$VALUE" | gh secret set ODOO_API_KEY -R <owner>/<repo>
```

## Local testing

```bash
# from this folder, with a local .env (gitignored) holding the same keys:
python route_leads_external.py --dry-run --verbose
python loyalty_maintenance_external.py --dry-run --verbose
```

## First deploy

```bash
gh repo create <owner>/supreme-detailing-cron --private --source . --remote origin --push
# then add the 5 secrets above, then trigger:
gh workflow run "Route leads & orders"
gh workflow run "Loyalty maintenance"
```

## Cutover (only AFTER cloud runs are verified)

Delete the in-Odoo code so the billable LoC drops to 0 (deactivating is NOT enough):
cron 56 + actions 715/716, cron 62 + actions 760/761, action 562 + automation rule #1.
Then re-check the Count LoC wizard → Billable = 0.
