import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'posix', reason='Shared Linux account lock')
def test_account_claim_is_durable_before_panel_io_and_duplicate_is_cached(storage):
    from utils import account_operations as operations, database
    calls = []
    def panel():
        assert not database.get_connection().in_transaction
        # Another connection sees the committed intent before the panel call.
        import sqlite3
        with sqlite3.connect(database.database_path()) as db:
            assert db.execute('SELECT status FROM account_operations').fetchone()[0] == 'executing'
        calls.append(True)
        return {'success': True, 'username': 'account', 'server_id': 'server', 'after_state': {'cycle': 'new'}}
    args = ('payment:1', 'server', 'account', 'renewal', {'cycle': 'old'})
    assert operations.execute(*args, panel)['success']
    assert operations.execute(*args, panel)['reconciled']
    assert calls == [True]


@pytest.mark.skipif(os.name != 'posix', reason='Shared Linux account lock')
def test_timeout_retains_claim_and_blocks_different_order(storage):
    from utils import account_operations as operations
    def panel():
        raise TimeoutError('synthetic response lost after panel success')
    with pytest.raises(TimeoutError):
        operations.execute('one', 'server', 'account', 'renewal', {}, panel)
    assert operations.existing('one')['status'] == 'uncertain'
    for ident in ('one', 'two'):
        with pytest.raises(operations.AccountBusy):
            operations.execute(ident, 'server', 'account', 'renewal', {}, lambda: pytest.fail('must not replay'))


@pytest.mark.skipif(os.name != 'posix', reason='Shared Linux account lock')
def test_process_death_keeps_external_outcome_uncertain(storage):
    from utils import account_operations as operations
    source = '''from utils import account_operations as a
import os
a.execute('killed', 'server', 'account', 'renewal', {}, lambda: os._exit(17))
'''
    environment = dict(os.environ)
    from pathlib import Path
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot')
    result = subprocess.run([sys.executable, '-c', source], env=environment)
    assert result.returncode == 17
    assert operations.existing('killed')['status'] == 'executing'
    with pytest.raises(operations.AccountBusy):
        operations.execute('other', 'server', 'account', 'renewal', {}, lambda: pytest.fail('must not replay'))


@pytest.mark.skipif(os.name != 'posix', reason='Shared Linux account lock')
def test_another_process_cannot_mutate_a_locked_account(storage):
    from utils import account_operations as operations
    from pathlib import Path
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot')}
    with operations.serialize('server', 'account'):
        result = subprocess.run([sys.executable, '-c', '''from utils import account_operations as a
try:
    with a.serialize('server', 'account'): raise SystemExit(9)
except a.AccountBusy: raise SystemExit(0)
'''], env=env)
    assert result.returncode == 0


def test_mutation_inside_write_transaction_is_rejected(storage):
    from utils import account_operations as operations, database
    with database.transaction():
        with pytest.raises(operations.AccountBusy, match='transaction'):
            with operations.serialize('server', 'account'):
                pytest.fail('must not allow panel I/O')


@pytest.fixture
def renewal_panel(storage):
    from copy import deepcopy
    from utils import database, renewal
    class Panel:
        server_id = 'server'
        calls = 0
        fail = False
        user = {'username': 'account', 'blocked': True, 'expiration_days': 30,
                'account_creation_date': '2020-01-01T00:00:00+00:00',
                'upload_bytes': 1024**3, 'download_bytes': 2*1024**3,
                'max_download_bytes': 5*1024**3, 'status': 'expired'}

        def resolve_unique_user(self, username, **kwargs):
            assert not database.get_connection().in_transaction
            return self, deepcopy(self.user), {'status': 'found', 'uniqueness_verified': True, 'actual_server_id': self.server_id}

        def renew_user_result(self, username, gb, days, unlimited):
            assert not database.get_connection().in_transaction
            self.calls += 1
            self.user = {**self.user, 'blocked': False, 'status': 'active', 'upload_bytes': 0,
                         'download_bytes': 0, 'account_creation_date': '2026-09-12T00:00:00+00:00'}
            if self.fail:
                return {'status': 'unavailable', 'stage': 'verify', 'error': 'synthetic_timeout'}
            return {'status': 'succeeded', 'user': deepcopy(self.user), 'data': {'ok': True}}

        def get_user(self, username):
            assert not database.get_connection().in_transaction
            return deepcopy(self.user)

    panel = Panel()
    record = {'renewal_username': 'account', 'renewal_server_id': 'server', 'plan_gb': '5',
              'days': 30, 'unlimited': False, 'mutation_operation_id': 'main-payment:renew-1',
              'renewal_before_state': {**renewal.capture_user_state(panel.user), 'cycle_fingerprint': 'ledger-cycle'}}
    return panel, record


