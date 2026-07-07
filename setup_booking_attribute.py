"""
Add a no_variant + is_custom "Booking" attribute to the 5 bookable detailing
templates so the custom cart can persist a machine-parseable booking string on
each sale.order.line (the ONLY public-writable channel).

Mirrors the EXISTING gift attributes (product.attribute 9-12 "Giftee *", each
create_variant='no_variant', display_type='radio', with a single is_custom
"Custom" value). Those prove a no_variant is_custom attribute attaches free text
to a sale.order.line WITHOUT generating any variant (gift templates 20-26 carry
4 such attrs each yet have exactly 1 variant).

SAFETY: adding a create_variant='no_variant' attribute line does NOT enter the
'always' cartesian product, so Odoo's _create_variant_ids() keeps the existing
Vehicle Type variants (product.product 34-38 / 39-43 / 44-48 / 49-53 / 54-58)
with their SAME ids and prices. This script proves it: it snapshots every
variant id + list_price BEFORE writing and re-reads them AFTER, asserting
equality. Any drift aborts loudly.

SAFETY GUARD (review fix): the BEFORE/AFTER variant snapshot now ABORTS (raises
SystemExit) on ANY drift instead of merely printing -- so if Odoo ever did
regenerate variants, --commit stops immediately and you notice before touching
the cart. It also re-reads the appointment.type -> product links and aborts if
any changed. RECOMMENDED first run: --only-tmpl 2 (prove it on ONE template,
eyeball [PROOF] IDENTICAL + appt.type 1 product still 34, THEN run all five).

Usage:
    python setup_booking_attribute.py                     # DRY-RUN all 5 templates
    python setup_booking_attribute.py --only-tmpl 2        # DRY-RUN one template
    python setup_booking_attribute.py --only-tmpl 2 --commit   # commit ONE template first (recommended)
    python setup_booking_attribute.py --commit            # commit all 5 templates
    python setup_booking_attribute.py --commit --verify-only   # just re-read + print the ptav map

Idempotent: safe to re-run. Search-before-create everywhere.
After --commit, copy the printed SD_BOOKING_PTAV map into the view-2421 cart JS.
"""

import argparse
import io
import json
import sys
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

# The 5 bookable service templates (SERVICE_DATA keys in view 2421 -> templates).
# Add-ons (8,9,10,11) are ONLY ever added as extras to a service, never booked
# standalone through the custom flow, so they do NOT need the Booking attribute.
BOOKABLE_TMPLS = [2, 3, 4, 5, 7]

# appointment.type -> product.template links that MUST survive (id: expected tmpl).
# If a variant regenerate ever renumbered product.product, these links would break.
APPT_TYPE_TMPL = {1: 2, 2: 3, 3: 4, 8: 5, 10: 7}

ATTR_NAME = "Booking"
VALUE_NAME = "Custom"

# Working set for this run (narrowed by --only-tmpl). snapshot_variants reads this.
TMPLS = list(BOOKABLE_TMPLS)


def uid1(res):
    """XML-RPC create returns [id]; unwrap to scalar."""
    return res[0] if isinstance(res, list) else res


def snapshot_variants(c):
    """Return {tmpl_id: [(variant_id, list_price), ...]} for the bookable templates."""
    snap = {}
    tmpls = c.call("product.template", "read", TMPLS,
                   fields=["id", "name", "product_variant_ids"])
    for t in tmpls:
        vids = sorted(t["product_variant_ids"])
        vs = c.call("product.product", "read", vids,
                    fields=["id", "default_code", "list_price"])
        snap[t["id"]] = sorted([(v["id"], v["list_price"]) for v in vs])
    return snap


def appt_type_snapshot(c):
    """Return {appt_type_id: product_id} for the links that MUST survive."""
    rows = c.call("appointment.type", "read", list(APPT_TYPE_TMPL.keys()),
                  fields=["id", "product_id"])
    return {r["id"]: (r["product_id"][0] if r.get("product_id") else None) for r in rows}


