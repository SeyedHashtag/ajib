from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest


def panels_for(panel):
    return SimpleNamespace(prepare_new_user_creation=lambda **kw: {'client': panel, 'existing_usernames': set()},
                           get_client=lambda server: panel, record_created_user=lambda *args: None)


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
    services = Services(panels_for(panel))
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
    services = Services(panels_for(panel))
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
                web_trials.process_one(Services(panels_for(panel)))
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


def replacement_trial():
    return {'username': 'oldtrial', 'server_id': 'primary', 'used_at': '2026-01-01T00:00:00Z',
            'reset_at': '2026-02-01T00:00:00Z', 'replacement_eligible_at': '2026-02-01T00:00:00Z',
            'replacement_from_username': 'oldtrial', 'replacement_from_server_id': 'primary',
            'replacement_from_used_at': '2026-01-01T00:00:00Z'}


def test_trial_replacement_retains_both_accounts_and_queues_cleanup_without_io(storage, monkeypatch):
    import sys
    from utils import trial_operations, account_operations as operations, database
    from utils.atomic_store import locked_json, read_json
    with locked_json(trial_operations.CONFIGS, {}) as configs:
        configs['123'] = replacement_trial()
    ident = trial_operations.claim(123, language='fa')
    with pytest.raises(operations.AccountBusy, match='Trial recovery'):
        operations.assert_no_pending_obligations('primary', 'oldtrial')
    def create(*args, **kwargs):
        assert not database.get_connection().in_transaction
        for name in ('oldtrial', 'newtrial'):
            with pytest.raises(operations.AccountBusy):
                operations.assert_available('primary', name)
        return {'created': True}
    panel = SimpleNamespace(server_id='primary', add_user=create)
    panels = panels_for(panel)
    def lookup(*args, **kwargs):
        assert not database.get_connection().in_transaction
        with pytest.raises(operations.AccountBusy):
            operations.assert_available('primary', 'oldtrial')
        return panel, {'status': 'on hold', 'blocked': False, 'expiration_days': 30, 'max_download_bytes': 1024**3,
                       'upload_bytes': 0, 'download_bytes': 0}, {'status': 'found', 'uniqueness_verified': True}
    panels.resolve_unique_user = lookup
    trial_operations.create(ident, 123, panels, lambda names: 'newtrial', {'gb': 1, 'days': 1})
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 3
    # Importing the Telegram cleanup module or dispatching its notifier here is forbidden.
    monkeypatch.setitem(sys.modules, 'utils.expired_cleanup', None)
    trial_operations.complete(ident, notify=True)
    trial_operations.complete(ident, notify=True)
    entry = read_json(trial_operations.CONFIGS, {})['123']
    assert entry['username'] == 'newtrial'
    assert entry['historical_configs'][0]['username'] == 'oldtrial'
    assert entry['historical_configs'][0]['cleanup_reason'] == 'superseded_on_hold_test'
    assert not database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 1


def test_trial_replacement_rejects_cleanup_claim_and_changed_identity(storage):
    from utils import trial_operations, account_operations as operations
    from utils.atomic_store import locked_json
    with locked_json(trial_operations.CONFIGS, {}) as configs:
        configs['123'] = replacement_trial()
    operations.execute('cleanup-test', 'primary', 'oldtrial', 'delete', {}, lambda: {'success': True})
    with pytest.raises(operations.AccountBusy):
        trial_operations.claim(123)
    operations.complete('cleanup-test')
    ident = trial_operations.claim(123)
    with locked_json(trial_operations.CONFIGS, {}) as configs:
        configs['123']['replacement_from_username'] = 'changed'
    with pytest.raises(operations.AccountBusy, match='provenance'):
        trial_operations.create(ident, 123, None, None, {'gb': 1, 'days': 1})


def test_trial_replacement_rechecks_panel_after_acquiring_claims(storage):
    from utils import trial_operations, account_operations as operations, database
    from utils.atomic_store import locked_json
    with locked_json(trial_operations.CONFIGS, {}) as configs:
        configs['123'] = replacement_trial()
    ident = trial_operations.claim(123)
    calls = []
    panel = SimpleNamespace(server_id='primary', add_user=lambda *a, **kw: calls.append(a))
    panels = panels_for(panel)
    panels.resolve_unique_user = lambda *a, **kw: (panel,
        {'status': 'online', 'expiration_days': 30, 'max_download_bytes': 1024**3,
         'upload_bytes': 1, 'download_bytes': 0}, {'status': 'found', 'uniqueness_verified': True})
    with pytest.raises(operations.AccountBusy, match='no longer unused'):
        trial_operations.create(ident, 123, panels, lambda names: 'newtrial', {'gb': 1, 'days': 1})
    assert not calls
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 3
