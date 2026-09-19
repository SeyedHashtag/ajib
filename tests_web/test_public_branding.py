import io
from types import SimpleNamespace

import pytest

from utils.public_branding import (contains_private, require_public, install_telegram_guard,
                                   PublicContentUnavailable, TITLES, make_qr)


@pytest.mark.parametrize('text', ['AJIB', 'a\u200bjib', '%61jib', '&#97;jib', 'ａｊｉｂ', 'عجیب'])
def test_private_variants_cannot_be_configured(text):
    assert contains_private({'label': text})
    with pytest.raises(PublicContentUnavailable):
        require_public(text)


@pytest.mark.parametrize('language', ['en', 'fa', 'tk', 'ru'])
def test_localized_main_title(client, language):
    response = client.get('/api/v1/storefront')
    assert response.json()['titles'][language] == TITLES[language]
    assert not contains_private(response.text)


def test_cookies_openapi_and_errors_are_public(client, login):
    result = login(client)
    assert 'service_session=' in result.headers['set-cookie']
    assert not contains_private(dict(result.headers))
    assert not contains_private(client.get('/api/v1/openapi.json').json())
    bad = client.post('/api/v1/auth/consume', json={'challenge': 'ajib'})
    assert bad.status_code == 422
    assert not contains_private(bad.text)


@pytest.mark.parametrize('user', [1, 123])
def test_legacy_support_and_raw_exception_fail_closed(client, login, monkeypatch, user):
    login(client, user)
    services = client.app.state.services
    monkeypatch.setattr(services, 'support', lambda: {'en': 'Powered by AJIB'})
    assert client.get('/api/v1/storefront').status_code == 503
    assert not contains_private(client.get('/api/v1/storefront').text)
    def fail(*args):
        raise RuntimeError('Internal /etc/ajib/secrets/token.json')
    monkeypatch.setattr(services, 'support', fail)
    response = client.get('/api/v1/storefront')
    assert response.status_code == 500
    assert not contains_private(response.text)
    assert 'token.json' not in response.text


def test_unsafe_storefront_is_rejected_before_commit(client, login):
    from utils import database
    login(client)
    database.get_connection().execute("INSERT INTO resellers(reseller_id,status,payload_json) VALUES ('123','approved','{}')")
    response = client.put('/api/v1/reseller/storefront', json={'title': 'AJIB', 'slug': 'safe-store'})
    assert response.status_code == 400
    assert not database.get_connection().execute('SELECT 1 FROM web_storefronts').fetchone()


def test_outgoing_guard_ignores_incoming_message_and_private_directory():
    sent = []
    bot = SimpleNamespace(reply_to=lambda *a, **k: sent.append((a, k)),
                          send_document=lambda *a, **k: sent.append((a, k)))
    install_telegram_guard(bot)
    bot.reply_to(SimpleNamespace(text='ajib'), 'Your connections')
    stream = io.BytesIO(b'username,traffic\nalice,10\n')
    stream.name = '/etc/ajib/exports/accounts.csv'
    bot.send_document(1, stream)
    assert stream.tell() == 0
    assert len(sent) == 2
    with pytest.raises(PublicContentUnavailable):
        bot.reply_to(SimpleNamespace(text='hello'), 'AJIB')
    stream = io.BytesIO(b'{"project":"ajib"}')
    stream.name = 'report.json'
    with pytest.raises(PublicContentUnavailable):
        bot.send_document(1, stream)
    assert stream.tell() == 0
    assert len(sent) == 2


def test_qr_checks_original_configuration_without_rewriting():
    with pytest.raises(PublicContentUnavailable):
        make_qr('vless://original-credential@example.test#AJIB')


def test_encoded_configuration_label_cannot_bypass_qr_guard():
    import base64
    payload = base64.b64encode(b'{"ps":"AJIB","id":"original-credential"}').decode()
    with pytest.raises(PublicContentUnavailable):
        make_qr('vmess://' + payload)


def test_admin_portal_does_not_export_private_exception_or_audit_details(client, login):
    from utils import database, web_store
    login(client, 1)
    db = database.get_connection()
    db.execute("INSERT INTO web_worker_health VALUES ('worker',1,1,'Traceback /private/operator/key.json',0)")
    web_store.audit(db, 1, 'main', 'review', 'payment', {'internal_path': '/private/operator/key.json'})
    response = client.get('/api/v1/admin/operations')
    assert response.status_code == 200
    assert response.json()['worker']['last_error'] == 'attention_required'
    assert 'key.json' not in response.text
    audit = client.get('/api/v1/admin/audit')
    assert audit.status_code == 200
    assert 'details_json' not in audit.json()[0]
    assert 'key.json' not in audit.text
