from types import SimpleNamespace

import pytest


@pytest.fixture
def configurations(storage):
    from core.web.app import create_app
    from core.web.settings import Settings
    from fastapi.testclient import TestClient
    from utils.web_services import Services
    one = 'hysteria2://synthetic@one.example.test:443'
    two = 'hysteria2://synthetic@two.example.test:8443'
    payload = {'normal_sub': 'https://subscription.example.test/synthetic', 'ipv4': one,
               'links': [one, two, one], 'direct': False, 'password': 'never expose', 'note': 'private'}
    panel = SimpleNamespace(server_id='s1', get_user_uri=lambda name: payload)
    panels = SimpleNamespace(resolve_unique_user=lambda *a, **kw: (panel, {}, {'status': 'found', 'uniqueness_verified': True}))
    app = create_app(Settings(origin='https://testserver', secure_cookies=True, public_portal=True), Services(panels))
    with TestClient(app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client, payload


def test_subscription_all_inbounds_qr_and_download_keep_one_owned_account(configurations, login, monkeypatch):
    import qrcode
    client, payload = configurations
    base = '/api/v1/accounts/s1/s123a'
    for path in ('/configuration', '/configuration.txt', '/qr?key=uri_2'):
        assert client.get(base + path).status_code == 401
    login(client)
    result = client.get(base + '/configuration').json()
    assert result == {'ipv4': payload['ipv4'], 'sub_url': payload['normal_sub'],
                      'uri_1': payload['links'][0], 'uri_2': payload['links'][1]}
    encoded = []
    original = qrcode.make
    monkeypatch.setattr(qrcode, 'make', lambda value: (encoded.append(value), original(value))[1])
    assert client.get(base + '/qr?key=uri_2').headers['content-type'] == 'image/png'
    assert encoded == [payload['links'][1]]
    assert client.get(base + '/qr?key=password').status_code == 404
    download = client.get(base + '/configuration.txt')
    assert set(download.text.splitlines()) == {payload['normal_sub'], *payload['links']}
    assert len(download.text.splitlines()) == 3
    assert download.headers['content-disposition'] == 'attachment; filename="connections.txt"'
    assert 'no-store' in download.headers['cache-control']
    login(client, 999)
    for path in ('/configuration', '/configuration.txt', '/qr?key=uri_2'):
        assert client.get(base + path).status_code == 404


def test_direct_fallback_and_legacy_configurations_remain_compatible(configurations, login):
    client, payload = configurations
    login(client)
    payload.update(direct=True)
    result = client.get('/api/v1/accounts/s1/s123a/configuration').json()
    assert 'sub_url' not in result and result['uri'] == payload['normal_sub']
    payload.clear()
    payload['uri'] = 'hysteria2://synthetic@server.example.test:443'
    assert client.get('/api/v1/accounts/s1/s123a/configuration').json() == payload
    payload.clear()
    assert client.get('/api/v1/accounts/s1/s123a/configuration').status_code == 503


def test_simultaneous_configuration_reads_wait_without_touching_account_mutation_rules(storage):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from utils.web_services import Services
    entered = Event()
    def uri(username):
        entered.set()
        time.sleep(0.1)
        return {'uri': 'hysteria2://synthetic@server.example.test:443'}
    panel = SimpleNamespace(server_id='s1', get_user_uri=uri)
    service = Services(SimpleNamespace(resolve_unique_user=lambda *a, **kw: (panel, {}, {'status': 'found', 'uniqueness_verified': True})))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.configuration, 123, 'main', 's123a', 's1')
        assert entered.wait(2)
        second = pool.submit(service.configuration, 123, 'main', 's123a', 's1')
        assert first.result(timeout=3) == second.result(timeout=3)


def test_claimed_configuration_download_returns_safe_conflict(configurations, login):
    from utils import database
    client, _ = configurations
    login(client)
    with database.transaction() as db:
        db.execute('INSERT INTO account_operation_claims VALUES (?,?,?)', ('s1', 's123a', 'synthetic-owner'))
    response = client.get('/api/v1/accounts/s1/s123a/configuration.txt')
    assert response.status_code == 409
    assert 'synthetic-owner' not in response.text
