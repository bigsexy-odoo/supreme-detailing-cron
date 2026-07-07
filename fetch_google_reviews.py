"""Daily: pull Supreme Detailing's Google reviews and refresh the top ribbon
(view 3497 `website.sd_nav_testimonial`) with the latest ones — KEEPING its
format/rotator unbroken, only swapping the review text/author/date.

Stdlib-only. Dry-run by default; pass --commit to write.

Config (env vars / GitHub secrets; ODOO_* also read from SupremeDetailing/.env):
  GOOGLE_PLACES_API_KEY   restricted Places API (New) key
  GOOGLE_PLACE_ID         ChIJ… id (Supreme Detailing = ChIJpfg1QARJDW0ReDFACMMOlw8)
  ODOO_URL / ODOO_DB / ODOO_USER / ODOO_API_KEY
"""
import argparse
import os
import sys
from pathlib import Path

from odoo_client import OdooClient, cfg
import reviews_common as rc

RIBBON_VIEW = 3497       # website.sd_nav_testimonial  (top band, every page)
CAROUSEL_VIEWS = [585, 2388]  # homepage #reviews + Services page s_quotes carousels
RIBBON_MS = 5000         # top ribbon rotator: 5s
CAROUSEL_MS = 10000      # carousels: 10s (more text, slower)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--min-rating", type=int, default=5,
                    help="only surface reviews at/above this star rating (default 5)")
    args = ap.parse_args()

    api_key = cfg("GOOGLE_PLACES_API_KEY")
    place_id = cfg("GOOGLE_PLACE_ID")

    print(f"Fetching reviews for place {place_id} …")
    data = rc.fetch_reviews(api_key, place_id)
    all_reviews = data["reviews"]
    reviews = [r for r in all_reviews if r["rating"] >= args.min_rating] or all_reviews
    print(f"  {data['rating']}★ from {data['count']} ratings; "
          f"{len(all_reviews)} review texts, {len(reviews)} at ≥{args.min_rating}★")
    for r in reviews[:6]:
        print(f'   - {r["rating"]}★ {r["author"]} ({r["date"] or r["when"]}): '
              f'"{rc.short_snippet(r["text"])}"')

    if not reviews:
        print("No reviews to publish — leaving ribbon unchanged.")
        return 0

    c = OdooClient()
    langs = [l["code"] for l in c.call("res.lang", "search_read",
                                       [["active", "=", True]], fields=["code"])]
    bdir = Path(__file__).resolve().parent / "backups"
    bdir.mkdir(exist_ok=True)

    def refresh(view_id, label, transform):
        arch = c.call("ir.ui.view", "read", [view_id], fields=["arch_db"],
                      context={"lang": langs[0]})[0]["arch_db"]
        try:
            new_arch = transform(arch)
        except RuntimeError as e:
            print(f"  [{label}] skipped: {e}")
            return
        if new_arch == arch:
            print(f"  [{label}] already up to date.")
            return
        if not args.commit:
            print(f"  [{label}] would update (dry-run).")
            return
        (bdir / f"view{view_id}-{label}-pre.html").write_text(arch, encoding="utf-8")
        for code in langs:
            c.call("ir.ui.view", "write", [view_id], {"arch_db": new_arch},
                   context={"lang": code})
        print(f"  [{label}] committed ({langs}).")

    refresh(RIBBON_VIEW, "ribbon",
            lambda a: rc.update_ribbon_arch(a, reviews, interval_ms=RIBBON_MS))
    for vid in CAROUSEL_VIEWS:
        refresh(vid, f"carousel-{vid}",
                lambda a: rc.update_carousel_arch(a, reviews, interval=CAROUSEL_MS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
