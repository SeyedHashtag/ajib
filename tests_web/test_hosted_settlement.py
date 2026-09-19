import copy
import json

import pytest

from utils import account_operations as operations, database, hosted_settlement as settlement, state_store, web_store


@pytest.fixture
def sale(storage, monkeypatch):
    from utils import hosted_bots, reseller, reseller_wholesale_credit as prepaid
    monkeypatch.setattr(hosted_bots, 'HOSTED_ROOT', str(storage / 'hosted_bots'))
    monkeypatch.setattr(reseller, 'RESELLERS_FILE', str(storage / 'resellers.json'))
    record = {'user_id': 123, 'status': 'processing', 'plan_gb': 10, 'days': 30, 'retail_price': 12,
              'wholesale_price': 10, 'margin': 2, 'referral_reward': 1, 'wholesale_prepaid': True,
              'payment_method': 'card', 'fulfillment_owner': 'hosted'}
    db = database.get_connection()
    state_store._save_payment_record(db, 'hosted:7', 'p1', record)
    state_store._save_reseller_record(db, '7', {'status': 'active', 'debt': 0, 'total_paid': 0, 'configs': []})
    state_store._save_referrals(db, 'hosted:7', {'referrals': {'123': '456'}, 'stats': {}})
    prepaid.credit_wholesale_balance('7', 20, 'synthetic-topup')
    assert prepaid.reserve_wholesale_balance('7', 'p1', 10) == 10
    settlement.prepare('7', 'p1', record, False, bot_id='synthetic')
    operations.execute('hosted-payment:7:p1', 's1', 'alice', 'create', {'gb': 10, 'days': 30},
                       lambda: {'success': True, 'username': 'alice', 'server_id': 's1'})
    return record


def balances():
    from utils.reseller_wholesale_credit import get_wholesale_balance
    from utils.hosted_bots import get_ledger
    return get_wholesale_balance('7'), get_ledger('7')


def test_hosted_accounting_outbox_and_claim_commit_once(sale):
    from utils.reseller import get_reseller_data
    for _ in range(2):
        result = settlement.finalize('7', 'p1')
        assert result['status'] == 'completed'
    db = database.get_connection()
    assert operations.details('hosted-payment:7:p1')['phase'] == 'completed'
    assert not db.execute('SELECT 1 FROM account_operation_claims').fetchone()
    reseller = get_reseller_data('7')
    assert len(reseller['configs']) == 1
    assert reseller['total_paid'] == 10
    prepaid, ledger = balances()
    assert prepaid['reserved'] == 0 and prepaid['available'] == 10
    assert ledger['earnings_available'] == 2
    assert ledger['referral_liability'] == 1
    referrals = state_store._load_referrals(db, 'hosted:7')
    assert referrals['stats']['456']['available_balance'] == 1
    assert db.execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 2


