import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

UTILS = Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot/utils'
NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def modules(tmp_path, monkeypatch):
    saved = {key: value for key, value in sys.modules.items() if key == 'utils' or key.startswith('utils.')}
    for key in saved:
        sys.modules.pop(key)
    package = types.ModuleType('utils')
    package.__path__ = [str(UTILS)]
    sys.modules['utils'] = package
    monkeypatch.setenv('AJIB_SQLITE_ACTIVE', '0')
    monkeypatch.setenv('AJIB_DB_PATH', str(tmp_path / 'ajib.db'))
    monkeypatch.setenv('AJIB_BOT_DIR', str(tmp_path))
    store = importlib.import_module('utils.reseller')
    blocks = importlib.import_module('utils.reseller_blocks')
    experience = importlib.import_module('utils.reseller_experience')
    store.RESELLERS_FILE = str(tmp_path / 'resellers.json')
    monkeypatch.setattr(store, 'utc_now', lambda: NOW)
    monkeypatch.setattr(blocks, 'utc_now', lambda: NOW)
    # Accounting timestamps use the same fixed clock as policy evaluation.
    monkeypatch.setattr(store, '_now_str', lambda: NOW.isoformat())
    yield store, blocks, experience
    database = sys.modules.get('utils.database')
    if database:
        database.close_connections()
    for key in list(sys.modules):
        if key == 'utils' or key.startswith('utils.'):
            sys.modules.pop(key)
    sys.modules.update(saved)


def save(store, **extra):
    record = {'status': 'approved', 'configs': [{'username': 'alice', 'server_id': 's1', 'unlimited': False}], **extra}
    Path(store.RESELLERS_FILE).write_text(json.dumps({'7': record}), encoding='utf-8')
    return record


def event(key, amount, days=0):
    return {'id': key, 'kind': 'settlement', 'amount': amount, 'paid_at': (NOW-timedelta(days=days)).isoformat()}


class Panel:
    server_id = 's1'

    def __init__(self, blocked=False):
        self.live = {'username': 'alice', 'blocked': blocked, 'expiration_days': 30,
                     'max_download_bytes': 12345, 'upload_bytes': 123, 'download_bytes': 456}
        self.updates = []
        self.fail = False
        self.status = 'found'
        self.verify = True
        self.before_update = None

    def resolve_unique_user(self, username, **kwargs):
        return self, dict(self.live), {'status': self.status, 'uniqueness_verified': self.verify}

    def get_user(self, username):
        return dict(self.live)

    def update_user(self, username, data):
        if self.before_update:
            self.before_update()
        self.updates.append((username, dict(data)))
        if self.fail:
            return None
        self.live.update(data)
        return dict(self.live)


@pytest.mark.parametrize('hours', [1, 6, 24, 168, 720])
def test_block_expiry_preserves_subscription_and_survives_reload(modules, hours):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    original = dict(panel.live)
    token = blocks.customer_block_token(7, 0)
    def persisted_before_mutation():
        persisted = json.loads(Path(store.RESELLERS_FILE).read_text())['7']['configs'][0]
        assert persisted['reseller_block']['until']
    panel.before_update = persisted_before_mutation
    first = blocks.request_block(7, token, hours, panel, now=NOW, request_id='click-1')
    assert first['state'] == 'blocked'
    assert panel.live['blocked'] is True
    assert blocks.process_due_blocks(panel, now=NOW+timedelta(hours=hours)-timedelta(seconds=1)) == []
    assert blocks.process_due_blocks(panel, now=NOW+timedelta(hours=hours)) == [True]
    assert panel.live == original
    assert blocks.block_view(7, token)['reseller_block']['state'] == 'complete'
    blocks.request_block(7, token, hours, panel, now=NOW+timedelta(hours=hours+1), request_id='click-1')
    assert panel.live['blocked'] is False  # Old callback cannot start a new block.
    assert blocks.process_due_blocks(panel, now=NOW+timedelta(days=31)) == []


@pytest.mark.parametrize('hours', [0, -1, 721, 1.5, '6', True, None])
def test_invalid_block_duration_cannot_mutate_panel(modules, hours):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    with pytest.raises(ValueError):
        blocks.request_block(7, token, hours, panel)
    assert panel.updates == []


