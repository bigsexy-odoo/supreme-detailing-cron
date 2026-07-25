# Google Business Profile reviews — setup runbook (replaces the Places 5-limit)

Goal: pull **all** Supreme Detailing Google reviews (incl. brand-new ones, real-time) and feed the
existing ribbon (view 3497) + carousels (585/2388). Replaces `fetch_google_reviews.py` (Places API,
max 5). Once this is live, retire the Places workflow.

Reviews live in the **Google My Business API v4** (`mybusiness.googleapis.com/v4/.../reviews`);
accounts/locations come from the v1 Account-Management + Business-Information APIs.

---

## THE GATE (do this first — it's the multi-day wait)

The Business Profile APIs are **not open** — quota is **0 until Google approves an access request**.

**Access model (Michael does NOT own the profile — that's fine):** the *access request* + Cloud project
are YOURS (`mjnoone87`). The API *calls* just need a token from an account with **Manager or Owner**
rights on the Supreme Detailing listing. Two ways to get that — pick one:
- **(a) BEST — get added as a Manager:** whoever owns the profile signs in at
  <https://business.google.com> → the business → **Users / Managers** → **Add** `mjnoone87@gmail.com`
  as **Manager**. Then you OAuth as yourself and the automated token is yours (stable, you control it).
- **(b) if you just have the login:** do the one-time OAuth consent (step below) **signed in as the
  managing account** you can log into. Works, but the token then belongs to that account.

1. **Establish Manager access** via (a) or (b) above — confirm you (or an account you can log into) can
   see Supreme Detailing at <https://business.google.com>.
2. **Use the existing Google Cloud project** (the one that already holds the Places API key). Note its
   **Project number** (Cloud Console → top-left project picker → the number under the name).
3. **Enable these 3 APIs** (Cloud Console → APIs & Services → Library → search + Enable):
   - `Google My Business API`  ← the one with reviews
   - `My Business Account Management API`
   - `My Business Business Information API`
4. **Submit the access request:** <https://developers.google.com/my-business/content/prereqs> →
   "Request access" form. Fill in:
   - **Project number:** *(from step 2)*
   - **Use case:** "Display our own business's Google reviews on our own website (supremedetailing.co.nz)
     and keep the on-site review strip current. Read-only, single business, no resale."
   - Contact: your email.
   - → Google emails approval, usually a few days.

**Everything below can be prepped now; it only *runs* once access is approved.**

---

## OAuth (one-time, after approval)

1. Cloud Console → APIs & Services → **OAuth consent screen**: User type External; add scope
   `https://www.googleapis.com/auth/business.manage`; add yourself as a **Test user**.
2. APIs & Services → **Credentials** → Create credentials → **OAuth client ID** → **Desktop app** →
   download the JSON → save as `cloud-cron/gbp_client_secret.json` (gitignored).
3. Run the helper once (opens a browser, sign in as the business owner):
   ```
   python cloud-cron/get_gbp_token.py
   ```
   It prints `GBP_CLIENT_ID`, `GBP_CLIENT_SECRET`, `GBP_REFRESH_TOKEN`.
4. Add those 3 as **GitHub secrets** on `bigsexy-odoo/supreme-detailing-cron` (alongside the ODOO_* ones).

---

## Go live

- `python cloud-cron/fetch_gbp_reviews.py` (dry-run) → lists your reviews. `--commit` writes the views.
- Add `gbp-reviews.yml` (mirrors `reviews.yml`, same 2-hourly cadence) with the GBP_* secrets.
- Disable the old `reviews.yml` (Places) once GBP is confirmed.

## Notes
- The runner stays **stdlib-only**: the fetch refreshes the access token via a plain `urllib` POST to
  `oauth2.googleapis.com/token` (no pip on the runner). Only `get_gbp_token.py` (local, one-time) uses
  `google-auth-oauthlib`, which you already have.
- Auto-discovers the account + location; pin them with `GBP_ACCOUNT_ID` / `GBP_LOCATION_ID` secrets if
  you have more than one listing.
