"""
Gift-card consumer: turn a purchased gift into a real contact + a delivered code.

THE GAP THIS CLOSES
    The shop already CAPTURES the recipient - 'Giftee Name/Email/Suburb/Mobile'
    are no_variant custom attributes (ids 9/10/11/12) on the gift templates,
    the same mechanism SDBK1 uses on a booking. But nothing ever CONSUMED them:
      * the giftee details sat as text on the order line, read by no one
      * loyalty.card.partner_id stayed False (or pointed at the BUYER)
      * mail.template 40 relies on use_default_to, which resolves to that same
        partner - so the RECIPIENT never received their own gift card
    Net effect: you could sell a gift card and the recipient would never hear
    about it. This script is the missing consumer.

WHAT IT DOES  (per unprocessed card)
    1. read the giftee off the originating order line
    2. find-or-create a res.partner for them  (matched on email, so a repeat
       recipient is never duplicated - duplicates split loyalty balances)
    3. assign loyalty.card.partner_id = giftee
    4. send the BRANDED template 40 to the giftee's own address

IDEMPOTENCY
    partner_id IS the marker. A card with partner_id set is already processed
    and is never picked up again - no extra state, nothing to get out of sync.

DELIBERATELY NOT DONE
    * the giftee is NOT auto-subscribed to marketing. They received a gift; they
      did not sign up for anything. Consent comes from the rewards signup.
    * gift cards are left non-expiring (expiration_date False). NZ Fair Trading
      expects any expiry to be disclosed at purchase; "never expires" is the
      safe default. Package cards get 12 months via loyalty_maintenance_external.

Runs OUTSIDE Odoo over XML-RPC -> ZERO billable Odoo LoC (Rule 9).

    python sync_giftcards.py                 # dry run - writes nothing
    python sync_giftcards.py --commit
    python sync_giftcards.py --commit --no-email
"""

import argparse
import sys

from odoo_client import OdooClient

# --- config (Supreme Detailing) --------------------------------------------
GIFT_PROGRAMS = [1, 2, 3, 4]      # Gift Cards + the three 6x packages
GIFT_TEMPLATE = 40                # mail.template "Gift Card: Gift Card Information"
GIFTEE_ATTRS = {9: "name", 10: "email", 11: "suburb", 12: "mobile"}
SENDER = "Supreme Detailing <bookings@supremedetailing.co.nz>"
# ---------------------------------------------------------------------------

NOISE_OFF = {
    "tracking_disable": True, "mail_create_nosubscribe": True,
    "mail_create_nolog": True, "mail_notify_author": False,
    "mail_auto_subscribe_no_notify": True,
}

ARGS = None
C = None


def log(m):
    print(m, flush=True)


def vlog(m):
    if ARGS and ARGS.verbose:
        print("  . " + m, flush=True)


def unwrap(r):
    return r[0] if isinstance(r, (list, tuple)) else r


def giftee_ptav_map():
    """{product.template.attribute.value id -> 'name'|'email'|'suburb'|'mobile'}

    Resolved at runtime rather than hardcoded: the ptav ids differ per product
    template, and a new gift product would silently miss a hardcoded list."""
    rows = C.call("product.template.attribute.value", "search_read",
                  [["attribute_id", "in", list(GIFTEE_ATTRS)]],
                  fields=["id", "attribute_id"])
    return {r["id"]: GIFTEE_ATTRS[r["attribute_id"][0]] for r in rows}


def read_giftee(line_id, ptav_map):
    """Pull the giftee fields off one order line's custom attribute values."""
    cavs = C.call("product.attribute.custom.value", "search_read",
                  [["sale_order_line_id", "=", line_id]],
                  fields=["custom_product_template_attribute_value_id",
                          "custom_value"])
    out = {}
    for cav in cavs:
        ptav = cav.get("custom_product_template_attribute_value_id")
        if not ptav:
            continue
        key = ptav_map.get(ptav[0])
        val = (cav.get("custom_value") or "").strip()
        if key and val:
            out[key] = val
    return out


def resolve_partner(giftee, fallback_partner):
    """Find-or-create the giftee's contact, matched on EMAIL.

    Matching first is the whole point: a duplicate partner splits a customer's
    loyalty balance and history across two records that can never see each
    other (exactly what happened to Will Howson, partners 79 and 88)."""
    email = (giftee.get("email") or "").strip()
    if not email:
        return fallback_partner, "no giftee email -> fell back to the buyer"

    found = C.call("res.partner", "search_read", [["email", "=ilike", email]],
                   fields=["id", "name"], limit=1)
    if found:
        return found[0]["id"], f"matched existing partner {found[0]['id']}"

    if not ARGS.commit:
        return -1, f"would create partner for {email}"

    vals = {"name": giftee.get("name") or email.split("@")[0], "email": email}
    if giftee.get("mobile"):
        vals["phone"] = giftee["mobile"]
    if giftee.get("suburb"):
        vals["city"] = giftee["suburb"]
    pid = unwrap(C.call("res.partner", "create", [vals], context=NOISE_OFF))
    return pid, f"created partner {pid}"