def test_owner_checks_stale_release_and_retry(modules):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    with pytest.raises(ValueError):
        blocks.request_block(8, token, 1, panel)
    panel.fail = True
    pending = blocks.request_block(7, token, 1, panel, request_id='first')
    assert pending['state'] == 'pending'
    assert pending['attempts'] == 1
    panel.fail = False
    assert blocks.process_due_blocks(panel) == [True]
    panel.fail = True
    pending = blocks.release_block(7, token, panel, expected_block_id=pending['id'])
    assert pending['state'] == 'releasing'
    panel.fail = False
    assert blocks.process_due_blocks(panel) == [True]
    blocks.request_block(7, token, 1, panel, request_id='second')
    with pytest.raises(ValueError):
        blocks.release_block(7, token, panel, expected_block_id=pending['id'])
    assert panel.live['blocked'] is True


@pytest.mark.parametrize('status, verified', [('missing', True), ('duplicate', True), ('found', False)])
def test_unverified_identity_is_never_changed(modules, status, verified):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    panel.status, panel.verify = status, verified
    token = blocks.customer_block_token(7, 0)
    with pytest.raises(RuntimeError):
        blocks.request_block(7, token, 1, panel)
    assert panel.updates == []


@pytest.mark.parametrize('reason', ['admin_blocked', 'debt_policy_blocked'])
@pytest.mark.parametrize('temporary_first', [True, False])
def test_overlapping_reasons_release_independently(modules, reason, temporary_first):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    if temporary_first:
        blocks.request_block(7, token, 1, panel)
    blocks.set_owned_block_reason(7, 0, panel, panel.get_user('alice'), reason=reason, blocked=True)
    if not temporary_first:
        blocks.request_block(7, token, 1, panel)
    blocks.release_block(7, token, panel)
    assert panel.live['blocked'] is True
    blocks.set_owned_block_reason(7, 0, panel, panel.get_user('alice'), reason=reason, blocked=False)
    assert panel.live['blocked'] is False


def test_debt_restore_does_not_release_active_temporary_block(modules):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    blocks.request_block(7, token, 1, panel)
    blocks.set_owned_block_reason(7, 0, panel, panel.live, reason='debt_policy_blocked', blocked=True)
    blocks.set_owned_block_reason(7, 0, panel, panel.live, reason='debt_policy_blocked', blocked=False)
    assert panel.live['blocked'] is True
    blocks.release_block(7, token, panel)
    assert panel.live['blocked'] is False


def test_existing_external_block_and_banned_owner_are_protected(modules):
    store, blocks, _ = modules
    save(store, status='suspended')
    panel = Panel(blocked=True)
    token = blocks.customer_block_token(7, 0)
    blocks.request_block(7, token, 1, panel)
    blocks.release_block(7, token, panel)
    assert panel.live['blocked'] is True
    assert blocks.block_view(7, token)['reseller_block']['other_block'] is True
    records = store._read_resellers_file()
    records['7']['status'] = 'banned'
    store._write_resellers_file(records)
    with pytest.raises(ValueError):
        blocks.release_block(7, token, panel)


def test_rolling_boundaries_and_lifetime_are_independent(modules):
    store, _, _ = modules
    record = {'total_paid': 500, 'paid_activity_version': 1, 'paid_activity': [
        event('boundary', 10, 90), event('recent', 4), event('recent', 4),
        event('old', 50, 90.00001), event('future', 50, -1),
    ]}
    assert store.get_reseller_recent_paid(record, NOW) == 14
    assert store.get_reseller_total_paid(record) == 500
    assert store.get_reseller_level_summary(record, NOW)['level'] == 2
    assert store.get_reseller_level_summary(record, NOW+timedelta(seconds=1))['level'] == 1
    assert store.get_reseller_level_summary({'total_paid': 500}, NOW)['level'] == 1


