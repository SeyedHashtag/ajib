import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def interrupted(storage, monkeypatch):
    from utils.account_credit import credit_account
    from utils.web_services import Services
    from utils.web_orders import Orders
    from utils import database
    credit_account(123, 2, 'synthetic-credit')
    class Panels:
        server_id = 'server'
        user = None
        calls = 0
        verified = True
        def select_server_for_new_user(self):
            return self
        def get_client(self, server):
            assert server == self.server_id
            return self
        def add_user(self, username, gb, days, unlimited=False, note=None):
            assert not database.get_connection().in_transaction
            self.calls += 1
            self.user = {'username': username, 'max_download_bytes': gb*1024**3, 'expiration_days': days,
                         'unlimited_ip': unlimited, 'note': note, 'upload_bytes': 0, 'download_bytes': 0}
            return None  # Request applied, response lost.
        def resolve_unique_user(self, username, **kwargs):
            assert not database.get_connection().in_transaction
            return self, dict(self.user) if self.user else None, {
                'status': 'found' if self.user else 'missing', 'uniqueness_verified': self.verified}
    panels = Panels()
    services = Services(panels)
    orders = Orders(services)
    payment = orders.create('123', 'main', 'interrupted-purchase-key', '40', 'crypto', 'fa')
    assert orders.process_one()
    return panels, services, orders, payment['id'], 'main-payment:' + payment['id']


def test_inspection_is_read_only_and_omits_note_and_configuration(interrupted):
    from utils import database, operation_recovery
    panels, _, _, _, ident = interrupted
    panels.user['uri'] = 'secret-config-must-not-appear'
    before = database.get_connection().total_changes
    report = operation_recovery.inspect(ident, panels)
    assert report['classification'] == 'panel_verified'
    assert report['action'] == 'complete_accounting'
    assert 'secret-config' not in json.dumps(report)
    assert panels.user['note'] not in json.dumps(report)
    assert database.get_connection().total_changes == before


def test_reconciliation_consumes_reserved_credit_once_without_recreating(interrupted):
    from utils import database, operation_recovery, account_operations
    from utils.account_credit import get_account_credit
    panels, services, orders, payment, ident = interrupted
    assert get_account_credit(123)['reserved'] == 1.2
    with pytest.raises(account_operations.AccountBusy):
        account_operations.assert_available('server', panels.user['username'])
    report = operation_recovery.inspect(ident, panels)
    assert operation_recovery.reconcile(ident, panels, report['evidence_digest'])['applied']
    assert panels.calls == 1
    assert not orders.process_one()
    assert services.payment(123, 'main', payment)['status'] == 'completed'
    assert get_account_credit(123)['reserved'] == 0
    assert account_operations.details(ident)['phase'] == 'completed'
    account_operations.assert_available('server', panels.user['username'])
    report = operation_recovery.inspect(ident, panels)
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert database.get_connection().execute("SELECT COUNT(*) FROM web_outbox WHERE id=?", ('complete:' + payment,)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['note', 'plan', 'identity', 'missing', 'partial'])
def test_ambiguous_creation_keeps_funds_and_claim(interrupted, change):
    from utils import operation_recovery, account_operations
    from utils.account_credit import get_account_credit
    panels, _, _, _, ident = interrupted
    if change == 'note': panels.user['note'] = ''
    if change == 'plan': panels.user['max_download_bytes'] = 1024
    if change == 'identity': panels.server_id = 'other'
    if change == 'missing': panels.user = None
    if change == 'partial': panels.verified = False
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'none'
    with pytest.raises(ValueError, match='uncertain'):
        operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert account_operations.details(ident)['phase'] == 'uncertain'
    assert get_account_credit(123)['reserved'] == 1.2


def test_evidence_cannot_be_reused_after_payment_change(interrupted):
    from utils import operation_recovery, database
    from utils.web_orders import save_payment
    panels, _, _, payment, ident = interrupted
    report = operation_recovery.inspect(ident, panels)
    with database.transaction() as db:
        save_payment(db, 'main', payment, {'price': 2})
    with pytest.raises(ValueError, match='Evidence changed'):
        operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert panels.calls == 1


def test_accounting_failure_rolls_back_claim_credit_payment_and_notification(interrupted, monkeypatch):
    from utils import operation_recovery, account_operations, web_store
    from utils.account_credit import get_account_credit
    panels, services, _, payment, ident = interrupted
    report = operation_recovery.inspect(ident, panels)
    def fail(*args):
        raise RuntimeError('synthetic outbox storage failure')
    monkeypatch.setattr(web_store, 'enqueue', fail)
    with pytest.raises(RuntimeError):
        operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert account_operations.details(ident)['phase'] == 'uncertain'
    assert services.payment(123, 'main', payment)['status'] == 'uncertain'
    assert get_account_credit(123)['reserved'] == 1.2


def test_renewal_appearance_is_not_proof_of_dispatched_reset(interrupted):
    from utils import operation_recovery, database
    panels, _, _, _, ident = interrupted
    with database.transaction() as db:
        db.execute("UPDATE account_operations SET kind='renewal' WHERE operation_id=?", (ident,))
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'none'
    assert report['reason'] == 'renewal_dispatch_outcome_unproven'


def test_claim_is_retained_after_panel_verification_until_accounting(storage):
    from utils import account_operations as operations
    operations.execute('manual-op', 'server', 'Alice', 'update', {}, lambda: {'success': True})
    assert operations.details('manual-op')['phase'] == 'panel_verified'
    with pytest.raises(operations.AccountBusy):
        operations.assert_available('server', 'alice')
    operations.complete('manual-op')
    operations.assert_available('server', 'ALICE')


