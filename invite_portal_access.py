"""
Invite real customers to a portal account, so they can actually SEE their reward credits.

THE GAP THIS CLOSES: five loyalty programmes run with trigger='auto' and portal_visible=True
-- Gift Cards, the 6x Exterior/Interior/Supreme packages, and Membership - Included Detail.
Credits accrue on every qualifying order with no opt-in needed. But nothing in the checkout
ever invites the customer to make an account, so they bank credits they have no way to view.
The missing piece was never an opt-in; it was an invitation.

HOW THE INVITE WORKS (verified on this DB, SaaS 19.2):
  portal.wizard ("Grant Portal Access") + mail.template 4 "New Portal User Invite".
  The customer gets "Your account at Supreme Detailing" with an Activate Account button,
  clicks it, and PICKS THEIR OWN PASSWORD. We never see or set it.
  They then sign in at the same /web/login as you -- but they are share=True PORTAL users:
  free, unlimited, no billable seat. You land in the backend; they land on /my with their
  orders, invoices and reward card balances.

  NB in 19.2 res.partner has NO signup_token/signup_url field any more (only signup_type),
  so hand-rolling a signup link is not an option -- the wizard is the supported path. It also
  binds the login to the EXISTING partner, which is the whole point: a customer who signs up
  cold at /web/signup would get a NEW partner and their credit history would not follow them.

WHO GETS INVITED: a contact with a real email, at least one CONFIRMED order (sale/done), no
portal user already, and not staff/company. Quotes and abandoned carts are excluded -- we only
invite people who actually bought.

SAFETY (this emails real customers unattended):
  * dry-run by default; --commit to send
  * --max throttles per run (the SaaS daily email cap is real)
  * tags each invitee "Portal invited" so a re-run can never email them twice
  * skips anyone who already has a login, even if the tag is missing
  * the workflow step is gated behind a repo variable, so landing this file sends nothing

    python invite_portal_access.py                  # preview who would be invited
    python invite_portal_access.py --commit --max 1 # send one
    python invite_portal_access.py --partner 117    # target one contact (preview)
"""

import argparse
import io
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from odoo_client import OdooClient

INVITED_TAG = "Portal invited"
INVITED_COLOUR = 5
CONFIRMED_STATES = ("sale", "done")
EXCLUDE_TAGS = {"Company", "Founders"}

WELCOME = (
    "Your detail credits and booking history live here. "
    "Sign in any time to see what you've got saved up."
)


def get_tag(c, commit):
    found = c.call("res.partner.category", "search_read", [["name", "=", INVITED_TAG]], fields=["id"])
    if found:
        return found[0]["id"]
    if not commit:
        print(f"  [DRY-RUN] would create tag {INVITED_TAG!r}")
        return None
    r = c.call("res.partner.category", "create", [{"name": INVITED_TAG, "color": INVITED_COLOUR}])
    tid = r[0] if isinstance(r, list) else r
    print(f"  created tag {INVITED_TAG!r} -> {tid}")
    return tid


def eligible(c, only_partner=None, force=False):
    """Contacts who bought something, can be emailed, and have no login yet.

    force=True drops ONLY the confirmed-order requirement, and only makes sense with
    --partner: it exists so the invite can be demoed against your own address without
    emailing a real customer. Every other guard (has an email, no existing login, not
    already invited, not staff) still applies.
    """
    domain = [["active", "=", True], ["email", "!=", False]]
    if only_partner:
        domain.append(["id", "=", only_partner])
    partners = c.call("res.partner", "search_read", domain,
                      fields=["id", "name", "email", "category_id", "parent_id"])

    # One query for every login that exists -- cheaper than probing per contact, and it
    # catches someone who already has access but was never tagged.
    users = c.call("res.users", "search_read", [["id", ">", 0]],
                   fields=["partner_id"], context={"active_test": False})
    has_login = {u["partner_id"][0] for u in users if u["partner_id"]}

    tag_names = {}
    all_tags = c.call("res.partner.category", "search_read", [["id", ">", 0]], fields=["id", "name"])
    for t in all_tags:
        tag_names[t["id"]] = t["name"]

    out = []
    for p in partners:
        tags = {tag_names.get(t, "") for t in (p.get("category_id") or [])}
        if INVITED_TAG in tags:
            continue                                   # already invited
        if tags & EXCLUDE_TAGS:
            continue                                   # us, not a customer
        if p["id"] in has_login:
            continue                                   # already has access
        orders = c.call("sale.order", "search_count",
                        [["partner_id", "=", p["id"]], ["state", "in", list(CONFIRMED_STATES)]])
        if not orders and not force:
            continue                                   # never actually bought
        out.append((p, orders))
    return out


