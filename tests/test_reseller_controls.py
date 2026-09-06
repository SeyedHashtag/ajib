import ast
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


@pytest.mark.parametrize('level', range(1, 7))
@pytest.mark.parametrize('stage,base_hours,expected_kind', [
    ('suspend', 48, 'suspended'), ('hold', 72, 'hold_due'),
    ('warning', 144, 'deletion_warning'), ('removal', 168, 'remove_due'),
])
@pytest.mark.parametrize('seconds_before', [1, 0])
def test_level_deadline_boundaries(modules, level, stage, base_hours, expected_kind, seconds_before):
    store, _, _ = modules
    hours = base_hours + (level - 1) * 24
    started = NOW - timedelta(hours=hours) + timedelta(seconds=seconds_before)
    record = save(store, debt=4, debt_since=started.isoformat(), debt_cycle_id='boundary',
        paid_activity_version=1, paid_activity=[event('paid', (level - 1) * 10, days=30)])
    if stage != 'suspend':
        record.update(status='suspended', suspended_reason='debt', debt_cycle_late_recorded=True)
    if stage in {'warning', 'removal'}:
        record.update(debt_services_held_at=(NOW-timedelta(hours=1)).isoformat(),
            debt_cycle_default_recorded=True)
    store.save_resellers({'7': record})
    events = store.evaluate_reseller_debt_policies()
    saved = store.get_reseller_data(7)
    assert saved['debt_cycle_deadlines'][stage + '_hours'] == hours
    if seconds_before:
        assert expected_kind not in [item['kind'] for item in events]
        if stage == 'suspend':
            assert saved['status'] == 'approved'
        if stage in {'hold', 'removal'}:
            assert not saved['debt_service_' + ('remove' if stage == 'removal' else stage) + '_due']
    else:
        assert events[0]['kind'] == expected_kind
        assert events[0]['hours_until_suspend'] == max(0, (level + 1) * 24 - hours)
        assert events[0]['hours_until_hold'] == max(0, (level + 2) * 24 - hours)
        assert events[0]['hours_until_removal'] == max(0, (level + 6) * 24 - hours)
        assert saved['status'] == 'suspended'


@pytest.mark.parametrize('sqlite', [False, True])
@pytest.mark.parametrize('entrypoint', ['backfill_reseller_debt_deadlines', 'evaluate_reseller_debt_policies'])
def test_deadline_migration_persists_once_and_preserves_applied_restrictions(modules, monkeypatch, sqlite, entrypoint):
    store, _, _ = modules
    started = (NOW-timedelta(hours=170)).isoformat()
    record = save(store, debt=4, debt_since=started, status='suspended', suspended_reason='debt',
        debt_services_held_at=(NOW-timedelta(hours=90)).isoformat(),
        debt_cycle_late_recorded=True, debt_cycle_default_recorded=True,
        credit_outcomes=[{'outcome': 'late'}, {'outcome': 'default'}],
        debt_notification_state={'suspended:user': {'delivered_at': NOW.isoformat()}},
        debt_service_remove_due=True,
        paid_activity_version=1, paid_activity=[event('paid', 50, days=30)])
    monkeypatch.setenv('AJIB_SQLITE_ACTIVE', '1' if sqlite else '0')
    store.save_resellers({'7': record})
    getattr(store, entrypoint)()
    migrated = store._read_resellers_file()['7']
    assert migrated['debt_cycle_deadlines'] == {
        'level': 6, 'suspend_hours': 168, 'hold_hours': 192,
        'warning_hours': 264, 'removal_hours': 288,
    }
    assert migrated['debt_cycle_id']
    assert not migrated['debt_service_remove_due']
    for key in ('debt_since', 'status', 'suspended_reason', 'debt_services_held_at',
                'debt_cycle_late_recorded', 'debt_cycle_default_recorded', 'credit_outcomes',
                'debt_notification_state'):
        assert migrated[key] == record[key]
    store.backfill_reseller_debt_deadlines()
    assert store._read_resellers_file()['7'] == migrated
    importlib.import_module('utils.database').close_connections()
    # A later read with a lower level still uses the persisted cycle terms.
    monkeypatch.setattr(store, 'utc_now', lambda: NOW+timedelta(days=100))
    assert store.get_reseller_data(7)['debt_cycle_deadlines'] == migrated['debt_cycle_deadlines']