def test_sqlite_renewal_uses_panel_generation_and_replays_only_accounting(renewal_panel):
    from utils import renewal, account_operations
    panel, record = renewal_panel
    result = renewal.execute_customer_renewal(record, multi_api=panel)
    assert result['success'], result
    replay = renewal.execute_customer_renewal(record, multi_api=panel)
    assert replay['success'] and replay['reconciled']
    assert panel.calls == 1
    intent = json.loads(account_operations.existing(record['mutation_operation_id'])['request_json'])
    assert intent['cycle_fingerprint'] == 'ledger-cycle'


def test_sqlite_renewal_lost_response_blocks_other_bot_and_web_mutations(renewal_panel):
    from utils import renewal, reseller_blocks, account_operations
    panel, record = renewal_panel
    panel.fail = True
    assert renewal.execute_customer_renewal(record, multi_api=panel)['uncertain']
    for operation_id in (record['mutation_operation_id'], 'main-payment:another'):
        result = renewal.execute_customer_renewal({**record, 'mutation_operation_id': operation_id}, multi_api=panel)
        assert result['uncertain']
    with pytest.raises(account_operations.AccountBusy):
        reseller_blocks.set_admin_block(panel, 'account', False, panel.user)
    assert panel.calls == 1


def test_sqlite_renewal_rejects_changed_generation(renewal_panel):
    from utils import renewal
    panel, record = renewal_panel
    panel.user = {**panel.user, 'account_creation_date': '2021-01-01T00:00:00+00:00'}
    result = renewal.execute_customer_renewal(record, multi_api=panel)
    assert result['reason'] == 'renewal_cycle_changed'
    assert panel.calls == 0


def test_unexpected_panel_exception_is_reported_as_uncertain_to_payment_handlers(renewal_panel):
    from utils import renewal, account_operations
    panel, record = renewal_panel
    def lost_response(*args):
        raise TimeoutError('synthetic transport interruption')
    panel.renew_user_result = lost_response
    result = renewal.execute_customer_renewal(record, multi_api=panel)
    assert result['uncertain']
    assert account_operations.existing(record['mutation_operation_id'])['status'] == 'uncertain'


def test_sqlite_admin_block_commits_without_holding_a_write_transaction(renewal_panel):
    from utils import database, reseller_blocks, reseller
    panel, _ = renewal_panel
    with reseller.reseller_lock, reseller._resellers_file_lock():
        reseller._write_resellers_file({'7': {'status': 'approved', 'configs': [
            {'username': 'account', 'server_id': 'server'}]}})
    panel.user = {**panel.user, 'blocked': False}
    def update(username, values):
        assert not database.get_connection().in_transaction
        panel.user.update(values)
        return {'ok': True}
    panel.update_user = update
    assert reseller_blocks.set_admin_block(panel, 'account', True, panel.user)
    with reseller.reseller_lock, reseller._resellers_file_lock():
        config = reseller._read_resellers_file()['7']['configs'][0]
    assert config['admin_blocked'] and not config['block_reconcile_pending']
    assert panel.user['blocked']