def test_multiple_resources_are_claimed_atomically(storage):
    from utils import account_operations as operations
    operations.execute('move', 'a', 'alice', 'migration', {}, lambda: {'success': True}, resources=[('b', 'alice')])
    for server in ('a', 'b'):
        with pytest.raises(operations.AccountBusy):
            operations.assert_available(server, 'alice')
    operations.complete('move')
    for server in ('a', 'b'):
        operations.assert_available(server, 'alice')


@pytest.mark.parametrize('scope,owner', [('main', 'bot'), ('main', 'web'), ('hosted:7', 'hosted')])
def test_trial_timeout_recovery_preserves_global_eligibility(storage, scope, owner):
    from utils import trial_operations, operation_recovery, database
    from utils.atomic_store import read_json
    from utils.trial_state import _has_used_test_config_from
    calls, users = [], {}
    def add(username, gb, days, unlimited=False, note=None):
        assert not database.get_connection().in_transaction
        calls.append(username)
        users[username] = {'username': username, 'max_download_bytes': gb*1024**3,
                           'expiration_days': days, 'unlimited_ip': unlimited, 'note': note}
        return None
    panel = SimpleNamespace(server_id='s1', add_user=add)
    panels = SimpleNamespace(prepare_new_user_creation=lambda **kw: {'client': panel, 'existing_usernames': set()},
        get_client=lambda ident: panel, record_created_user=lambda *args: None,
        resolve_unique_user=lambda username, **kw: (panel, users.get(username), {'status': 'found', 'uniqueness_verified': True}))
    ident = trial_operations.claim(123, scope=scope, owner=owner, language='fa')
    from utils.account_operations import AccountBusy
    with pytest.raises(AccountBusy):
        trial_operations.create(ident, 123, panels, lambda names: 'trial123', {'gb': 1, 'days': 30, 'unlimited': True})
    assert not trial_operations.release_unallocated(123)
    assert _has_used_test_config_from(read_json(trial_operations.CONFIGS, {}), 123, now='2099-01-01T00:00:00Z')
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'complete_accounting'
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    entry = read_json(trial_operations.CONFIGS, {})['123']
    assert entry['username'] == 'trial123' and entry['used_at']
    assert not entry.get('account_operation_id')
    assert calls == ['trial123']
    assert database.get_connection().execute('SELECT scope FROM web_outbox WHERE id=?', (ident,)).fetchone()[0] == scope


def test_changed_quote_before_preview_cannot_authorize_completion(interrupted):
    from utils import operation_recovery, database
    from utils.web_orders import save_payment
    panels, _, _, payment, ident = interrupted
    with database.transaction() as db:
        save_payment(db, 'main', payment, {'price': 99})
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'none' and report['reason'] == 'obligation_changed'


def test_alias_reservation_cannot_be_released_after_dispatch(storage):
    from utils import account_operations as operations, database
    from utils.account_credit import credit_account, reserve_account_credit, release_account_credit
    from utils.web_orders import save_payment
    credit_account(123, 5, 'synthetic')
    reserve_account_credit(123, 'original-checkout', 2)
    with database.transaction() as db:
        save_payment(db, 'main', 'provider-payment', {'user_id': 123, 'price': 1,
            'status': 'approved', 'account_credit_reservation_id': 'original-checkout'})
    operations.execute('main-payment:provider-payment', 's1', 'alice', 'create', {}, lambda: {'success': False})
    with pytest.raises(operations.AccountBusy):
        release_account_credit(123, 'original-checkout')


def test_funding_recovery_finishes_original_split_once(storage, monkeypatch):
    from utils import reseller, reseller_funding as funding, reseller_wholesale_credit as wallet
    from utils import account_operations as operations, operation_recovery
    monkeypatch.setattr(reseller, '_update_recruitment_milestone', lambda *args: None)
    reseller.save_resellers({'7': {'status': 'approved', 'debt': 0, 'total_paid': 0, 'configs': []}})
    wallet.credit_wholesale_balance(7, 2, 'synthetic-topup')
    funding.reserve_funding(7, 'original-order', 5, metadata={'origin': 'main'})
    ident = 'reseller-create:7:original-order'
    operations.execute(ident, 's1', 'alice', 'create', {}, lambda: {'success': True},
        origin={'type': 'funding', 'scope': 'reseller:7', 'id': 'original-order', 'reseller_id': '7'})
    panel = SimpleNamespace(server_id='s1')
    panels = SimpleNamespace(resolve_unique_user=lambda *args, **kw: (panel, {'username': 'alice'},
        {'status': 'found', 'uniqueness_verified': True}))
    assert operation_recovery.inspect(ident, panels)['action'] == 'none'
    funding.remember_fulfillment(7, 'original-order', {'username': 'alice', 'server_id': 's1',
        'price': 5, 'gb': 5, 'days': 30})
    report = operation_recovery.inspect(ident, panels)
    assert report['action'] == 'complete_accounting'
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert operations.details(ident)['phase'] == 'completed'
    assert funding.get_funding(7, 'original-order')['status'] == 'completed'
    assert reseller.get_reseller_data(7)['debt'] == 3
    assert wallet.get_wholesale_balance(7)['reserved'] == 0
    report = operation_recovery.inspect(ident, panels)
    operation_recovery.reconcile(ident, panels, report['evidence_digest'])
    assert len(reseller.get_reseller_data(7)['configs']) == 1
    assert reseller.get_reseller_data(7)['debt'] == 3
