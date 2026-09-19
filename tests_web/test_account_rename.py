import copy
import json
from types import SimpleNamespace

import pytest

from utils import account_operations as operations, database, identity_references
from utils.account_rename import rename


@pytest.fixture
def rename_panel(storage):
    state = {'alice': {'username': 'alice', 'account_creation_date': '2026-01-01T00:00:00Z',
                       'expiration_days': 30, 'max_download_bytes': 1024**3, 'password': 'synthetic'}}
    calls = []
    def lookup(username):
        assert not database.get_connection().in_transaction
        return {'status': 'found', 'data': copy.deepcopy(state[username])} if username in state else {'status': 'missing'}
    def update(username, changes):
        assert not database.get_connection().in_transaction
        calls.append((username, changes))
        new = changes['new_username']
        state[new] = state.pop(username)
        state[new]['username'] = new
        return {'ok': True}
    return SimpleNamespace(server_id='s1', get_user_result=lookup, update_user=update, state=state, calls=calls)


def payment():
    from utils.state_store import _save_payment_record
    _save_payment_record(database.get_connection(), 'main', 'original-payment', {
        'username': 'alice', 'server_id': 's1', 'user_id': 123, 'status': 'completed',
        'price': 10, 'updates': [{'username': 'alice', 'status': 'completed'}]})


def test_rename_preserves_owner_history_and_duplicate_finalization(rename_panel):
    payment()
    for _ in range(2):
        rename('rename:1', rename_panel, 'alice', 's999', '1')
    assert len(rename_panel.calls) == 1
    record = json.loads(database.get_connection().execute('SELECT payload_json FROM payments').fetchone()[0])
    assert record['username'] == 's999'
    assert record['updates'][0]['username'] == 'alice'
    assert identity_references.ownership('s999', 123) is True
    assert identity_references.ownership('s999', 999) is False
    assert identity_references.ownership('alice', 123) is False
    assert operations.details('rename:1')['phase'] == 'completed'
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_identity_history').fetchone()[0] == 1
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 1


def test_timeout_after_rename_reconciles_without_second_panel_call(rename_panel):
    from utils import operation_recovery
    payment()
    update = rename_panel.update_user
    def timeout(*args):
        update(*args)
        raise TimeoutError('synthetic lost response')
    rename_panel.update_user = timeout
    with pytest.raises(TimeoutError):
        rename('rename:2', rename_panel, 'alice', 'bob', '1')
    for name in ['alice', 'bob']:
        with pytest.raises(operations.AccountBusy):
            operations.assert_available('s1', name)
    panels = SimpleNamespace(get_client=lambda server: rename_panel)
    evidence = operation_recovery.inspect('rename:2', panels)
    assert evidence['action'] == 'complete_accounting'
    operation_recovery.reconcile('rename:2', panels, evidence['evidence_digest'])
    assert len(rename_panel.calls) == 1
    assert operations.details('rename:2')['phase'] == 'completed'


def test_changed_ownership_after_dispatch_retains_claims(rename_panel):
    from utils import operation_recovery
    payment()
    update = rename_panel.update_user
    def change(*args):
        result = update(*args)
        from utils.state_store import _save_payment_record
        _save_payment_record(database.get_connection(), 'main', 'original-payment', {
            'username': 'alice', 'server_id': 's1', 'user_id': 456, 'status': 'completed'})
        return result
    rename_panel.update_user = change
    with pytest.raises(operations.AccountBusy):
        rename('rename:3', rename_panel, 'alice', 'bob', '1')
    evidence = operation_recovery.inspect('rename:3', SimpleNamespace(get_client=lambda server: rename_panel))
    assert evidence['reason'] == 'identity_references_changed'
    assert evidence['action'] == 'none'
    with pytest.raises(operations.AccountBusy):
        operations.assert_available('s1', 'bob')


def test_destination_collision_never_dispatches(rename_panel):
    rename_panel.state['bob'] = {**rename_panel.state['alice'], 'username': 'bob'}
    with pytest.raises(operations.AccountBusy):
        rename('rename:4', rename_panel, 'alice', 'bob', '1')
    assert not rename_panel.calls


def test_accounting_failure_resumes_verified_step(rename_panel, monkeypatch):
    payment()
    original = identity_references.move
    def fail(*args):
        raise RuntimeError('synthetic accounting crash')
    monkeypatch.setattr(identity_references, 'move', fail)
    with pytest.raises(RuntimeError):
        rename('rename:5', rename_panel, 'alice', 'bob', '1')
    assert operations.details('rename:5')['phase'] == 'panel_verified'
    monkeypatch.setattr(identity_references, 'move', original)
    rename('rename:5', rename_panel, 'alice', 'bob', '1')
    assert len(rename_panel.calls) == 1


def test_child_step_does_not_replay_after_dispatch(storage):
    calls = []
    def child():
        calls.append(1)
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        operations.execute('workflow', 's1', 'alice', 'migration', {},
                           lambda: operations.step('workflow', 'create_destination', {}, child))
    with pytest.raises(operations.AccountBusy):
        operations.step('workflow', 'create_destination', {}, child)
    assert len(calls) == 1


def test_rename_updates_trial_and_cached_results_without_losing_trial_type(rename_panel):
    db = database.get_connection()
    trial = {'username': 'alice', 'server_id': 's1', 'telegram_id': 123, 'trial_scope': 'main',
             'historical_configs': [{'username': 'older', 'server_id': 's1'}]}
    db.execute("INSERT INTO kv_state(namespace,scope,state_key,value_json) VALUES ('test_configs','main','123',?)", (json.dumps(trial),))
    db.execute("INSERT INTO web_trials VALUES ('trial','123','key','completed','fa','alice','s1',1,1,NULL)")
    db.execute("INSERT INTO web_actions VALUES ('main','123','trial','key','immutable-request',?,1)",
               (json.dumps({'username': 'alice', 'server_id': 's1'}),))
    rename('rename:trial', rename_panel, 'alice', 'neutral', 1)
    assert identity_references.is_trial('neutral', 's1')
    assert identity_references.ownership('neutral', 123)
    assert db.execute('SELECT username FROM web_trials').fetchone()[0] == 'neutral'
    assert json.loads(db.execute('SELECT result_json FROM web_actions').fetchone()[0])['username'] == 'neutral'
    assert db.execute('SELECT request_hash FROM web_actions').fetchone()[0] == 'immutable-request'
    record = json.loads(db.execute("SELECT value_json FROM kv_state WHERE namespace='test_configs'").fetchone()[0])
    assert record['historical_configs'][0]['username'] == 'older'