def process():
    ptav_map = giftee_ptav_map()
    vlog(f"giftee ptav map: {len(ptav_map)} value(s) across attrs {list(GIFTEE_ATTRS)}")

    cards = C.call("loyalty.card", "search_read",
                   [["program_id", "in", GIFT_PROGRAMS],
                    ["partner_id", "=", False],
                    ["order_id", "!=", False]],
                   fields=["id", "code", "points", "program_id", "order_id",
                           "expiration_date"])
    log(f"  {len(cards)} unassigned card(s) with an originating order")
    if not cards:
        return 0, 0

    # group cards by order so multiple gifts on one order can be paired to lines
    by_order = {}
    for cd in cards:
        by_order.setdefault(cd["order_id"][0], []).append(cd)

    done = warned = 0
    for oid, ocards in sorted(by_order.items()):
        if ARGS.max and done >= ARGS.max:
            log(f"  --max {ARGS.max} reached, stopping")
            break

        order = C.call("sale.order", "read", [oid],
                       fields=["name", "state", "partner_id"])[0]
        if order["state"] not in ("sale", "done"):
            vlog(f"{order['name']}: state={order['state']} - not a confirmed sale, skipped")
            continue

        lines = C.call("sale.order.line", "search_read", [["order_id", "=", oid]],
                       fields=["id", "product_id", "name"], order="id asc")
        gift_lines = [l for l in lines if read_giftee(l["id"], ptav_map)]

        if len(ocards) > 1 and len(gift_lines) != len(ocards):
            log(f"  WARNING {order['name']}: {len(ocards)} card(s) but "
                f"{len(gift_lines)} gift line(s) - pairing by order, verify manually")
            warned += 1

        buyer = order["partner_id"][0] if order.get("partner_id") else False

        for idx, cd in enumerate(sorted(ocards, key=lambda x: x["id"])):
            line = gift_lines[idx] if idx < len(gift_lines) else None
            giftee = read_giftee(line["id"], ptav_map) if line else {}

            if not giftee:
                log(f"  {order['name']} card {cd['id']}: no giftee captured "
                    f"-> would send to the buyer instead")
                warned += 1

            pid, how = resolve_partner(giftee, buyer)
            to_addr = giftee.get("email")
            if not to_addr and pid and pid > 0:
                bp = C.call("res.partner", "read", [pid], fields=["email"])[0]
                to_addr = bp.get("email")

            label = (f"{order['name']} card {cd['id']} {cd['code']} "
                     f"${cd['points']} -> {giftee.get('name') or '(buyer)'} "
                     f"<{to_addr or 'NO ADDRESS'}>")

            if not ARGS.commit:
                log(f"  would process: {label}  [{how}]")
                done += 1
                continue

            if pid and pid > 0:
                C.call("loyalty.card", "write", [cd["id"]], {"partner_id": pid},
                       context=NOISE_OFF)
            log(f"  assigned: {label}  [{how}]")

            if ARGS.no_email:
                vlog("email suppressed by --no-email")
            elif not to_addr:
                log(f"    WARNING no address for card {cd['id']} - code NOT delivered")
                warned += 1
            else:
                try:
                    C.call("mail.template", "send_mail", GIFT_TEMPLATE, cd["id"],
                           email_values={"email_to": to_addr, "email_from": SENDER},
                           force_send=False)
                    log(f"    queued branded gift-card email -> {to_addr}")
                except Exception as e:  # noqa: BLE001
                    log(f"    WARNING gift-card email failed: {e}")
                    warned += 1
            done += 1

    return done, warned


def main():
    global ARGS, C
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="actually write + email (default: dry run)")
    ap.add_argument("--no-email", action="store_true",
                    help="assign the partner but do not send the code")
    ap.add_argument("--max", type=int, default=25,
                    help="cap cards processed per pass (email-cap safety)")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()

    C = OdooClient()
    mode = "COMMIT" if ARGS.commit else "DRY-RUN"
    log(f"=== SD gift-card sync [{mode}] ===")
    log(f"    connected uid={C.uid}  programs={GIFT_PROGRAMS}  "
        f"email={'OFF' if ARGS.no_email else 'ON'}")

    done, warned = process()

    log(f"=== done [{mode}] processed={done} warnings={warned} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