def test_history_backfill_uses_dated_funding_and_paid_allocations_once(modules):
    store, _, _ = modules
    save(store, total_paid=999, configs=[{
        'username': 'alice', 'price': 10, 'funded_at_checkout': True, 'retail_order_id': 'o1',
        'timestamp': NOW.isoformat(), 'removed_from_vpn': True,
        'renewals': [{'funded_at_checkout': True, 'price': 2, 'reservation_id': 'r1', 'timestamp': NOW.isoformat()}],
    }, {'price': 100, 'timestamp': NOW.isoformat()}, {'price': 100, 'funded_at_checkout': True}],
    debt_allocations=[
        {'id': 'pay', 'kind': 'settlement', 'amount': 3, 'created_at': NOW.isoformat()},
        {'id': 'waiver', 'kind': 'admin_clear', 'amount': 99, 'created_at': NOW.isoformat()},
    ], processed_debt_payments=[{'payment_id': 'pay', 'credited_to_debt': 3, 'amount': 20, 'processed_at': NOW.isoformat()}])
    store.backfill_reseller_paid_activity()
    first = store._read_resellers_file()
    store.backfill_reseller_paid_activity()
    assert store._read_resellers_file() == first
    assert store.get_reseller_recent_paid(first['7'], NOW) == 15
    assert first['7']['total_paid'] == 999


def test_accounting_records_exact_paid_amount_without_topup_or_forgiveness(modules):
    store, _, _ = modules
    save(store, debt=5, total_paid=0)
    assert store.record_funded_reseller_config(7, 3, {'username': 'bob', 'retail_order_id': 'funded'})
    assert store.record_funded_reseller_config(7, 3, {'username': 'bob', 'retail_order_id': 'funded'})
    assert store.apply_reseller_payment(7, 7, payment_id='pay')[0]
    assert store.apply_reseller_payment(7, 7, payment_id='pay')[0]
    record = store.get_reseller_data(7)
    assert store.get_reseller_recent_paid(record, NOW) == 8
    assert record['total_paid'] == 8
    store.set_reseller_debt(7, 3)
    store.clear_reseller_debt(7)
    assert store.get_reseller_recent_paid(store.get_reseller_data(7), NOW) == 8


@pytest.mark.parametrize('language', ['en', 'fa', 'ru', 'tk'])
def test_credit_explanations_and_access_notices_are_localized(modules, language):
    store, _, experience = modules
    record = {'status': 'approved', 'debt': 2, 'debt_since': (NOW-timedelta(hours=47)).isoformat(),
              'paid_activity_version': 1, 'paid_activity': [event('pay', 20)],
              'credit_outcomes': [{'outcome': 'late'}]}
    text = experience.build_credit_summary(language, record, 7, now=NOW, balance={'available': 3, 'reserved': 1})
    assert 'half_credit' not in text and '{' not in text
    assert '$15.00' in text and '$7.50' in text and '$5.50' in text
    assert '1.0' in text and 'UTC' in text
    help_text = experience.build_credit_help(language)
    for hours in ('48', '72', '144', '168'):
        assert hours in help_text
    assert experience.access_limit_text(language, {}, plan=True) == experience.TEXT[language]['single_note']
    assert experience.access_limit_text(language, {'unlimited': True}) == experience.TEXT[language]['many_note']
    assert experience.access_limit_text(language, {'unlimited': False}, {'unlimited_ip': True}) == experience.TEXT[language]['single_note']
    assert experience.access_limit_text(language, live={'unlimited_user': False}) == experience.TEXT[language]['single_note']


def test_level_decrease_and_recovery_can_both_be_presented(modules):
    store, _, _ = modules
    save(store, total_paid=20, last_presented_reseller_level=3,
         paid_activity_version=1, paid_activity=[event('old', 20, 91)])
    claim = store.claim_reseller_level_presentation(7)
    assert claim['kind'] == 'level_down'
    assert store.complete_reseller_level_presentation(7, claim['id'])
    assert store.claim_reseller_level_presentation(7) is None
    store.record_funded_reseller_config(7, 20, {'username': 'bob', 'retail_order_id': 'fresh'})
    claim = store.claim_reseller_level_presentation(7)
    assert claim['kind'] == 'level_up'
    assert claim['from_level'] == 1 and claim['level'] == 3


@pytest.mark.parametrize('reason', ['temporary', 'admin_blocked', 'debt_policy_blocked', 'external_blocked'])
def test_renewal_cannot_reset_protected_access_blocks(modules, reason):
    store, blocks, _ = modules
    save(store)
    panel = Panel(blocked=reason == 'external_blocked')
    token = blocks.customer_block_token(7, 0)
    if reason in {'temporary', 'external_blocked'}:
        blocks.request_block(7, token, 1, panel, now=NOW)
    else:
        blocks.set_owned_block_reason(7, 0, panel, panel.live, reason=reason, blocked=True)
    renewal = importlib.import_module('utils.renewal')
    result = renewal.execute_reseller_renewal({'username': 'alice', 'server_id': 's1'}, multi_api=panel)
    assert result == {'success': False, 'reason': 'renewal_ineligible_protected_block'}
    blocks.release_block(7, token, panel)
    with blocks.renewal_block_guard('alice', 's1') as allowed:
        assert allowed is (reason == 'temporary')


