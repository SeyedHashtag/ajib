import copy
import json
from types import SimpleNamespace

import pytest
from utils import account_operations as operations, cleanup_operations, database, operation_recovery, state_store


@pytest.fixture
def cleanup(storage):
    user = {'username': 'alice', 'account_creation_date': '2026-01-01', 'expiration_days': 1,
            'max_download_bytes': 1024**3}
    state = {'user': user}
    calls = []
    def lookup(name):
        assert not database.get_connection().in_transaction
        return {'status': 'found', 'data': copy.deepcopy(state['user'])} if state['user'] else {'status': 'missing'}
    def delete(name):
        assert not database.get_connection().in_transaction
        state['user'] = None
        calls.append(name)
        return {'ok': True}
    client = SimpleNamespace(server_id='s1', get_user_result=lookup, delete_user=delete)
    state_store._save_payment_record(database.get_connection(), 'main', 'p1',
                                    {'username': 'alice', 'server_id': 's1', 'user_id': 123, 'status': 'completed', 'price': 10})
    candidate = {'username': 'alice', 'server_id': 's1', 'telegram_user_id': 123, 'source': 'customer', '_record_ref': ['payment', 'p1']}
    return client, candidate, user, calls, state


def test_cleanup_commits_metadata_claim_and_outbox_once(cleanup):
    client, candidate, user, calls, _ = cleanup
    for _ in range(2):
        cleanup_operations.remove(client, candidate, user, {}, lambda live: True)
    assert calls == ['alice']
    record = json.loads(database.get_connection().execute("SELECT payload_json FROM payments WHERE payment_id='p1'").fetchone()[0])
    assert record['cleanup_status'] == 'deleted'
    assert record['price'] == 10
    assert not database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 1


def test_cleanup_blocks_conflicting_renewal(cleanup):
    client, candidate, user, calls, _ = cleanup
    operations.execute('renewal', 's1', 'alice', 'renewal', {}, lambda: {'success': True})
    with pytest.raises(operations.AccountBusy):
        cleanup_operations.remove(client, candidate, user, {}, lambda live: True)
    assert calls == []


def test_cleanup_refuses_changed_generation_or_reference(cleanup):
    client, candidate, user, calls, state = cleanup
    original = copy.deepcopy(user)
    state['user']['account_creation_date'] = '2026-09-13'
    with pytest.raises(operations.AccountBusy):
        cleanup_operations.remove(client, candidate, original, {}, lambda live: True)
    candidate['_record_ref'] = ['payment', 'missing']
    with pytest.raises(operations.AccountBusy):
        cleanup_operations.remove(client, candidate, user, {}, lambda live: True)
    assert calls == []


def test_cleanup_lost_response_finishes_from_missing_evidence(cleanup):
    client, candidate, user, calls, _ = cleanup
    delete = client.delete_user
    def timeout(name):
        delete(name)
        raise TimeoutError()
    client.delete_user = timeout
    with pytest.raises(TimeoutError):
        cleanup_operations.remove(client, candidate, user, {}, lambda live: True)
    ident = database.get_connection().execute('SELECT operation_id FROM account_operations').fetchone()[0]
    panels = SimpleNamespace(resolve_unique_user=lambda *a, **k: (client, None, {'status': 'missing', 'uniqueness_verified': True}))
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'complete_accounting'
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert calls == ['alice']
    assert operations.details(ident)['phase'] == 'completed'


def test_cleanup_metadata_failure_rolls_back_and_retains_claim(cleanup, monkeypatch):
    from utils import web_store
    client, candidate, user, calls, _ = cleanup
    def fail(*args):
        raise RuntimeError('synthetic outbox failure')
    monkeypatch.setattr(web_store, 'enqueue', fail)
    with pytest.raises(RuntimeError):
        cleanup_operations.remove(client, candidate, user, {}, lambda live: True)
    record = json.loads(database.get_connection().execute("SELECT payload_json FROM payments WHERE payment_id='p1'").fetchone()[0])
    assert 'cleanup_status' not in record
    assert database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()
