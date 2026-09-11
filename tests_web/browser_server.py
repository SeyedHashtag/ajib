"""Synthetic, loopback-only API for browser tests. Never use for deployment."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / ".web-local" / f"browser-{os.getpid()}"
DATA.mkdir(parents=True, exist_ok=True)
os.environ.update(AJIB_BOT_ROLE="api", AJIB_BOT_DIR=str(DATA),
    AJIB_DB_PATH=str(DATA / "ajib.db"), AJIB_SQLITE_ACTIVE="1",
    API_TOKEN="123456:synthetic-browser-test-token", ADMIN_USER_IDS="[1]",
    AJIB_WEB_BOT_USERNAME="SyntheticTestBot", SERVERS_JSON="[]")

from core.web.runtime import configure
configure()
from utils import database, web_store
from utils.web_orders import save_payment
from utils.web_services import Services
from core.web.app import create_app
from core.web.settings import Settings


class Panel:
    server_id = "test"

    def get_user_uri(self, username):
        return {"uri": "hysteria2://synthetic-password@server.invalid:443/?sni=server.invalid"}


class Panels:
    def __init__(self):
        self.client = Panel()
        self.user = {"username": "s123a", "status": "online", "blocked": False,
                     "expiration_days": 30, "account_creation_date": datetime.now(timezone.utc).isoformat(),
                     "max_download_bytes": 40 * 1024**3, "download_bytes": 3 * 1024**3,
                     "upload_bytes": 1024**3}

    def get_user_snapshot_entries(self, **kwargs):
        return [{"client": self.client, "users": {"s123a": self.user}}]

    def resolve_unique_user(self, username, preferred_server_id=None):
        return self.client, self.user, {"status": "found", "uniqueness_verified": True}


web_store.initialize()
with database.transaction() as connection:
    save_payment(connection, "main", "synthetic-purchase", {
        "user_id": 123, "status": "completed", "plan_gb": "40", "price": 1.20,
        "days": 30, "username": "s123a", "server_id": "test", "payment_method": "Crypto",
    })
app = create_app(Settings(origin="http://127.0.0.1:5173", public_portal=True), Services(Panels()))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