def test_sqlite_restart_preserves_timer_activity_and_explicit_no_recommendation(modules, monkeypatch):
    store, blocks, _ = modules
    record = save(store, total_paid=20, paid_activity_version=1, paid_activity=[event('paid', 20)])
    monkeypatch.setenv('AJIB_SQLITE_ACTIVE', '1')
    store._write_resellers_file({'7': record})
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    blocks.request_block(7, token, 6, panel, now=NOW)
    prefs = importlib.import_module('utils.plan_recommendation')
    plans_path = str(Path(store.RESELLERS_FILE).with_name('plans.json'))
    prefs.set_recommendation(None, plans_file=plans_path)
    importlib.import_module('utils.database').close_connections()
    assert store.get_reseller_recent_paid(store.get_reseller_data(7), NOW) == 20
    assert prefs.get_recommendation({'5': {'recommended': True}}, '5', plans_file=plans_path) is None
    assert blocks.process_due_blocks(panel, now=NOW+timedelta(hours=6)) == [True]
    assert panel.live['blocked'] is False
    assert blocks.block_view(7, token)['reseller_block']['state'] == 'complete'


def test_hosted_recommendations_follow_rename_clear_and_eligibility(modules):
    hosted = importlib.import_module('utils.hosted_bots')
    Path(hosted.REGISTRY_FILE).write_text('{"7": {}}', encoding='utf-8')
    hosted.update_settings(7, {'enabled_plan_ids': ['5', '10'], 'plan_selection_configured': True,
                               'recommended_plan_id': '5'})
    hosted.update_catalog_plan_reference('5', '6')
    assert hosted.get_settings(7)['recommended_plan_id'] == '6'
    assert hosted.get_settings(7)['enabled_plan_ids'] == ['6', '10']
    hosted.update_settings(7, {'enabled_plan_ids': ['10']})
    assert hosted.get_settings(7)['recommended_plan_id'] is None
    hosted.update_settings(7, {'recommended_plan_id': '10'})
    hosted.update_catalog_plan_reference('10')
    assert hosted.get_settings(7)['recommended_plan_id'] is None
    assert hosted.get_settings(7)['enabled_plan_ids'] == []
    assert hosted.get_settings(7)['plan_selection_configured'] is True


def test_partial_settlement_keeps_deadlines_and_full_settlement_keeps_penalties(modules):
    store, _, experience = modules
    started = (NOW-timedelta(hours=60)).isoformat()
    save(store, status='suspended', suspended_reason=store.SUSPENDED_REASON_DEBT,
         debt=4, debt_since=started, credit_outcomes=[{'outcome': 'late'}, {'outcome': 'late'}])
    assert store.apply_reseller_payment(7, 1, payment_id='partial')[0]
    current = store.get_reseller_data(7)
    assert current['debt_since'] == started and current['debt'] == 3
    text = experience.build_credit_summary('en', current, 7, now=NOW, balance={})
    assert 'Selling suspended' in text and 'Prepaid only' in text
    assert '12.0 hours remaining' in text
    assert store.apply_reseller_payment(7, 3, payment_id='full')[0]
    current = store.get_reseller_data(7)
    assert current['debt_since'] is None
    assert store.get_reseller_credit_policy(current)['mode'] == 'prepaid_only'
    assert 'Selling available' in experience.build_credit_summary('en', current, 7, now=NOW, balance={})



def test_banned_reseller_cannot_act_but_expiry_removes_only_its_timer(modules):
    store, blocks, _ = modules
    save(store)
    panel = Panel()
    token = blocks.customer_block_token(7, 0)
    blocks.request_block(7, token, 1, panel, now=NOW)
    assert store.update_reseller_status(7, 'banned')
    with pytest.raises(ValueError):
        blocks.release_block(7, token, panel)
    assert blocks.process_due_blocks(panel, now=NOW+timedelta(hours=1)) == [True]
    assert panel.live['blocked'] is False
