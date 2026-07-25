"""Pull ALL Supreme Detailing Google reviews via the Business Profile API (My Business v4) and
refresh the ribbon (view 3497) + carousels (585/2388) with the latest — newest first, NOT capped
at 5 like the Places API. Replaces fetch_google_reviews.py once GBP access is approved.

Stdlib-only on the runner: refreshes the OAuth access token with a plain urllib POST (no pip),
then calls the My Business APIs. Config (GitHub secrets / env / SupremeDetailing/.env):
  GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN   (from get_gbp_token.py)
  GBP_ACCOUNT_ID, GBP_LOCATION_ID   (OPTIONAL — auto-discovered if unset)
  ODOO_URL / ODOO_DB / ODOO_USER / ODOO_API_KEY

  python cloud-cron/fetch_gbp_reviews.py [--commit] [--min-rating 5]

NB: the account/location cross-version dance (v1 for accounts/locations, v4 for reviews) is
Google's, not ours — VERIFY the first live run's discovery output and pin GBP_ACCOUNT_ID /
GBP_LOCATION_ID as secrets if there's more than one listing.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from odoo_client import OdooClient, cfg
import reviews_common as rc

RIBBON_VIEW = 3497
CAROUSEL_VIEWS = [585, 2388]
RIBBON_MS, CAROUSEL_MS = 5000, 10000
RIBBON_MAX, CAROUSEL_MAX = 15, 12   # playback pool — well past the old 5
STAR = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def opt(key):
    try:
        return cfg(key)
    except Exception:
        return ""


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GBP HTTP {e.code} on {url.split('?')[0]}: {e.read().decode()[:300]}") from e


def access_token():
    body = urllib.parse.urlencode({
        "client_id": cfg("GBP_CLIENT_ID"), "client_secret": cfg("GBP_CLIENT_SECRET"),
        "refresh_token": cfg("GBP_REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=30)
        return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token refresh HTTP {e.code}: {e.read().decode()[:300]}") from e


def discover(token):
    acc = opt("GBP_ACCOUNT_ID")
    if not acc:
        accs = _get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts", token).get("accounts", [])
        if not accs:
            raise RuntimeError("no GBP accounts for this login — is it a Manager/Owner of the listing?")
        acc = accs[0]["name"].split("/")[-1]
        print(f"  account: {accs[0].get('accountName')} ({acc})  [{len(accs)} total]")
    loc = opt("GBP_LOCATION_ID")
    if not loc:
        url = (f"https://mybusinessbusinessinformation.googleapis.com/v1/accounts/{acc}"
               f"/locations?readMask=name,title&pageSize=100")
        locs = _get(url, token).get("locations", [])
        if not locs:
            raise RuntimeError("no locations for this account")
        pick = next((l for l in locs if "supreme" in (l.get("title", "").lower())), locs[0])
        loc = pick["name"].split("/")[-1]
        print(f"  location: {pick.get('title')} ({loc})  [{len(locs)} total]")
    return acc, loc


def fetch_all_reviews(token, acc, loc):
    reviews, page = [], None
    while True:
        url = f"https://mybusiness.googleapis.com/v4/accounts/{acc}/locations/{loc}/reviews?pageSize=50"
        if page:
            url += "&pageToken=" + page
        d = _get(url, token)
        for r in d.get("reviews", []):
            text = (r.get("comment") or "").strip()
            if not text:
                continue
            pt = r.get("createTime") or r.get("updateTime") or ""
            date = ""
            if pt:
                try:
                    date = datetime.fromisoformat(pt.replace("Z", "+00:00")).strftime("%b %Y")
                except ValueError:
                    date = ""
            reviews.append({
                "rating": STAR.get(r.get("starRating"), 5), "text": text,
                "author": (r.get("reviewer") or {}).get("displayName", "Google user"),
                "date": date, "when": "", "publishTime": pt})
        page = d.get("nextPageToken")
        if not page:
            break
    reviews.sort(key=lambda r: r.get("publishTime", ""), reverse=True)   # newest first
    return reviews


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--min-rating", type=int, default=5)
    args = ap.parse_args()

    tok = access_token()
    acc, loc = discover(tok)
    allr = fetch_all_reviews(tok, acc, loc)
    reviews = [r for r in allr if r["rating"] >= args.min_rating] or allr
    print(f"  {len(allr)} reviews total, {len(reviews)} at >={args.min_rating}star")
    for r in reviews[:8]:
        print(f'   - {r["rating"]}* {r["author"]} ({r["date"]}): "{rc.short_snippet(r["text"])}"')
    if not reviews:
        print("no reviews — leaving views unchanged")
        return 0

    c = OdooClient()
    langs = [l["code"] for l in c.call("res.lang", "search_read", [["active", "=", True]], fields=["code"])]
    bdir = Path(__file__).resolve().parent / "backups"
    bdir.mkdir(exist_ok=True)

    def refresh(vid, label, transform):
        arch = c.call("ir.ui.view", "read", [vid], fields=["arch_db"], context={"lang": langs[0]})[0]["arch_db"]
        try:
            new = transform(arch)
        except RuntimeError as e:
            print(f"  [{label}] skipped: {e}")
            return
        if new == arch:
            print(f"  [{label}] already up to date.")
            return
        if not args.commit:
            print(f"  [{label}] would update (dry-run).")
            return
        (bdir / f"view{vid}-{label}-pre.html").write_text(arch, encoding="utf-8")
        for code in langs:
            c.call("ir.ui.view", "write", [vid], {"arch_db": new}, context={"lang": code})
        print(f"  [{label}] committed.")

    refresh(RIBBON_VIEW, "ribbon",
            lambda a: rc.update_ribbon_arch(a, reviews, interval_ms=RIBBON_MS, max_items=RIBBON_MAX))
    for vid in CAROUSEL_VIEWS:
        refresh(vid, f"carousel-{vid}",
                lambda a: rc.update_carousel_arch(a, reviews, interval=CAROUSEL_MS, max_items=CAROUSEL_MAX))
    return 0


if __name__ == "__main__":
    sys.exit(main())
