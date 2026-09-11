import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
import pytest


def signed_data(token="123456:synthetic-test-token", user_id=123, when=None, **extras):
    data = {"auth_date": str(int(time.time()) if when is None else when),
            "user": json.dumps({"id": user_id, "first_name": "Test"}), **extras}
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_api_does_not_import_handlers(client):
    import sys
    assert "utils.command" not in sys.modules
    assert "utils.purchase_plan" not in sys.modules
    assert "utils.edit_plans" not in sys.modules
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_login_is_bound_to_browser_and_single_use(client, login):
    from utils.web_auth import confirm_challenge
    started = client.post("/api/v1/auth/challenge", json={}).json()
    assert client.post("/api/v1/auth/consume", json={"challenge": started["challenge"]}).json()["status"] == "waiting"
    assert confirm_challenge(started["challenge"], 123, "main")
    saved = client.cookies.get("ajib_login")
    client.cookies.clear()
    assert client.post("/api/v1/auth/consume", json={"challenge": started["challenge"]}).status_code == 401
    client.cookies.set("ajib_login", saved, path="/api/v1/auth")
    assert client.post("/api/v1/auth/consume", json={"challenge": started["challenge"]}).json()["status"] == "authenticated"
    assert client.post("/api/v1/auth/consume", json={"challenge": started["challenge"]}).status_code == 401
    assert client.get("/api/v1/me").json()["user_id"] == "123"


def test_mini_auth_rejects_forgery_stale_and_replay(client):
    for raw in [signed_data(token="wrong"), signed_data(when=1), signed_data(when=int(time.time()) + 90), signed_data() + "&auth_date=1"]:
        assert client.post("/api/v1/auth/telegram", json={"init_data": raw}).status_code == 401
    raw = signed_data()
    assert client.post("/api/v1/auth/telegram", json={"init_data": raw}).status_code == 200
    assert client.post("/api/v1/auth/telegram", json={"init_data": raw}).status_code == 401


def test_csrf_origin_and_revocation(client, login):
    login(client)
    assert client.get("/api/v1/me").json()["roles"] == ["customer"]
    assert client.get("/api/v1/admin/overview").status_code == 403
    assert client.get("/api/v1/reseller").status_code == 403
    client.headers["Origin"] = "https://evil.example"
    assert client.post("/api/v1/auth/logout").status_code == 403
    client.headers["Origin"] = "https://testserver"
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/auth/logout").status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/me").status_code == 401


def test_ownership_and_language(client, login):
    from utils import database
    from utils.web_orders import save_payment
    with database.transaction() as connection:
        save_payment(connection, "main", "other", {"user_id": 999, "status": "completed", "price": 1})
        save_payment(connection, "hosted:7", "hosted", {"user_id": 123, "status": "completed", "price": 1})
    login(client)
    assert client.get("/api/v1/payments").json() == []
    assert client.get("/api/v1/payments/other").status_code == 404
    assert client.get("/api/v1/accounts/primary/s999/configuration").status_code == 404
    assert client.put("/api/v1/me/language", json={"language": "fa"}).status_code == 200
    assert client.get("/api/v1/me").json()["language"] == "fa"


def test_role_revocation_is_effective_immediately(client, login, monkeypatch):
    login(client, 1)
    assert client.get("/api/v1/admin/overview").status_code == 200
    monkeypatch.setenv("ADMIN_USER_IDS", "[]")
    assert client.get("/api/v1/admin/overview").status_code == 403


def test_login_challenge_cannot_cross_storefront(storage):
    from utils.web_auth import create_challenge, confirm_challenge
    challenge, _ = create_challenge("hosted:7")
    assert not confirm_challenge(challenge, 123, "main")
    assert confirm_challenge(challenge, 123, "hosted:7")


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/v1/auth/challenge", json={}).status_code == 200
    assert client.post("/api/v1/auth/challenge", json={}).status_code == 429


def test_public_download_guidance_uses_existing_catalog(client):
    import sys
    for language in ['fa', 'en', 'ru', 'tk']:
        response = client.get('/api/v1/downloads', params={'language': language})
        assert response.status_code == 200
        apps = response.json()
        assert len(apps) == 4
        assert all(app['details'] and app['url'].startswith('https://') for app in apps)
    assert 'utils.download_guidance' not in sys.modules