def invite(c, partner, tag_id, commit):
    """Drive portal.wizard for ONE partner. Creates the login and sends template 4."""
    if not commit:
        return True
    wiz = c.call("portal.wizard", "create", [{"welcome_message": WELCOME}],
                 context={"active_model": "res.partner", "active_ids": [partner["id"]]})
    wid = wiz[0] if isinstance(wiz, list) else wiz
    lines = c.call("portal.wizard", "read", [wid], fields=["user_ids"])[0]["user_ids"]
    if not lines:
        print(f"    ! wizard produced no line for {partner['name']!r} -- skipped")
        return False
    rows = c.call("portal.wizard.user", "read", lines,
                  fields=["id", "email", "email_state", "is_portal", "is_internal"])
    for r in rows:
        # email_state mirrors the UI: 'ok' | 'ko' (malformed) | 'exist' (taken by another user).
        # Granting on anything but 'ok' is what the form hides the button for.
        if r["email_state"] != "ok":
            print(f"    ! email_state={r['email_state']} for {r['email']} -- skipped")
            return False
        if r["is_internal"]:
            print(f"    ! {r['email']} is an INTERNAL user -- skipped (would burn a seat)")
            return False
        if r["is_portal"]:
            print(f"    ! {r['email']} already has portal access -- skipped")
            return False
        # action_grant_access lives on the LINE and does everything: creates the portal user
        # and sends the invite. There is no wizard-level apply in 19.2 -- action_apply was
        # removed, and pre-setting is_portal just hides the action from itself.
        c.call("portal.wizard.user", "action_grant_access", [r["id"]])
    if tag_id:
        c.call("res.partner", "write", [partner["id"]], {"category_id": [(4, tag_id)]})
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually create logins + send invites")
    ap.add_argument("--max", type=int, default=5, help="max invites per run (daily email cap)")
    ap.add_argument("--partner", type=int, help="target a single partner id")
    ap.add_argument("--force", action="store_true",
                    help="with --partner: skip the confirmed-order check (for demoing on your own address)")
    args = ap.parse_args()

    c = OdooClient()
    tag_id = get_tag(c, args.commit)
    if args.force and not args.partner:
        raise SystemExit("--force requires --partner: it is for demoing on ONE known address, "
                         "not for mass-inviting people who never bought.")
    targets = eligible(c, args.partner, args.force)

    if not targets:
        print("nobody eligible -- every paying customer already has access or was invited.")
        return

    print(f"{len(targets)} eligible:")
    sent = 0
    for p, orders in targets:
        if sent >= args.max:
            print(f"  ... stopping at --max {args.max} ({len(targets) - sent} left for next run)")
            break
        verb = "INVITE " if args.commit else "would invite"
        print(f"  {verb} {p['name']!r} <{p['email']}>  ({orders} confirmed order/s)")
        if invite(c, p, tag_id, args.commit):
            sent += 1

    if args.commit:
        print(f"\nsent {sent} invite/s.")
    else:
        print(f"\nDRY RUN -- nothing sent. Pass --commit to invite (throttled by --max).")


if __name__ == "__main__":
    main()
