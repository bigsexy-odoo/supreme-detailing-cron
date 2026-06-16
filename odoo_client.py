"""
Shared Odoo XML-RPC client. Loads auth from `.env` so credentials
never sit in scripts that might end up in git.
"""

import os
import xmlrpc.client
from pathlib import Path


def _load_env(path: Path) -> dict[str, str]:
    """Tiny .env parser - avoids the python-dotenv dependency."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV = _load_env(PROJECT_ROOT / ".env")


def cfg(key: str) -> str:
    """Get a config value: prefer process env, fall back to .env file."""
    val = os.environ.get(key) or ENV.get(key)
    if not val:
        raise RuntimeError(f"Missing config: {key}. Set in .env or env var.")
    return val


class OdooClient:
    """Lightweight XML-RPC wrapper. One instance = one authenticated session."""

    def __init__(self) -> None:
        self.url = cfg("ODOO_URL")
        self.db = cfg("ODOO_DB")
        self.user = cfg("ODOO_USER")
        self.api_key = cfg("ODOO_API_KEY")

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = self._common.authenticate(self.db, self.user, self.api_key, {})
        if not self.uid:
            raise RuntimeError(
                f"Auth failed for db={self.db!r} user={self.user!r}. "
                "Check ODOO_DB matches the actual database name and the API key is current."
            )

        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def version(self) -> dict:
        return self._common.version()

    def call(self, model: str, method: str, *args, **kwargs):
        """Call any model method. Positional args go through, kwargs become the options dict."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key, model, method, list(args), kwargs
        )


if __name__ == "__main__":
    """Smoke test: connect and print version + uid."""
    c = OdooClient()
    print("Connected to", c.url)
    print("DB:", c.db)
    print("User:", c.user, "uid:", c.uid)
    print("Server version:", c.version())
