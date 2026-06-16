"""
EXTERNAL port of Odoo server action #760 "SD loyalty maintenance".
Runs OUTSIDE Odoo (GitHub Actions cron) via XML-RPC. Faithful port of
`server_action_loyalty_sd.py`. Debug output kept in.

Jobs:
  1. Set a 12-month expiry on package loyalty cards (programs 2/3/4) that have none.
  2. Issue +1 membership credit per posted membership invoice, idempotent via a
     high-water-mark config param `sd.membership.last_move` (max processed move id).

Usage:  python loyalty_maintenance_external.py [--dry-run] [--verbose]
Creds:  odoo_client.cfg() reads env vars first (GitHub secrets) then .env.
"""

import argparse
import datetime
import sys

from odoo_client import OdooClient

# ----- CONFIG (Supreme Detailing) - identical to the in-Odoo action -----
PACKAGE_PROGRAMS = [2, 3, 4]   # 6x Exterior / Interior / Supreme
MEMBERSHIP_PROGRAM = 5         # "Membership - Included Detail"
MEMBERSHIP_PRODUCT = 19        # Detail Membership product.template id
PARAM_KEY = "sd.membership.last_move"
# ------------------------------------------------------------------------

ARGS = None
C = None


def log(m):
    print(m, flush=True)


def vlog(m):
    if ARGS and ARGS.verbose:
        print("  . " + m, flush=True)


def search(model, domain, **kw):
    return C.call(model, "search", domain, **kw)


def search_read(model, domain, fields, **kw):
    return C.call(model, "search_read", domain, fields=fields, **kw)


def read(model, ids, fields):
    return C.call(model, "read", ids, fields=fields) if ids else []


def write(model, ids, vals):
    if not isinstance(ids, (list, tuple)):
        ids = [ids]
    if ARGS and ARGS.dry_run:
        vlog(f"DRY write {model} {ids} <- {vals}")
        return True
    return C.call(model, "write", ids, vals)


def do_create(model, vals):
    if ARGS and ARGS.dry_run:
        vlog(f"DRY create {model} <- {vals}")
        return -1
    res = C.call(model, "create", vals)
    return res[0] if isinstance(res, (list, tuple)) else res


def get_param(key, default="0"):
    return C.call("ir.config_parameter", "get_param", key, default)


def set_param(key, value):
    if ARGS and ARGS.dry_run:
        vlog(f"DRY set_param {key}={value}")
        return True
    return C.call("ir.config_parameter", "set_param", key, value)


# ---------- JOB 1: 12-month expiry on package cards ----------
def job_expiry():
    cards = search_read(
        "loyalty.card",
        [["program_id", "in", PACKAGE_PROGRAMS], ["expiration_date", "=", False]],
        ["id", "create_date"],
    )
    vlog(f"job1: {len(cards)} package cards missing expiry")
    n = 0
    for card in cards:
        cd = card.get("create_date")
        base = (datetime.datetime.strptime(cd[:10], "%Y-%m-%d").date()
                if cd else datetime.date.today())
        new_exp = base + datetime.timedelta(days=365)
        write("loyalty.card", card["id"], {"expiration_date": new_exp.isoformat()})
        log(f"sd-loyalty: set expiry {new_exp.isoformat()} on card {card['id']}")
        n += 1
    return n


# ---------- JOB 2: membership credit per posted membership invoice ----------
def job_membership():
    last = int(get_param(PARAM_KEY, "0") or "0")
    moves = search(
        "account.move",
        [["move_type", "=", "out_invoice"], ["state", "=", "posted"], ["id", ">", last]],
        order="id asc",
    )
    vlog(f"job2: {len(moves)} posted invoices with id > {last}")
    maxid = last
    n = 0
    for mv_id in moves:
        if mv_id > maxid:
            maxid = mv_id
        # dotted-path domain: does this invoice carry the membership product?
        has_m = C.call("account.move.line", "search_count",
                       [["move_id", "=", mv_id],
                        ["product_id.product_tmpl_id", "=", MEMBERSHIP_PRODUCT]])
        if not has_m:
            continue
        mv = read("account.move", [mv_id], ["partner_id", "name"])[0]
        partner = mv.get("partner_id")
        if not partner:
            continue
        partner_id = partner[0]
        mcard = search("loyalty.card",
                       [["program_id", "=", MEMBERSHIP_PROGRAM],
                        ["partner_id", "=", partner_id]], limit=1)
        if mcard:
            cur = read("loyalty.card", mcard, ["points"])[0].get("points") or 0
            card_id = mcard[0]
        else:
            card_id = do_create("loyalty.card",
                                {"program_id": MEMBERSHIP_PROGRAM,
                                 "partner_id": partner_id, "points": 0})
            cur = 0
        write("loyalty.card", card_id, {"points": cur + 1})
        log(f"sd-loyalty: membership credit +1 -> partner {partner_id} (invoice {mv.get('name')})")
        n += 1
    if maxid > last:
        set_param(PARAM_KEY, str(maxid))
        vlog(f"advanced {PARAM_KEY} {last} -> {maxid}")
    return n


def main():
    global ARGS, C
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read + log, write nothing")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()

    C = OdooClient()
    mode = "DRY-RUN" if ARGS.dry_run else "LIVE"
    log(f"sd-loyalty [{mode}] connected uid={C.uid} db={C.db}")
    try:
        a = job_expiry()
        b = job_membership()
    except Exception as e:  # noqa: BLE001
        log(f"sd-loyalty ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
    log(f"sd-loyalty done [{mode}] expiry_set={a} credits={b} errors=0")


if __name__ == "__main__":
    main()