def test_notification_failure_rolls_back_all_accounting_and_retains_claim(sale, monkeypatch):
    before = copy.deepcopy(balances())
    with monkeypatch.context() as patch:
        patch.setattr(web_store, 'enqueue', lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic outbox failure')))
        with pytest.raises(RuntimeError, match='outbox'):
            settlement.finalize('7', 'p1')
    assert balances() == before
    assert settlement.payment('7', 'p1')['status'] == 'processing'
    assert database.get_connection().execute('SELECT 1 FROM account_operation_claims').fetchone()
    assert operations.details('hosted-payment:7:p1')['phase'] == 'panel_verified'
    assert settlement.finalize('7', 'p1')['status'] == 'completed'


@pytest.mark.parametrize('field,value', [('wholesale_price', 11), ('referral_reward', 0), ('user_id', 999)])
def test_changed_financial_terms_or_owner_cannot_finalize(sale, field, value):
    record = settlement.payment('7', 'p1')
    record[field] = value
    state_store._save_payment_record(database.get_connection(), 'hosted:7', 'p1', record)
    with pytest.raises(operations.AccountBusy, match='terms changed'):
        settlement.finalize('7', 'p1')
    assert balances()[0]['reserved'] == 10


def test_cli_completes_verified_original_hosted_obligation(sale, monkeypatch):
    from types import SimpleNamespace
    from utils import operation_recovery, reseller
    original_lock = reseller.reseller_lock
    class OrderedLock:
        depth = 0
        def __enter__(self):
            if not self.depth:
                assert not database.get_connection().in_transaction
            original_lock.__enter__()
            self.depth += 1
            return self
        def __exit__(self, *args):
            self.depth -= 1
            return original_lock.__exit__(*args)
    monkeypatch.setattr(reseller, 'reseller_lock', OrderedLock())
    panels = SimpleNamespace(resolve_unique_user=lambda *args, **kwargs: (
        SimpleNamespace(server_id='s1'), {'username': 'alice'}, {'status': 'found', 'uniqueness_verified': True}))
    report = operation_recovery.inspect('hosted-payment:7:p1', panels)
    assert report['action'] == 'complete_accounting', report
    operation_recovery.reconcile('hosted-payment:7:p1', panels, report['evidence_digest'])
    assert settlement.payment('7', 'p1')['status'] == 'completed'


def test_missing_legacy_intent_cannot_be_reconstructed(sale):
    database.get_connection().execute("DELETE FROM kv_state WHERE namespace='hosted_settlement'")
    with pytest.raises(operations.AccountBusy, match='provenance'):
        settlement.prepare('7', 'p1', sale, False)
    with pytest.raises(operations.AccountBusy, match='provenance'):
        settlement.finalize('7', 'p1')


@pytest.fixture
def reserved(sale):
    db = database.get_connection()
    for table in ('account_operation_claims', 'account_operation_events', 'account_operation_details', 'account_operations'):
        db.execute('DELETE FROM ' + table)
    db.execute("DELETE FROM kv_state WHERE namespace='hosted_settlement'")
    record = {**sale, 'renew_username': 'alice', 'server_id': 's1', 'renewal_mode': 'reserved',
              'renewal_baseline': {'account_creation_date': '2026-01-01', 'max_download_bytes': 10 * 1024**3, 'expiration_days': 30}}
    state_store._save_payment_record(db, 'hosted:7', 'p1', record)
    state_store._save_reseller_record(db, '7', {'status': 'approved', 'debt': 0, 'total_paid': 0, 'configs': [
        {'username': 'alice', 'server_id': 's1', 'customer_telegram_id': 123, 'price': 10, 'days': 30, 'plan_gb': 10}]})
    settlement.prepare('7', 'p1', record, False)
    return record


def test_reserved_purchase_settles_once_without_panel_dispatch(reserved):
    for _ in range(2):
        settlement.prepare('7', 'p1', settlement.payment('7', 'p1'), False)
        result = settlement.reserve('7', 'p1')
        assert result['status'] == 'completed' and result['renewal_status'] == 'reserved'
    assert not database.get_connection().execute('SELECT 1 FROM account_operations').fetchone()
    assert balances()[0]['reserved'] == 0
    assert balances()[1]['earnings_available'] == 2
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 2


def test_scheduled_hosted_completion_updates_history_and_outbox_atomically(reserved, monkeypatch):
    from utils import renewal, reseller
    from utils.hosted_bots import tenant_file
    settlement.reserve('7', 'p1')
    path = tenant_file('7', 'payments.json')
    claim = renewal.claim_payment_renewal('p1', payments_file=path)
    result = {'success': True, 'before_state': reserved['renewal_baseline'],
              'after_state': {**reserved['renewal_baseline'], 'account_creation_date': '2026-09-19'}}
    operations.execute('hosted-payment:7:p1', 's1', 'alice', 'renewal', {'target': {}}, lambda: result)
    with monkeypatch.context() as patch:
        patch.setattr(web_store, 'enqueue', lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic notification failure')))
        with pytest.raises(RuntimeError):
            renewal.finish_payment_renewal('p1', claim['claim_id'], 'applied', payments_file=path)
    assert settlement.payment('7', 'p1')['renewal_status'] == 'processing'
    assert operations.details('hosted-payment:7:p1')['phase'] == 'panel_verified'
    assert reseller.get_reseller_data('7')['configs'][0]['renewals'][0]['renewal_status'] == 'reserved'
    assert renewal.finish_payment_renewal('p1', claim['claim_id'], 'applied', payments_file=path)
    assert settlement.payment('7', 'p1')['renewal_status'] == 'applied'
    assert reseller.get_reseller_data('7')['configs'][0]['renewals'][0]['renewal_status'] == 'applied'
    assert operations.details('hosted-payment:7:p1')['phase'] == 'completed'


def test_hosted_reservation_cannot_be_claimed_by_main_scheduler(reserved):
    from utils import reseller
    settlement.reserve('7', 'p1')
    assert reseller.claim_reseller_renewal_reservation('7', 'p1', force=True) is None


def test_reseller_reserved_recovery_commits_notification_and_history_together(storage, monkeypatch):
    from types import SimpleNamespace
    from utils import reseller, reserved_completion, operation_recovery
    monkeypatch.setattr(reseller, 'RESELLERS_FILE', str(storage / 'resellers.json'))
    entry = {'username': 'alice', 'server_id': 's1', 'price': 10, 'renewals': [{
        'reservation_id': 'r1', 'price': 10, 'plan_gb': 10, 'days': 30, 'renewal_mode': 'reserved',
        'renewal_status': 'processing', 'renewal_claim_id': 'claim', 'renewal_source': 'reseller_customer'}]}
    state_store._save_reseller_record(database.get_connection(), '7', {'status': 'approved', 'debt': 0, 'configs': [entry]})
    terms = reserved_completion.reseller_terms(reserved_completion.reseller_obligation('7', 'r1'))
    after = {'account_creation_date': '2026-09-19', 'max_download_bytes': 10 * 1024**3, 'expiration_days': 30}
    operations.execute('reseller-reservation:7:r1', 's1', 'alice', 'renewal', {}, lambda: {'success': True, 'after_state': after},
                       origin={'type': 'reseller_reservation', 'scope': 'reseller:7', 'id': 'r1', 'reseller_id': '7', 'terms_digest': terms})
    panels = SimpleNamespace(resolve_unique_user=lambda *args, **kwargs: (
        SimpleNamespace(server_id='s1'), after, {'status': 'found', 'uniqueness_verified': True}))
    report = operation_recovery.inspect('reseller-reservation:7:r1', panels)
    assert report['action'] == 'complete_accounting'
    with monkeypatch.context() as patch:
        patch.setattr(web_store, 'enqueue', lambda *args: (_ for _ in ()).throw(RuntimeError('outbox')))
        with pytest.raises(RuntimeError):
            operation_recovery.reconcile('reseller-reservation:7:r1', panels, report['evidence_digest'])
    assert reserved_completion.reseller_obligation('7', 'r1')['reservation']['renewal_status'] == 'processing'
    assert operations.details('reseller-reservation:7:r1')['phase'] == 'panel_verified'
    operation_recovery.reconcile('reseller-reservation:7:r1', panels, report['evidence_digest'])
    assert reserved_completion.reseller_obligation('7', 'r1')['reservation']['renewal_status'] == 'applied'
    assert operations.details('reseller-reservation:7:r1')['phase'] == 'completed'
