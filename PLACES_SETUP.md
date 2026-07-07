# Google Places API — setup for the Google-reviews strip

Goal: a **daily** cron that pulls Supreme Detailing's Google rating + top reviews and bakes
an on-brand "happy customers" strip behind the site's `#reviews` anchor. This file is the
one-time setup checklist to get the **API key** + **Place ID**, and where to store them.

> Context: Supreme Detailing has **no existing Google Cloud project** — the email was set up
> with Google **App Passwords** (Workspace admin console), not OAuth, so it never created one.
> This is the business's first Cloud project. Do it under the **business** account.

---

## 0. Which Google account?

Log into [console.cloud.google.com](https://console.cloud.google.com) as
**`admin@supremedetailing.co.nz`** (the Workspace super-admin) — NOT a personal Gmail.
Keeps the project, key, and billing under the business.

**Renaming "My First Project":** you can change the display **name** (project selector → the
project → *Settings* → edit name → `Supreme Detailing`) but the **project ID** is permanent and
can't be changed — that's fine, the ID is cosmetic for our use. Only rename it if it shows up
under `admin@supremedetailing.co.nz`; if it's under your personal Gmail, create a fresh project
under admin@ instead.

---

## 1. Enable the API

APIs & Services → **Library** → search **"Places API (New)"** → **Enable**.
(Enable the *New* one — not the legacy "Places API".)

## 2. Attach billing

Billing → link a payment method to the project. Places returns nothing without billing.
A once-daily cron is ~30 calls/month, comfortably inside the free allowance → **≈ $0**.
Set a **budget alert at $1** (Billing → Budgets & alerts) for peace of mind.

## 3. Create + restrict the API key

APIs & Services → **Credentials → + Create Credentials → API key** → copy it, then **Edit**:

| Setting | Value | Why |
|---|---|---|
| **API restrictions** | *Restrict key* → **Places API (New)** only | key is useless if leaked to any other API |
| **Application restrictions** | **None** (server cron) | GitHub Actions egress IPs rotate, so IP-locking is impractical; the API restriction above is the real guard |

> If you ever run it from a fixed-IP host, switch Application restrictions to **IP addresses**
> and add that host's egress IP.

## 4. Find the Place ID

Google **Place ID Finder**
(developers.google.com/maps/documentation/places/web-service/place-id) → search
"Supreme Detailing" → copy the `ChIJ…` id. (Or use Text Search once the key works.)

---

## 5. Test the key (stdlib only — matches this repo's no-`pip` rule)

```python
import json, urllib.request

API_KEY  = "PASTE_KEY"
PLACE_ID = "PASTE_ChIJ..."

req = urllib.request.Request(
    f"https://places.googleapis.com/v1/places/{PLACE_ID}",
    headers={
        "X-Goog-Api-Key": API_KEY,
        # field mask is REQUIRED in Places API (New) — you must name every field you want
        "X-Goog-FieldMask": "id,displayName,rating,userRatingCount,reviews",
    },
)
data = json.loads(urllib.request.urlopen(req, timeout=20).read())
print(data.get("rating"), "avg /", data.get("userRatingCount"), "ratings")
for rev in data.get("reviews", []):
    who  = rev["authorAttribution"]["displayName"]
    text = rev.get("text", {}).get("text", "")
    print(f"— {rev['rating']}star  {who}: {text[:120]}")
```

PowerShell sanity-check (key only):

```powershell
$KEY = "PASTE_KEY"; $PLACE = "PASTE_ChIJ..."
curl.exe -s "https://places.googleapis.com/v1/places/$PLACE" `
  -H "X-Goog-Api-Key: $KEY" -H "X-Goog-FieldMask: rating,userRatingCount,reviews"
```

---

## 6. Where the key + Place ID go

**GitHub Actions secrets** (this repo → Settings → Secrets and variables → Actions).
Pipe the value — never `--body -` (stores the literal `-`):

```bash
printf '%s' "PASTE_KEY"     | gh secret set GOOGLE_PLACES_API_KEY -R <owner>/<repo>
printf '%s' "PASTE_ChIJ..." | gh secret set GOOGLE_PLACE_ID       -R <owner>/<repo>
```

For local testing, add the same two keys to this folder's gitignored `.env`:

```ini
GOOGLE_PLACES_API_KEY=...
GOOGLE_PLACE_ID=...
```

| Secret | Value |
|---|---|
| `GOOGLE_PLACES_API_KEY` | the restricted key from step 3 |
| `GOOGLE_PLACE_ID` | the `ChIJ…` id from step 4 |

---

## 7. The one hard limit (design constraint)

Places API returns **max 5 reviews**, and **you can't choose which 5** (Google serves "most
relevant") or page for more. Fine for a rotating "happy customers" strip. If you ever need
**all** reviews or to reply to them, that's the **Google Business Profile API** instead — OAuth
as the verified owner (Alex) + a Google access-request approval; much heavier. Not needed here.

---

## 8. Next step (once key + Place ID exist)

Build `fetch_google_reviews.py` in this folder (stdlib `urllib`, same shape as the other crons):
fetch rating + 5 reviews → bake a `window.__SD_REVIEWS` JSON island into `custom_code_head`
(marker-replace between `/*GR_START*/…/*GR_END*/`, idempotent); a one-time
`odoo-rpc/_push_social_proof_strip.py` renders the on-brand strip that reads that global — so it
lands behind the `#reviews` anchor with no client-side fetch/CORS/flash. Schedule daily via a
GitHub Actions workflow (clone `daily-summary.yml`, `cron: "0 14 * * *"` ≈ 2am NZ).
Google ToS: attribute ("Reviews from Google" + link to the listing), refresh daily (don't
permanently store). Full parked design in memory `todo-supreme-google-reviews-strip`.