@pytest.mark.parametrize('action,age_hours', [('hold', 80), ('remove', 175)])
def test_legacy_queued_service_action_waits_for_extended_deadline(modules, action, age_hours):
    store, _, _ = modules
    save(store, debt=4, debt_since=(NOW-timedelta(hours=age_hours)).isoformat(),
        debt_cycle_id='legacy', debt_service_hold_due=True, debt_service_remove_due=True,
        paid_activity_version=1, paid_activity=[event('paid', 50, days=30)])
    # An object with no panel methods makes any attempted external action fail.
    assert store.process_reseller_debt_service_action(7, object(), action) == (
        False, {'reason': 'deadline_not_due'})
    saved = store._read_resellers_file()['7']
    assert not saved['debt_service_hold_due']
    assert not saved['debt_service_remove_due']
    assert all(item['service_action'] is None for item in store.evaluate_reseller_debt_policies())


def test_cycle_deadlines_survive_upgrade_downgrade_partial_payment_and_new_charges(modules):
    store, _, _ = modules
    save(store, debt=0, paid_activity_version=1, paid_activity=[event('paid', 10)])
    assert store.set_reseller_debt(7, 5)
    original = store.get_reseller_data(7)
    assert original['debt_cycle_deadlines']['suspend_hours'] == 72
    assert store.apply_reseller_payment(7, 2, 'partial')[0]
    assert store.add_reseller_debt(7, 1, {'username': 'bob', 'server_id': 's1', 'price': 1})
    for paid_amount in (50, 0):
        records = store.load_resellers()
        records['7']['paid_activity'] = [event('change-level', paid_amount)]
        store.save_resellers(records)
        store.evaluate_reseller_debt_policies()
        current = store.get_reseller_data(7)
        assert current['debt_cycle_deadlines'] == original['debt_cycle_deadlines']
        assert current['debt_since'] == original['debt_since']
        assert current['debt_cycle_id'] == original['debt_cycle_id']
    assert store.apply_reseller_payment(7, 4, 'finish')[0]
    assert store.get_reseller_data(7)['debt_cycle_deadlines'] is None
    assert store.set_reseller_debt(7, 2)
    new_cycle = store.get_reseller_data(7)
    assert new_cycle['debt_cycle_deadlines']['suspend_hours'] == 48
    assert new_cycle['debt_cycle_id'] != original['debt_cycle_id']


@pytest.mark.parametrize('age_hours,on_time', [(60, True), (72, False)])
def test_payment_classification_uses_saved_terms_before_payment_level_increase(modules, age_hours, on_time):
    store, _, _ = modules
    save(store, debt=10, debt_since=(NOW-timedelta(hours=age_hours)).isoformat(),
        paid_activity_version=1, paid_activity=[event('paid', 10, days=10)])
    assert store.apply_reseller_payment(7, 10, 'full')[0]
    saved = store.get_reseller_data(7)
    assert ('good' in [item['outcome'] for item in saved['credit_outcomes']]) is on_time
    assert store.get_reseller_level_summary(saved)['level'] == 3
    assert saved['debt_cycle_deadlines'] is None


