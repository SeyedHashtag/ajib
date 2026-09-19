from types import SimpleNamespace
import pytest


@pytest.fixture
def panel(storage):
    from utils import database
    state = {'username': 'alice', 'account_creation_date': '2026-01-01', 'expiration_days': 30,
             'max_download_bytes': 1024**3}
    calls = []
    def update(username, changes):
        assert not database.get_connection().in_transaction
        calls.append(('update', username, changes))
        return {'ok': True}
    return SimpleNamespace(server_id='s1', get_user=lambda username: dict(state), update_user=update, calls=calls)


def test_admin_edit_cannot_override_unsettled_purchase(panel):
    from utils import account_operations as operations
    from utils.admin_account_operations import mutate
    operations.execute('customer-order', 's1', 'Alice', 'renewal', {}, lambda: {'success': True})
    assert mutate('admin-edit', panel, 'alice', changes={'new_traffic_limit': 10}) is None
    assert panel.calls == []


def test_successful_admin_edit_is_idempotent_and_audited(panel):
    from utils import account_operations as operations, database
    from utils.admin_account_operations import mutate
    for _ in range(2):
        assert mutate('admin-edit', panel, 'alice', changes={'new_traffic_limit': 10}, actor='1')
    assert len(panel.calls) == 1
    assert operations.details('admin-edit')['phase'] == 'completed'
    assert database.get_connection().execute("SELECT actor FROM account_operation_events WHERE phase='admin_effect_committed' LIMIT 1").fetchone()[0] == '1'


def test_uncertain_admin_edit_blocks_retry_and_customer_renewal(panel):
    from utils import account_operations as operations
    from utils.admin_account_operations import mutate
    def lose_response(*args):
        panel.calls.append(args)
        return None
    panel.update_user = lose_response
    assert mutate('admin-edit', panel, 'alice', changes={'renew_creation_date': True}) is None
    assert mutate('admin-edit', panel, 'alice', changes={'renew_creation_date': True}) is None
    assert len(panel.calls) == 1
    with pytest.raises(operations.AccountBusy):
        operations.assert_available('s1', 'alice')


def test_rename_does_not_orphan_ownership_records(panel):
    from utils.admin_account_operations import mutate
    assert mutate('rename', panel, 'alice', changes={'new_username': 'bob'}) is None
    assert not panel.calls
