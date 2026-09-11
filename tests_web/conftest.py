import os
import sys
from pathlib import Path

os.environ["AJIB_BOT_ROLE"] = "api"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core/scripts/telegrambot"))

import pytest


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AJIB_BOT_DIR", str(tmp_path))
    monkeypatch.setenv("AJIB_DB_PATH", str(tmp_path / "ajib.db"))
    monkeypatch.setenv("AJIB_SQLITE_ACTIVE", "1")
    monkeypatch.setenv("API_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("ADMIN_USER_IDS", "[1]")
    monkeypatch.setenv("AJIB_WEB_BOT_USERNAME", "SyntheticTestBot")
    monkeypatch.delenv("RECEIPT_CHECKER_USER_ID", raising=False)
    from utils import web_store
    web_store.initialize()
    yield tmp_path
    from utils import database
    for connection in database._connection_map().values():
        connection.close()
    database._connection_map().clear()


@pytest.fixture
def client(storage):
    from fastapi.testclient import TestClient
    from core.web.app import create_app
    from core.web.settings import Settings
    from utils.web_services import Services

    class Panels:
        def get_user_snapshot_entries(self, **kwargs):
            return []

    app = create_app(Settings(origin="https://testserver", secure_cookies=True,
                              writes_enabled=True, public_portal=True), Services(Panels()))
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        yield client


@pytest.fixture
def login():
    def perform(client, user=123):
        from utils.web_auth import confirm_challenge
        started = client.post("/api/v1/auth/challenge", json={})
        assert started.status_code == 200, started.text
        challenge = started.json()["challenge"]
        assert confirm_challenge(challenge, user, "main")
        result = client.post("/api/v1/auth/consume", json={"challenge": challenge})
        assert result.status_code == 200, result.text
        client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        return result
    return perform
