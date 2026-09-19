import copy
import json

import pytest

from utils import account_operations as operations, bulk_transfer, database, operation_recovery, state_store
from utils.api_client import BulkUserTransferSpec


class Panel:
    panel_type = 'blitz'

    def __init__(self, ident, users=None):
        self.server_id = self.server_name = ident
        self.users = users or {}
        self.deletions = []
        self.timeout = False

    def get_users(self):
        return list(copy.deepcopy(self.users).values())

    def get_user_result(self, name):
        assert not database.get_connection().in_transaction
        return {'status': 'found', 'data': copy.deepcopy(self.users[name])} if name in self.users else {'status': 'missing'}

    def delete_user(self, name):
        assert not database.get_connection().in_transaction
        self.deletions.append(name)
        self.users.pop(name, None)
        if self.timeout:
            raise TimeoutError('lost deletion response')
        return {'ok': True}

    def get_user_uri(self, name):
        return {'normal_sub': 'https://example.test/subscription'}


class Panels:
    def __init__(self):
        self.source = Panel('source', {'alice': {
            'username': 'alice', 'password': 'synthetic', 'max_download_bytes': 10 * 1024**3,
            'upload_bytes': 0, 'download_bytes': 0, 'expiration_days': 30, 'blocked': False,
            'unlimited_user': False, 'delayed_start': True, 'status': 'on hold', 'note': None,
        }})
        self.destination = Panel('destination')
        self.servers = [{'id': p.server_id, 'name': p.server_name, 'panel': p.panel_type}
                        for p in (self.source, self.destination)]
        self.copies = 0
        self.timeout = False

    def get_client(self, ident):
        return {'source': self.source, 'destination': self.destination}.get(ident)

    def copy_user(self, spec):
        def create():
            assert not database.get_connection().in_transaction
            self.copies += 1
            self.destination.users['alice'] = copy.deepcopy(self.source.users['alice'])
            if self.timeout:
                raise TimeoutError('lost creation response')
            return {'success': True}
        operations.step(operations.active_workflow(), 'create_destination', {'username': 'alice'}, create)
        return {'ok': True, 'destination_server_id': 'destination', 'panel_type': 'blitz'}


@pytest.fixture
def migration(storage):
    panels = Panels()
    state_store._save_payment_record(database.get_connection(), 'main', 'p1', {
        'username': 'alice', 'server_id': 'source', 'user_id': 123, 'status': 'completed', 'price': 10,
    })
    spec = BulkUserTransferSpec(mode='migrate', source_server_id='source', destination_server_id='destination',
                               requesting_admin='42', notification_policy='send')
    preview = bulk_transfer.preflight_transfer(spec, panels)
    assert preview['ok']
    result = bulk_transfer.create_transfer_job(spec, preview)
    assert result['ok']
    return result['job_id'], panels


def original_operation(job):
    return database.get_connection().execute('SELECT operation_id FROM account_operations').fetchone()[0]


def assert_completed(job, panels):
    ident = original_operation(job)
    assert operations.details(ident)['phase'] == 'completed'
    assert panels.copies == 1
    assert panels.source.deletions == ['alice']
    payment = json.loads(database.get_connection().execute("SELECT payload_json FROM payments WHERE payment_id='p1'").fetchone()[0])
    assert payment['server_id'] == 'destination'
    assert not database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()
    assert bulk_transfer.job_counts(job)['completed'] == 1


def test_migration_retains_both_claims_until_references_and_source_finish(migration):
    job, panels = migration
    delete = panels.source.delete_user
    def check(name):
        with pytest.raises(operations.AccountBusy):
            operations.assert_available('source', name)
        with pytest.raises(operations.AccountBusy):
            operations.assert_available('destination', name)
        return delete(name)
    panels.source.delete_user = check
    bulk_transfer.run_transfer_job(job, multi_api=panels)
    assert_completed(job, panels)


def test_lost_copy_response_resumes_original_without_another_copy(migration):
    job, panels = migration
    panels.timeout = True
    bulk_transfer.run_transfer_job(job, multi_api=panels)
    ident = original_operation(job)
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'resume_migration', report
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert_completed(job, panels)


def test_lost_delete_response_completes_without_replaying_mutation(migration):
    job, panels = migration
    panels.source.timeout = True
    bulk_transfer.run_transfer_job(job, multi_api=panels)
    ident = original_operation(job)
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'complete_accounting', report
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert_completed(job, panels)


@pytest.mark.parametrize('change', ['credential', 'entitlement', 'ownership', 'generation'])
def test_migration_changed_evidence_never_deletes_source(migration, change):
    job, panels = migration
    panels.timeout = True
    bulk_transfer.run_transfer_job(job, multi_api=panels)
    ident = original_operation(job)
    before = operation_recovery.inspect(ident, panels)
    if change == 'credential':
        panels.destination.users['alice']['password'] = 'different'
    elif change == 'entitlement':
        panels.destination.users['alice']['max_download_bytes'] *= 2
    elif change == 'generation':
        panels.source.users['alice']['password'] = 'new-generation'
    else:
        state_store._save_payment_record(database.get_connection(), 'main', 'p1', {
            'username': 'alice', 'server_id': 'source', 'user_id': 999, 'status': 'completed',
        })
    with pytest.raises(ValueError, match='Evidence changed'):
        operation_recovery.reconcile(ident, panels, before['evidence_digest'])
    assert operation_recovery.inspect(ident, panels)['action'] == 'none'
    assert panels.source.deletions == []
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 2


@pytest.mark.parametrize('timeout', [False, True])
def test_admin_copy_and_recovery_never_delete_source_or_repeat_creation(storage, timeout):
    from utils import copy_operations
    from utils.api_client import UserCopySpec, UserRef
    panels = Panels()
    panels.timeout = timeout
    spec = UserCopySpec(source=UserRef(server_id='source', username='alice', panel_type='blitz'), destination_server_id='destination')
    if timeout:
        with pytest.raises(TimeoutError):
            copy_operations.copy('copy:1', panels, spec, 1)
        report = operation_recovery.inspect('copy:1', panels)
        assert report['action'] == 'complete_accounting'
        operation_recovery.reconcile('copy:1', panels, report['evidence_digest'])
    else:
        assert copy_operations.copy('copy:1', panels, spec, 1)['ok']
    assert copy_operations.copy('copy:1', panels, spec, 1)['ok']
    assert panels.copies == 1 and panels.source.deletions == []
    assert operations.details('copy:1')['phase'] == 'completed'
