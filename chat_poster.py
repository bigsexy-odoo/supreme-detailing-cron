"""
chat_poster.py — reusable Google Chat poster for EXTERNAL Odoo scripts
(Architecture B: a script OUTSIDE the Odoo SaaS sandbox posts directly to a Chat
space incoming webhook). Stdlib-only (urllib) → runs unmodified on GitHub Actions.

Ported from the proven Te Moke `chat_bridge.py` (drtim5, 2026-06-07). Uses Google
Chat *text markup* (simple + reliable), not cardsV2.

    from chat_poster import lead_to_message, post_to_chat
    ok = post_to_chat(lead_to_message(lead, base_url), webhook_url)

`lead` is a plain dict of fields you read via RPC (m2o fields may be [id, name]).
Generalise TEAM_EMOJI per project. Watermark/dedup is the CALLER's job — in a cloud
cron, post right after you process each record (its "done" state lives in Odoo:
a tag/team/stage you set), or track a high-water-mark in an ir.config_parameter.

Self-test:  GCHAT_WEBHOOK_URL=... python chat_poster.py --test
"""

import json
import os
import sys
import urllib.error
import urllib.request

# team name -> emoji (read at a glance). Unknown teams fall back to 🆕.
TEAM_EMOJI = {
    "North": "⬆️", "Central": "🎯", "Triage": "📋", "Sales": "📋",
}


def _m2o_name(v, default="—"):
    """Odoo m2o comes as False or [id, name]; return the name (or default)."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return v[1]
    return default


def lead_to_message(lead: dict, base_url: str = "") -> str:
    """Build a Google-Chat-markup message for one lead/enquiry dict."""
    team = _m2o_name(lead.get("team_id"))
    stage = _m2o_name(lead.get("stage_id"))
    emoji = TEAM_EMOJI.get(team, "🆕")
    name = (lead.get("contact_name") or lead.get("partner_name")
            or lead.get("name") or "—")

    kind = lead.get("_kind", f"{team} lead")
    lines = [f"{emoji} *New {kind}* — *{name}*"]
    if lead.get("city"):
        lines.append(f"📍 {lead['city']}")
    contact = [x for x in (lead.get("email_from") or lead.get("email"),
                           lead.get("phone")) if x]
    if contact:
        lines.append("✉️ " + "  ·  ".join(contact))
    if lead.get("expected_revenue"):
        lines.append(f"💰 ${lead['expected_revenue']:.0f}")
    if team != "—":
        lines.append(f"📂 {team}" + (f"  ·  {stage}" if stage != "—" else ""))
    if base_url and lead.get("id"):
        model = lead.get("_model", "crm.lead")
        link = f"{base_url}/web#id={lead['id']}&model={model}&view_type=form"
        lines.append(f"<{link}|Open in Odoo →>")
    return "\n".join(lines)


def post_payload(payload: dict, webhook_url: str = None, timeout: int = 20) -> bool:
    """POST an arbitrary Chat message payload dict (text and/or cardsV2) to the
    incoming webhook. Returns True on 2xx. Surfaces Chat's JSON error body."""
    url = webhook_url or os.environ.get("GCHAT_WEBHOOK_URL")
    if not url:
        raise RuntimeError("GCHAT_WEBHOOK_URL not set")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=UTF-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"  [chat] HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        return False
    except urllib.error.URLError as e:
        print(f"  [chat] network error: {e.reason}")
        return False


def post_to_chat(text: str, webhook_url: str = None, timeout: int = 20) -> bool:
    """POST {'text': …} to the Chat incoming webhook (plain-text convenience)."""
    return post_payload({"text": text}, webhook_url, timeout)


if __name__ == "__main__":
    if "--test" in sys.argv:
        sample = {"contact_name": "Test Lead", "email_from": "test@example.com",
                  "phone": "021 000 0000", "city": "Takapuna",
                  "team_id": [4, "North"], "_kind": "Website Enquiry"}
        print("posted:", post_to_chat(lead_to_message(sample)))
    else:
        print(__doc__)
