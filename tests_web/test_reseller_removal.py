import copy
import json
from types import SimpleNamespace

import pytest
from utils import account_operations as operations, database, operation_recovery, reseller_removal, state_store


@pytest.fixture
def reseller_case(storage):
    record = {'status': 'banned', 'debt': 20, 'configs': [{'username': 'alice', 'server_id': 's1', 'price': 10,
               'timestamp': '2026-01-01T00:00:00Z', 'days': 30, 'gb': 1}], 'total_paid': 0}
    state_store._save_reseller_record(database.get_connection(), '7', record)
    state = {'user': {'username': 'alice', 'account_creation_date': '2026-01-01', 'expiration_days': 30, 'max_download_bytes': 1024**3}}
    calls = []
    def lookup(name):
        assert not database.get_connection().in_transaction
        return {'status': 'found', 'data': copy.deepcopy(state['user'])} if state['user'] else {'status': 'missing'}
    def delete(name):
        state['user'] = None
        calls.append(name)
        return {'ok': True}
    client = SimpleNamespace(server_id='s1', get_user_result=lookup, delete_user=delete)
    panels = SimpleNamespace(get_client=lambda server: client,
       resolve_unique_user=lambda name, **kw: (client, lookup(name).get('data'), {'status': lookup(name)['status'], 'uniqueness_verified': True}))
    return panels, client, calls


def test_banned_cleanup_accounts_once(reseller_case):
    panels, _, calls = reseller_case
    success, result = reseller_removal.cleanup_banned('7', panels, actor=1)
    assert success
    assert result['remaining_debt'] == 10
    reseller_removal.cleanup_banned('7', panels, actor=1)
    assert calls == ['alice']
    assert reseller_removal.current('7')['debt'] == 10
    assert not database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()


def test_repayment_after_delete_retains_panel_result_and_blocks_writeoff(reseller_case):
    panels, client, calls = reseller_case
    delete = client.delete_user
    def repay(name):
        result = delete(name)
        record = reseller_removal.current('7')
        record['debt'] = 0
        state_store._save_reseller_record(database.get_connection(), '7', record)
        return result
    client.delete_user = repay
    with pytest.raises(operations.AccountBusy):
        reseller_removal.cleanup_banned('7', panels, actor=1)
    assert reseller_removal.current('7')['debt'] == 0
    assert calls == ['alice']
    ident = database.get_connection().execute('SELECT operation_id FROM account_operations').fetchone()[0]
    report = operation_recovery.inspect(ident, panels)
    assert report['reason'] == 'reseller_debt_or_ownership_changed'
    with pytest.raises(operations.AccountBusy):
        operations.assert_available('s1', 'alice')


def test_banned_delete_timeout_recovers_original_accounting(reseller_case):
    panels, client, calls = reseller_case
    delete = client.delete_user
    def timeout(name):
        delete(name)
        raise TimeoutError()
    client.delete_user = timeout
    with pytest.raises(TimeoutError):
        reseller_removal.cleanup_banned('7', panels, actor=1)
    ident = database.get_connection().execute('SELECT operation_id FROM account_operations').fetchone()[0]
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'complete_accounting'
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert reseller_removal.current('7')['debt'] == 10
    assert calls == ['alice']


def test_debt_removal_uses_persisted_proration(reseller_case):
    panels, _, calls = reseller_case
    record = reseller_removal.current('7')
    record.update(status='approved', debt=10, debt_since='2026-01-01T00:00:00Z',
                  debt_charges=[{'id': 'charge1', 'original_amount': 10, 'outstanding_amount': 10}])
    config = record['configs'][0]
    config.update(debt_charge_id='charge1', days=10, gb=1,
                  debt_policy_hold_snapshot={'held_at': '2026-01-06T00:00:00Z', 'used_bytes': 0, 'quota_bytes': 1024**3})
    state_store._save_reseller_record(database.get_connection(), '7', record)
    success, result = reseller_removal.cleanup_banned('7', panels, actor='scheduler', _mode='debt')
    assert success
    assert result['writeoff'] == 5
    assert result['remaining_debt'] == 5
    saved = reseller_removal.current('7')
    assert saved['configs'][0]['debt_proration'][0]['writeoff'] == 5
    assert calls == ['alice']