def test_environment_baselines_and_saved_terms_survive_config_changes(modules, monkeypatch, tmp_path):
    store, _, _ = modules
    for setting, hours in [('SUSPEND_DEADLINE', 60), ('HOLD_DEADLINE', 90),
                           ('FINAL_WARNING', 180), ('REMOVAL_DEADLINE', 210)]:
        monkeypatch.setenv('RESELLER_DEBT_' + setting + '_HOURS', str(hours))
    store = importlib.reload(store)
    monkeypatch.setattr(store, 'utc_now', lambda: NOW)
    monkeypatch.setattr(store, '_now_str', lambda: NOW.isoformat())
    store.RESELLERS_FILE = str(tmp_path / 'resellers.json')
    save(store, debt=4, debt_since=NOW.isoformat(),
        paid_activity_version=1, paid_activity=[event('paid', 20)])
    saved = store.get_reseller_data(7)
    assert saved['debt_cycle_deadlines'] == {
        'level': 3, 'suspend_hours': 108, 'hold_hours': 138,
        'warning_hours': 228, 'removal_hours': 258,
    }
    monkeypatch.setattr(store, 'DEBT_SUSPEND_DEADLINE_HOURS', 120)
    assert store.get_reseller_data(7)['debt_cycle_deadlines']['suspend_hours'] == 108
    assert store.get_reseller_level_summary(saved)['settlement_hours'] == 168


@pytest.mark.parametrize('language', ['en', 'fa', 'ru', 'tk'])
def test_credit_displays_distinguish_locked_and_next_cycle_terms(modules, language):
    store, _, experience = modules
    save(store, debt=4, debt_since=(NOW-timedelta(hours=60)).isoformat(),
        paid_activity_version=1, paid_activity=[event('paid', 10, days=30)])
    records = store.load_resellers()
    records['7']['paid_activity'] = [event('level-up', 50)]
    store.save_resellers(records)
    record = store.get_reseller_data(7)
    summary = experience.build_credit_summary(language, record, 7,
        balance={'available': 0, 'reserved': 0}, now=NOW)
    help_text = experience.build_credit_help(language, record)
    for text in (summary, help_text):
        assert experience.settlement_window_text(language, 72) in text
        assert experience.settlement_window_text(language, 168) in text
        assert '{' not in text
    assert experience.experience_text(language, 'selling') in summary
    assert '12.0' in summary
    assert experience.experience_text(language, 'settlement_rules') in help_text


@pytest.mark.parametrize('source,function_name,callback', [
    ('utils/reseller_handlers.py', 'handle_reseller_credit_help', 'reseller:credit_help'),
    ('hosted_worker.py', 'owner_credit_customer_controls', 'hb:credithelp'),
])
def test_both_bot_help_callbacks_use_reseller_cycle_terms(modules, source, function_name, callback):
    store, _, experience = modules
    save(store, debt=4, debt_since=NOW.isoformat(),
        paid_activity_version=1, paid_activity=[event('paid', 50)])
    record = store.get_reseller_data(7)
    messages = []
    bot = types.SimpleNamespace(send_message=lambda chat, text: messages.append(text),
        answer_callback_query=lambda *_args: None)
    namespace = {
        'OWNER_ID': 7, 'bot': bot, 'get_reseller_data': lambda _id: record,
        '_get_active_reseller_data': lambda _id: record,
        'get_user_language': lambda _id: 'en', '_language': lambda _id: 'en',
        'safe_answer_callback_query': lambda *_args: None,
        'build_credit_help': experience.build_credit_help,
    }
    tree = ast.parse((UTILS.parent / source).read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), source, 'exec'), namespace)
    namespace[function_name](types.SimpleNamespace(id='callback', data=callback,
        from_user=types.SimpleNamespace(id=7), message=types.SimpleNamespace(chat=types.SimpleNamespace(id=7))))
    assert messages == [experience.build_credit_help('en', record)]
    assert '168' in messages[0] and '288' in messages[0]


@pytest.mark.parametrize('level', range(1, 7))
def test_final_payment_reminder_moves_with_settlement_window(modules, level):
    store, _, _ = modules
    save(store, debt=4, debt_since=(NOW-timedelta(hours=(level + 1) * 24 - 6)).isoformat(),
        paid_activity_version=1, paid_activity=[event('paid', (level - 1) * 10, days=30)])
    events = store.evaluate_reseller_debt_policies()
    assert events[0]['kind'] == 'deadline_final'
    assert events[0]['hours_until_suspend'] == 6
    assert store.get_reseller_data(7)['status'] == 'approved'


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
