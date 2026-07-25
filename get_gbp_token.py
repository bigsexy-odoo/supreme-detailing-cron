"""One-time: authorise Google Business Profile access, print the refresh token + client creds
to add as GitHub secrets. Run LOCALLY — it opens a browser; sign in as an account that has
Manager/Owner rights on the Supreme Detailing listing.

Needs cloud-cron/gbp_client_secret.json (Desktop OAuth client JSON from Cloud Console — see
GBP_SETUP.md) and google-auth-oauthlib (already installed for the Invoicing project).

  python cloud-cron/get_gbp_token.py
"""
import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit("Missing dependency: pip install google-auth-oauthlib")

SCOPES = ["https://www.googleapis.com/auth/business.manage"]
CS = Path(__file__).with_name("gbp_client_secret.json")

if not CS.exists():
    sys.exit(f"Missing {CS}\n  -> download the Desktop OAuth client JSON from Google Cloud Console "
             f"(APIs & Services -> Credentials) and save it there. See GBP_SETUP.md.")

flow = InstalledAppFlow.from_client_secrets_file(str(CS), SCOPES)
# access_type=offline + prompt=consent guarantees a refresh_token comes back
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

info = (json.loads(CS.read_text()).get("installed")
        or json.loads(CS.read_text()).get("web") or {})

print("\n=== add these 3 as GitHub secrets on bigsexy-odoo/supreme-detailing-cron ===")
print("GBP_CLIENT_ID     =", info.get("client_id"))
print("GBP_CLIENT_SECRET =", info.get("client_secret"))
print("GBP_REFRESH_TOKEN =", creds.refresh_token or "(NONE — see below)")
if not creds.refresh_token:
    print("\nNo refresh_token returned. Revoke the prior grant at "
          "https://myaccount.google.com/permissions and re-run.")
