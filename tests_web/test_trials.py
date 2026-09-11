from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def test_trial_and_shared_bot_eligibility(client, login):
    from utils import web_trials
    from utils.web_services import Services
    from utils.trial_state import _has_used_test_config_from
    from utils.test_config_store import load_test_configs
    login(client)
    headers = {'Idempotency-Key': 'trial-request-key-123'}
    response = client.post('/api/v1/trial', headers=headers)
    assert response.status_code == 200, response.text
    assert client.post('/api/v1/trial', headers=headers).json() == response.json()
    calls = []
    def create(username, *args, **kwargs):
        from utils import database
        assert not database.get_connection().in_transaction
        assert _has_used_test_config_from(load_test_configs(web_trials.CONFIGS), 123)
        calls.append(username)
        return {'username': username}
    panel = SimpleNamespace(server_id='primary', add_user=create)
    services = Services(SimpleNamespace(select_server_for_new_user=lambda: panel))
    assert web_trials.process_one(services)
    assert not web_trials.process_one(services)
    state = client.get('/api/v1/trial').json()
    assert state['status'] == 'completed' and not state['eligible'] and not state['queued']
    assert len(calls) == 1
    assert state['account']['username'] == calls[0]
    assert client.post('/api/v1/trial/connected').status_code == 200
    assert client.get('/api/v1/trial').json()['account']['connected_at']
    assert client.post('/api/v1/trial', headers={'Idempotency-Key': 'new-trial-request-key'}).status_code == 409


def test_trial_disabled_waitlist_and_unknown_panel_result(storage):
    from utils import web_trials, database
    from utils.atomic_store import locked_json
    from utils.web_services import Services
    from utils.trial_state import _has_used_test_config_from
    from utils.test_config_store import load_test_configs
    with locked_json(web_trials.SETTINGS, {}) as settings:
        settings['creation_disabled'] = True
    web_trials.request('123', 'main', 'queued-test-key-123', 'fa')
    assert not web_trials.process_one(Services())
    assert web_trials.state('123', 'main')['queued']
    with locked_json(web_trials.SETTINGS, {}) as settings:
        settings['creation_disabled'] = False
    calls = []
    panel = SimpleNamespace(server_id='primary', add_user=lambda *a, **kw: calls.append(a) or None)
    services = Services(SimpleNamespace(select_server_for_new_user=lambda: panel))
    assert web_trials.process_one(services)
    assert not web_trials.process_one(services)
    assert web_trials.state('123', 'main')['status'] == 'uncertain'
    # Unlike a legacy pre-request claim, an uncertain network outcome never ages out.
    assert _has_used_test_config_from(load_test_configs(web_trials.CONFIGS), 123, now='2099-01-01T00:00:00Z')
    assert len(calls) == 1
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_trials').fetchone()[0] == 1


def test_simultaneous_bot_worker_trial_claim(storage):
    from utils import web_trials, database
    from utils.atomic_store import locked_json
    from utils.trial_state import _has_used_test_config_from, _mark_test_config_used_in_memory
    from utils.web_services import Services
    web_trials.request('123', 'main', 'concurrent-trial-123', 'en')
    calls = []
    panel = SimpleNamespace(server_id='primary', add_user=lambda *a, **kw: calls.append(a) or {'created':True})
    def run(interface):
        try:
            if interface == 'web':
                web_trials.process_one(Services(SimpleNamespace(select_server_for_new_user=lambda: panel)))
            else:
                with locked_json(web_trials.CONFIGS, {}) as configs:
                    if not _has_used_test_config_from(configs, 123):
                        _mark_test_config_used_in_memory(configs, 123, username='t123bot', server_id='primary')
                        calls.append('bot')
        finally:
            for connection in database._connection_map().values(): connection.close()
            database._connection_map().clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ['bot', 'web']))
    assert len(calls) == 1
