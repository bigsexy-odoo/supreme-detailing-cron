"""
UNDO for setup_booking_attribute.py.

Removes the "Booking" no_variant is_custom attribute, its "Custom" value, and
the attribute lines on templates 2,3,4,5,7 - restoring the templates to their
pre-change state. Backs up every record it deletes to backups/ first.

Order matters (Odoo cascade): delete the attribute LINES first (this removes the
auto-generated ptavs), then the value, then the attribute.

SAFETY GUARD: if any sale.order.line already references the Booking ptav via a
product.attribute.custom.value, the script ABORTS unless --force is given, so you
don't strip booking data off live orders by accident.

Usage:
    python teardown_booking_attribute.py           # DRY-RUN (default)
    python teardown_booking_attribute.py --commit  # actually delete
    python teardown_booking_attribute.py --commit --force  # delete even if orders reference it
"""

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

ATTR_NAME = "Booking"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Delete even if live order lines reference the Booking ptav")
    args = ap.parse_args()

    c = OdooClient()
    print(f"[connected] {c.url} db={c.db} uid={c.uid}")
    print(f"[mode] {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    attr = c.call("product.attribute", "search_read",
                  [["name", "=", ATTR_NAME], ["create_variant", "=", "no_variant"]],
                  fields=["id", "name"])
    if not attr:
        print("Booking attribute not found - nothing to undo.")
        return
    attr_id = attr[0]["id"]

    ptavs = c.call("product.template.attribute.value", "search_read",
                   [["attribute_id", "=", attr_id]],
                   fields=["id", "product_tmpl_id"])
    ptav_ids = [p["id"] for p in ptavs]
    lines = c.call("product.template.attribute.line", "search_read",
                   [["attribute_id", "=", attr_id]], fields=["id", "product_tmpl_id"])
    line_ids = [l["id"] for l in lines]
    vals = c.call("product.attribute.value", "search_read",
                  [["attribute_id", "=", attr_id]], fields=["id", "name", "is_custom"])
    val_ids = [v["id"] for v in vals]

    # guard: any live custom values referencing these ptavs?
    used = c.call("product.attribute.custom.value", "search_read",
                  [["custom_product_template_attribute_value_id", "in", ptav_ids]],
                  fields=["id"]) if ptav_ids else []
    print(f"attr={attr_id} lines={line_ids} value(s)={val_ids} ptavs={ptav_ids}")
    print(f"live order custom-values referencing Booking ptavs: {len(used)}")
    if used and not args.force:
        print("[ABORT] order lines reference the Booking ptav. Re-run with --force to delete anyway.")
        return

    # backup everything first
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = {
        "attribute": c.call("product.attribute", "read", [attr_id],
                            fields=["id", "name", "create_variant", "display_type"]),
        "values": vals,
        "lines": c.call("product.template.attribute.line", "read", line_ids,
                        fields=["id", "product_tmpl_id", "attribute_id", "value_ids"]) if line_ids else [],
        "ptavs": ptavs,
        "used_custom_values": used,
    }
    bpath = BACKUP_DIR / f"{stamp}-booking-attr-teardown.json"
    bpath.write_text(json.dumps(backup, indent=2, default=str), encoding="utf-8")
    print(f"[backup] {bpath}")

    if not args.commit:
        print("\n[DRY-RUN] would unlink lines -> value(s) -> attribute (in that order). Nothing written.")
        return

    if line_ids:
        c.call("product.template.attribute.line", "unlink", line_ids)
        print(f"[unlink] lines {line_ids}")
    if val_ids:
        c.call("product.attribute.value", "unlink", val_ids)
        print(f"[unlink] values {val_ids}")
    c.call("product.attribute", "unlink", [attr_id])
    print(f"[unlink] attribute {attr_id}")
    print("\nDone. Templates 2,3,4,5,7 restored to pre-change state.")


if __name__ == "__main__":
    main()