def main():
    global TMPLS
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--only-tmpl", type=int, default=None,
                    help="Run on ONE template id first (recommended for the first --commit)")
    ap.add_argument("--verify-only", action="store_true",
                    help="Skip creation; just read + print current ptav map + variant snapshot")
    args = ap.parse_args()

    if args.only_tmpl is not None:
        if args.only_tmpl not in BOOKABLE_TMPLS:
            raise SystemExit(f"--only-tmpl {args.only_tmpl} is not in BOOKABLE_TMPLS {BOOKABLE_TMPLS}")
        TMPLS = [args.only_tmpl]

    c = OdooClient()
    print(f"[connected] {c.url} db={c.db} uid={c.uid}")
    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"[mode] {mode}  templates={TMPLS}\n")

    # ---- appointment.type link snapshot (must survive) -------------------
    at_before = appt_type_snapshot(c)
    print(f"=== appt.type -> product BEFORE === {at_before}\n")

    # ---- BEFORE snapshot (proof variants are untouched) ------------------
    before = snapshot_variants(c)
    print("=== variant snapshot BEFORE ===")
    for t, rows in before.items():
        print(f"  tmpl {t}: {rows}")
    print()

    if args.verify_only:
        # just print existing Booking ptav map if the attribute already exists
        attr = c.call("product.attribute", "search_read",
                      [["name", "=", ATTR_NAME], ["create_variant", "=", "no_variant"]],
                      fields=["id"])
        if attr:
            _print_ptav_map(c, attr[0]["id"])
        else:
            print("Booking attribute not created yet.")
        return

    # ---- 1. Booking attribute --------------------------------------------
    attr = c.call("product.attribute", "search_read",
                  [["name", "=", ATTR_NAME], ["create_variant", "=", "no_variant"]],
                  fields=["id", "name", "create_variant", "display_type"])
    if attr:
        attr_id = attr[0]["id"]
        print(f"[attr] exists id={attr_id} (mirror of gift attrs 9-12)")
    else:
        vals = {"name": ATTR_NAME, "create_variant": "no_variant", "display_type": "radio"}
        if args.commit:
            attr_id = uid1(c.call("product.attribute", "create", [vals]))
            print(f"[attr] created id={attr_id} {vals}")
        else:
            attr_id = None
            print(f"[attr] WOULD create {vals}")

    # ---- 2. is_custom "Custom" value -------------------------------------
    val_id = None
    if attr_id:
        val = c.call("product.attribute.value", "search_read",
                     [["attribute_id", "=", attr_id], ["is_custom", "=", True]],
                     fields=["id", "name", "is_custom"])
        if val:
            val_id = val[0]["id"]
            print(f"[value] exists id={val_id} (is_custom=True)")
        else:
            vals = {"name": VALUE_NAME, "attribute_id": attr_id, "is_custom": True}
            if args.commit:
                val_id = uid1(c.call("product.attribute.value", "create", [vals]))
                print(f"[value] created id={val_id} {vals}")
            else:
                print(f"[value] WOULD create {vals}")
    else:
        print("[value] WOULD create is_custom 'Custom' value (needs attr first)")

    # ---- 3. attribute lines on each bookable template --------------------
    print("\n=== attribute lines ===")
    for tmpl in TMPLS:
        line = None
        if attr_id:
            line = c.call("product.template.attribute.line", "search_read",
                          [["product_tmpl_id", "=", tmpl], ["attribute_id", "=", attr_id]],
                          fields=["id", "value_ids"])
        if line:
            print(f"  tmpl {tmpl}: line exists id={line[0]['id']}")
        else:
            vals = {"product_tmpl_id": tmpl, "attribute_id": attr_id,
                    "value_ids": [(6, 0, [val_id])] if val_id else []}
            if args.commit and attr_id and val_id:
                lid = uid1(c.call("product.template.attribute.line", "create", [vals]))
                print(f"  tmpl {tmpl}: created line id={lid}")
            else:
                print(f"  tmpl {tmpl}: WOULD create line {vals}")

    # ---- 4. AFTER snapshot + ABORT on any variant drift ------------------
    print("\n=== variant snapshot AFTER ===")
    after = snapshot_variants(c)
    for t, rows in after.items():
        print(f"  tmpl {t}: {rows}")
    if after == before:
        print("\n[PROOF] variant ids + list_prices IDENTICAL before/after -> variants NOT regenerated.")
    else:
        print("\n[!!!] variant snapshot CHANGED:")
        for t in TMPLS:
            if before.get(t) != after.get(t):
                print(f"    tmpl {t}: {before.get(t)} -> {after.get(t)}")
        # teardown cannot restore renumbered variant ids -> stop hard.
        raise SystemExit("[ABORT] variants drifted -- appointment.type product links may be "
                         "broken. Investigate immediately; do NOT touch the cart JS.")

    # ---- 4b. appointment.type links must be unchanged --------------------
    at_after = appt_type_snapshot(c)
    print(f"\n=== appt.type -> product AFTER === {at_after}")
    if at_after != at_before:
        raise SystemExit(f"[ABORT] appointment.type product links changed "
                         f"{at_before} -> {at_after}")
    # Assert every link still points at the expected template's variant is not
    # required here (ids preserved => links preserved), but confirm none went null.
    for atid, pid in at_after.items():
        if pid is None:
            raise SystemExit(f"[ABORT] appointment.type {atid} lost its product link")
    print("[PROOF] appointment.type -> product links intact.")

    # ---- 5. print the ptav map for the cart JS ---------------------------
    if attr_id:
        _print_ptav_map(c, attr_id)


def _print_ptav_map(c, attr_id):
    """Print SD_BOOKING_PTAV = {tmpl: booking_ptav_id} for pasting into view 2421."""
    ptavs = c.call("product.template.attribute.value", "search_read",
                   [["attribute_id", "=", attr_id]],
                   fields=["id", "product_tmpl_id", "is_custom", "price_extra"])
    m = {}
    for p in ptavs:
        m[p["product_tmpl_id"][0]] = p["id"]
        assert p["is_custom"] is True, f"ptav {p['id']} not is_custom!"
        assert p["price_extra"] == 0.0, f"ptav {p['id']} price_extra != 0 ({p['price_extra']})"
    print("\n=== PASTE INTO view 2421 (SD_BOOKING_PTAV) ===")
    # Always print the FULL map (all templates the attr covers), not just this run's subset.
    ordered = {t: m[t] for t in BOOKABLE_TMPLS if t in m}
    print("var SD_BOOKING_PTAV = " +
          json.dumps(ordered).replace('"', "") + "; // {tmpl: booking ptav id}")
    print("(all is_custom=True, price_extra=0.0 -> zero price impact)")


if __name__ == "__main__":
    main()
