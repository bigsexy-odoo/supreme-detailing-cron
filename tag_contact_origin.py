"""
Tag every contact with WHERE IT CAME FROM, so provenance is visible in the list.

Odoo records provenance but never surfaces it. This turns three buried signals into one
readable tag:

    create_uid = Public user   -> the visitor did it themselves on the website
    create_uid = <admin/API>   -> you, or one of our scripts (the API key authenticates
                                  as that user, so RPC and manual look identical)
    create_uid = OdooBot       -> Odoo's own install-time seed rows
    + a linked sale.order      -> they actually bought, not just signed up

Tags applied (created on first run if missing):

    Website booking   self-service on the site AND has an order   -> a real customer
    Website signup    self-service, NO order                      -> account made, never booked
    Added manually    created internally by you or a script
    Founders          Alex / Kade (a child of the company with a @supremedetailing.co.nz email)
    Company           the company records themselves

RULES THIS OBEYS (it runs unattended against live data):
  * NEVER removes a tag it didn't put there, and never touches non-origin tags.
  * Skips any contact that ALREADY carries an origin tag -- so if you re-tag someone by
    hand, the cron leaves your decision alone.
  * ONE exception, and it's an upgrade not a stomp: "Website signup" -> "Website booking"
    once that person places their first order. That is the transition worth seeing.
  * Never writes to archived contacts or to portal/system seed rows.

Dry-run by default; the workflow passes --commit.

    python tag_contact_origin.py              # preview
    python tag_contact_origin.py --commit     # apply
"""

import argparse
import io
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

# The public/portal template users -- a partner created BY one of these was created by a
# website visitor acting for themselves, not by us.
PUBLIC_UIDS = {3, 4}
# Odoo's own seed rows. Never tag these.
SEED_UIDS = {1}

T_BOOKING = "Website booking"
T_SIGNUP = "Website signup"
T_MANUAL = "Added manually"
T_FOUNDER = "Founders"
T_COMPANY = "Company"

# name -> colour index used when the tag has to be created
TAG_COLOURS = {T_BOOKING: 10, T_SIGNUP: 4, T_MANUAL: 3, T_FOUNDER: 9, T_COMPANY: 1}
ORIGIN_TAGS = set(TAG_COLOURS)

COMPANY_PARTNER_IDS = {1, 3}          # Supreme Detailing + Supreme Bookings
STAFF_EMAIL_DOMAIN = "@supremedetailing.co.nz"


def ensure_tags(c, commit):
    """Resolve every origin tag to an id, creating any that don't exist yet."""
    ids = {}
    for name, colour in TAG_COLOURS.items():
        found = c.call("res.partner.category", "search_read", [["name", "=", name]], fields=["id"])
        if found:
            ids[name] = found[0]["id"]
        elif commit:
            r = c.call("res.partner.category", "create", [{"name": name, "color": colour}])
            ids[name] = r[0] if isinstance(r, list) else r   # create returns a LIST over XML-RPC
            print(f"  created tag {name!r} -> {ids[name]}")
        else:
            print(f"  [DRY-RUN] would create tag {name!r}")
    return ids


def classify(c, p):
    """Return the origin tag this contact should carry, or None to leave it alone."""
    creator = (p.get("create_uid") or [0])[0]
    # Company first: these two ARE created by OdooBot at install, but they're the business,
    # not seed junk -- checking SEED_UIDS first would silently leave them untagged forever.
    if p["id"] in COMPANY_PARTNER_IDS:
        return T_COMPANY
    if creator in SEED_UIDS:
        return None                                   # Odoo's own seed rows
    if p.get("parent_id") and STAFF_EMAIL_DOMAIN in (p.get("email") or ""):
        return T_FOUNDER
    if creator in PUBLIC_UIDS:
        has_order = c.call("sale.order", "search_count", [["partner_id", "=", p["id"]]])
        return T_BOOKING if has_order else T_SIGNUP
    return T_MANUAL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually write (default: preview)")
    args = ap.parse_args()
    commit = args.commit

    c = OdooClient()
    tag_ids = ensure_tags(c, commit)
    by_id = {v: k for k, v in tag_ids.items()}

    partners = c.call(
        "res.partner", "search_read", [["active", "=", True]],
        fields=["id", "name", "email", "create_uid", "parent_id", "category_id"],
    )

    tagged = upgraded = skipped = 0
    for p in partners:
        want = classify(c, p)
        if not want:
            continue
        current = [by_id[t] for t in (p.get("category_id") or []) if t in by_id]

        if current:
            # Already carries an origin tag -> respect it, with one exception: a signup
            # that has since ordered becomes a booking.
            if T_SIGNUP in current and want == T_BOOKING:
                if commit:
                    c.call("res.partner", "write", [p["id"]],
                           {"category_id": [(3, tag_ids[T_SIGNUP]), (4, tag_ids[T_BOOKING])]})
                print(f"  UPGRADE {p['name']!r}: {T_SIGNUP} -> {T_BOOKING} (first order placed)")
                upgraded += 1
            else:
                skipped += 1
            continue

        if commit and tag_ids.get(want):
            c.call("res.partner", "write", [p["id"]], {"category_id": [(4, tag_ids[want])]})
        print(f"  TAG     {p['name']!r} -> {want}")
        tagged += 1

    verb = "tagged" if commit else "would tag"
    print(f"\n{verb} {tagged}, upgraded {upgraded}, left alone {skipped} "
          f"(of {len(partners)} active contacts)")
    if not commit:
        print("DRY RUN -- nothing written. Pass --commit to apply.")


if __name__ == "__main__":
    main()
