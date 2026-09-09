import importlib
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    saved = {k: v for k, v in sys.modules.items() if k == 'utils' or k.startswith('utils.')}
    for key in saved:
        sys.modules.pop(key)
    package = types.ModuleType('utils')
    package.__path__ = [str(Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot/utils')]
    sys.modules['utils'] = package
    monkeypatch.setenv('AJIB_SQLITE_ACTIVE', '1')
    monkeypatch.setenv('AJIB_DB_PATH', str(tmp_path / 'ajib.db'))
    monkeypatch.setenv('AJIB_BOT_DIR', str(tmp_path))
    store = importlib.import_module('utils.reseller')
    store.RESELLERS_FILE = str(tmp_path / 'resellers.json')
    monkeypatch.setattr(store, '_update_recruitment_milestone', lambda *a: None)
    funding = importlib.import_module('utils.reseller_funding')
    wallet = importlib.import_module('utils.reseller_wholesale_credit')
    store.save_resellers({'7': {'status': 'approved', 'debt': 0, 'total_paid': 0, 'configs': []}})
    yield store, funding, wallet
    funding.database.close_connections()
    for key in list(sys.modules):
        if key == 'utils' or key.startswith('utils.'):
            sys.modules.pop(key)
    sys.modules.update(saved)


def purchase(app, amount, reference):
    store, funding, _ = app
    funding.reserve_funding(7, reference, amount)
    funding.finalize_funding(7, reference, {'username': reference, 'server_id': 's1', 'price': amount})
    return store.get_reseller_data(7)


@pytest.mark.parametrize('balance,prepaid,debt', [(0, 0, 5), (2, 2, 3), (5, 5, 0), (8, 5, 0)])
def test_prepaid_first_and_accounting(app, balance, prepaid, debt):
    store, funding, wallet = app
    if balance:
        wallet.credit_wholesale_balance(7, balance, 'topup')
    before = funding.quote_funding(7, 5)
    assert before['prepaid_cents'] == prepaid * 100
    assert before['debt_cents'] == debt * 100
    record = purchase(app, 5, 'order')
    assert record['debt'] == debt
    assert record['total_paid'] == prepaid
    if not prepaid:
        assert record['last_payment_at'] is None
    assert record['configs'][0]['price'] == 5
    assert store.get_reseller_recent_paid(record) == prepaid
    assert wallet.get_wholesale_balance(7)['available'] == balance - prepaid
    # A duplicate finalization cannot append a config, charge or payment.
    funding.finalize_funding(7, 'order', {'username': 'order', 'server_id': 's1'})
    assert store.get_reseller_data(7) == record


@pytest.mark.parametrize('level', range(1, 7))
@pytest.mark.parametrize('spent,penalty', [(4.99, 2), (5, 2), (9.99, 2), (10, 0), (12, 0)])
def test_fixed_five_dollar_cycles_all_levels(app, level, spent, penalty):
    store, funding, wallet = app
    record = store.get_reseller_data(7)
    record.update(paid_activity_version=1, paid_activity=[{
        'id': 'level', 'kind': 'settlement', 'amount': (level - 1) * 10,
        'paid_at': datetime.now(timezone.utc).isoformat()}])
    store.save_resellers({'7': record})
    store.record_reseller_credit_outcome(7, 'default', 'test', 'default')
    wallet.credit_wholesale_balance(7, 20, 'topup')
    record = purchase(app, spent, 'order')
    policy = store.get_reseller_credit_policy(record)
    assert policy['adverse_weight'] == penalty
    assert policy['recovery']['spent_cents'] == min(1000, round(spent * 100))
    assert policy['effective_limit'] == (policy['base_limit'] if not penalty else 0)


def test_small_orders_mixed_spending_settlement_and_new_penalty(app):
    store, funding, wallet = app
    store.record_reseller_credit_outcome(7, 'late', 'test', 'late')
    wallet.credit_wholesale_balance(7, 2, 'topup')
    record = purchase(app, 3, 'mixed')
    assert record['credit_recovery']['spent_cents'] == 200
    assert record['debt'] == 1
    store.record_reseller_credit_outcome(7, 'good', 'on_time_settlement', 'settled')
    assert store.get_reseller_data(7)['credit_recovery']['spent_cents'] == 200
    wallet.credit_wholesale_balance(7, 20, 'more')
    for i in range(8):
        record = purchase(app, 1, f'order{i}')
    assert record['credit_recovery']['penalty'] == 0
    store.record_reseller_credit_outcome(7, 'default', 'test', 'new-default')
    record = store.get_reseller_data(7)
    assert record['credit_recovery']['spent_cents'] == 0
    assert record['credit_recovery']['penalty'] == 2
    assert record['debt'] == 1


def test_insufficient_and_changed_funding_leave_no_reservations(app):
    _, funding, wallet = app
    wallet.credit_wholesale_balance(7, 2, 'topup')
    with pytest.raises(funding.FundingUnavailable):
        funding.reserve_funding(7, 'too-large', 7.01)
    assert wallet.get_wholesale_balance(7)['available'] == 2
    expected = funding.quote_funding(7, 5)
    wallet.credit_wholesale_balance(7, 1, 'more')
    with pytest.raises(funding.FundingChanged):
        funding.reserve_funding(7, 'changed', 5, expected=expected)
    assert funding.borrowing_reserved(7) == 0
    assert wallet.get_wholesale_balance(7)['reserved'] == 0


def test_rollback_release_restart_and_concurrent_capacity(app, monkeypatch):
    store, funding, wallet = app
    wallet.credit_wholesale_balance(7, 2, 'topup')
    funding.reserve_funding(7, 'order', 5)
    funding.database.close_connections()
    with monkeypatch.context() as patch:
        patch.setattr(store, 'record_funded_reseller_config', lambda *a, **kw: False)
        with pytest.raises(funding.FundingUnavailable):
            funding.finalize_funding(7, 'order', {'username': 'test'})
    assert wallet.get_wholesale_balance(7)['reserved'] == 2
    assert store.get_reseller_data(7)['debt'] == 0
    assert funding.release_funding(7, 'order')
    assert not funding.release_funding(7, 'order')

    def reserve(reference):
        try:
            funding.reserve_funding(7, reference, 5)
            return True
        except funding.FundingUnavailable:
            return False
        finally:
            funding.database.close_connections()
    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(reserve, ['main', 'hosted'])) == [False, True]
    assert funding.borrowing_reserved(7) == 3
    assert wallet.get_wholesale_balance(7)['available'] == 0


@pytest.mark.parametrize('kind', ['renewal', 'reserved_renewal'])
def test_split_renewal_accounted_once(app, kind):
    store, funding, wallet = app
    record = store.get_reseller_data(7)
    record['configs'] = [{'username': 'alice', 'server_id': 's1', 'price': 0}]
    store.save_resellers({'7': record})
    wallet.credit_wholesale_balance(7, 2, 'topup')
    funding.reserve_funding(7, 'renewal', 5)
    for _ in range(2):
        funding.finalize_funding(7, 'renewal', {'price': 5}, kind=kind, username='alice', server_id='s1')
    record = store.get_reseller_data(7)
    assert record['debt'] == 3 and record['total_paid'] == 2
    assert len(record['configs'][0]['renewals']) == 1


def test_external_funded_order_and_override_preserve_debt_and_access(app):
    store, _, wallet = app
    store.record_reseller_credit_outcome(7, 'default', 'test', 'default')
    wallet.credit_wholesale_balance(7, 5, 'topup')
    assert store.record_funded_reseller_config(7, 5, {'username': 'external', 'retail_order_id': 'crypto'})
    assert wallet.get_wholesale_balance(7)['available'] == 5
    record = store.get_reseller_data(7)
    assert record['credit_recovery']['spent_cents'] == 500
    record.update(status='banned', debt=3, debt_since='2026-01-01 00:00:00')
    store.save_resellers({'7': record})
    assert not store.restore_reseller_credit(7, 99, ' ', 'override')
    for _ in range(2):
        assert store.restore_reseller_credit(7, 99, 'Payment reviewed', 'override')
    after = store.get_reseller_data(7)
    assert after['debt'] == 3 and after['status'] == 'banned'
    assert after['debt_since'] == '2026-01-01 00:00:00'
    assert after['credit_recovery']['penalty'] == 0
    assert len([x for x in after['credit_policy_history'] if x['kind'] == 'admin_restore']) == 1


def test_migration_requires_amount_evidence(app):
    store, _, _ = app
    started = datetime.now(timezone.utc) - timedelta(days=2)
    record = {'credit_outcomes': [{'outcome': 'default', 'recorded_at': started.isoformat()},
                                {'outcome': 'good'}, {'outcome': 'good'}],
              'configs': [{'username': 'a', 'funded_at_checkout': True, 'price': 10,
                           'timestamp': (started + timedelta(days=1)).isoformat()}]}
    assert store.get_reseller_credit_policy(record)['mode'] == 'credit'
    record['configs'] = []
    assert store.get_reseller_credit_policy(record)['mode'] == 'prepaid_only'


@pytest.mark.parametrize('language,stats', [('en', 'Business Statistics'), ('fa', 'آمار کسب‌وکار'), ('ru', 'Бизнес-статистика'), ('tk', 'Iş statistikasy')])
def test_localized_profile_and_recovery(app, language, stats):
    store, _, wallet = app
    store.record_reseller_credit_outcome(7, 'default', 'test', 'default')
    wallet.credit_wholesale_balance(7, 7, 'topup')
    record = purchase(app, 7, 'order')
    level_ui = importlib.import_module('utils.reseller_level_ui')
    experience = importlib.import_module('utils.reseller_experience')
    text = level_ui.build_reseller_level_profile(language, record, 7, '2026-01-01', 1, 7, 0)
    terms = experience.build_settlement_terms(language, record)
    assert text.index(terms) < text.index(stats)
    assert '$2.00' in text and '$3.00' in text
    assert '{' not in text and 'prepaid_only' not in text
    assert '$10' in experience.build_credit_help(language, record)


def load_function(path, name, namespace):
    """Run the real handler with only Telegram/panel boundaries replaced."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


def test_main_creation_runs_split_accounting_and_completion(app):
    from unittest.mock import Mock
    store, funding, wallet = app
    wallet.credit_wholesale_balance(7, 2, 'topup')
    client = Mock(server_id='s1')
    client.get_user_uri.return_value = None
    namespace = {name: getattr(funding, name) for name in (
        'reserve_funding', 'finalize_funding', 'release_funding', 'remember_fulfillment', 'FundingUnavailable')}
    namespace.update({
        'get_reseller_data': store.get_reseller_data,
        'reseller_config_is_recorded': store.reseller_config_is_recorded,
        '_configured_primary_api_client': lambda: client,
        '_create_reseller_user_with_note': lambda *a, **kw: ('alice', True, client),
        'bot': Mock(), 'safe_send_message': Mock(), 'safe_reply_to': Mock(),
        'funding_text': lambda *a: 'split', 'build_credit_summary': lambda *a, **kw: 'journey',
        'get_message_text': lambda *a: 'created', 'escape_markdown_code': str,
        'escape_markdown_text': str, 'access_limit_text': lambda *a: 'single user',
    })
    run = load_function('core/scripts/telegrambot/utils/reseller_handlers.py', '_run_reseller_customer_creation', namespace)
    run(types.SimpleNamespace(chat=types.SimpleNamespace(id=7)), 7, 'en', {
        'gb': 5, 'days': 30, 'price': 5, 'funding_mode': funding.quote_funding(7, 5),
        'wholesale_reservation_id': 'main-order'}, 'alice')
    record = store.get_reseller_data(7)
    assert record['debt'] == 3 and record['total_paid'] == 2
    assert namespace['safe_send_message'].call_count == 2
    assert funding.get_funding(7, 'main-order')['status'] == 'completed'


@pytest.mark.parametrize('renewed', [False, True])
def test_hosted_fulfillment_uses_saved_split_and_does_not_repeat(app, monkeypatch, renewed):
    import os
    from unittest.mock import Mock
    store, funding, wallet = app
    if renewed:
        record = store.get_reseller_data(7)
        record['configs'] = [{'username': 'alice', 'server_id': 's1', 'price': 0}]
        store.save_resellers({'7': record})
    wallet.credit_wholesale_balance(7, 2, 'topup')
    split = funding.reserve_funding(7, 'hosted-order', 5)
    client = Mock(server_id='s1')
    renewal = types.ModuleType('utils.renewal')
    renewal.execute_hosted_renewal = Mock(return_value={'success': True, 'api_client': client})
    monkeypatch.setitem(sys.modules, 'utils.renewal', renewal)
    namespace = {name: getattr(funding, name) for name in ('finalize_funding', 'get_funding')}
    namespace.update({
        'OWNER_ID': 7, 'os': os, 'bot': Mock(), 'get_reseller_data': store.get_reseller_data,
        '_settlement_financials': lambda r: {'referral_reward': 0, 'margin': 0},
        '_create_user': Mock(return_value=('alice', True, client)),
        '_save_payment': Mock(), '_credit_sale_and_referral': Mock(),
        '_owner_payment_snapshot': Mock(return_value={}), '_notify_owner_payment': Mock(),
        '_record_completed_growth': Mock(), '_deliver_config_safely': Mock(),
        '_resolve_hosted_user': Mock(return_value=(client, {'username': 'alice'}, {'status': 'found', 'uniqueness_verified': True})),
        'MultiServerAPI': Mock(),
    })
    run = load_function('core/scripts/telegrambot/hosted_worker.py', '_provision_payment', namespace)
    payment = {'user_id': 42, 'funding': split, 'wholesale_price': 5, 'retail_price': 7,
               'plan_gb': 5, 'days': 30, 'server_id': 's1'}
    if renewed:
        payment['renew_username'] = 'alice'
    for _ in range(2):
        assert run('hosted-order', payment, False)[0]
    record = store.get_reseller_data(7)
    assert record['debt'] == 3 and record['total_paid'] == 2
    assert wallet.get_wholesale_balance(7)['available'] == 0
    assert (renewal.execute_hosted_renewal if renewed else namespace['_create_user']).call_count == 1


def test_restart_finishes_saved_panel_work_without_new_provisioning(app):
    store, funding, wallet = app
    wallet.credit_wholesale_balance(7, 2, 'topup')
    funding.reserve_funding(7, 'interrupted', 5, metadata={'origin': 'main'})
    funding.remember_fulfillment(7, 'interrupted', {'username': 'alice', 'server_id': 's1', 'price': 5})
    funding.database.close_connections()
    future = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert len(funding.reconcile_funding(now=future)) == 1
    assert funding.reconcile_funding(now=future) == []
    assert store.get_reseller_data(7)['debt'] == 3
    assert len(store.get_reseller_data(7)['configs']) == 1


def test_admin_restore_callback_rechecks_authorization_and_ignores_replay(app):
    from unittest.mock import Mock
    import time
    store, _, _ = app
    store.record_reseller_credit_outcome(7, 'default', 'test', 'default')
    namespace = {
        'is_admin': lambda user: user == 99, 'get_user_language': lambda user: 'en',
        'get_reseller_data': store.get_reseller_data,
        'get_reseller_credit_policy': store.get_reseller_credit_policy,
        'get_message_text': lambda *a: 'invalid', 'time': time, 'bot': Mock(),
        '_render_admin_reseller_detail': Mock(),
        '_admin_view_context': lambda user: {'return_status': 'approved', 'return_page': 0},
        'ADMIN_CREDIT_RESTORE_STATE': {99: {'state': 'confirm', 'token': 'nonce', 'reseller_id': '7',
            'reason': 'Reviewed', 'created_at': time.time(), 'action_id': 'override', 'limit': 5}},
    }
    run = load_function('core/scripts/telegrambot/utils/reseller_handlers.py', 'handle_admin_reseller_ui', namespace)
    call = types.SimpleNamespace(from_user=types.SimpleNamespace(id=42), id='callback',
                                 data='admin_reseller_ui:restoreconfirm:nonce:silent')
    run(call)
    assert store.get_reseller_credit_policy(store.get_reseller_data(7))['mode'] == 'prepaid_only'
    call.from_user.id = 99
    run(call)
    run(call)
    assert store.get_reseller_credit_policy(store.get_reseller_data(7))['mode'] == 'credit'
    assert namespace['_render_admin_reseller_detail'].call_count == 1


@pytest.mark.parametrize('kind', ['immediate', 'reserved'])
def test_main_renewal_handler_commits_split(app, monkeypatch, kind):
    from unittest.mock import Mock
    store, funding, wallet = app
    record = store.get_reseller_data(7)
    record['configs'] = [{'username': 'alice', 'server_id': 's1', 'price': 0}]
    store.save_resellers({'7': record})
    wallet.credit_wholesale_balance(7, 2, 'topup')
    split = funding.quote_funding(7, 5)
    offer = {'eligible': True, 'price': 5, 'plan_gb': '5', 'days': 30, 'username': 'alice',
             'server_id': 's1', 'config': record['configs'][0], 'renewal_mode': kind}
    renewal = types.ModuleType('utils.renewal')
    renewal.execute_reseller_renewal = Mock(return_value={'success': True})
    renewal.reseller_renewal_record = lambda *a: {'price': 5}
    renewal.reserved_renewal_record = lambda *a: {'price': 5}
    renewal.format_renewal_success = lambda *a, **kw: 'renewed'
    renewal.mark_cleanup_state_renewed = Mock()
    monkeypatch.setitem(sys.modules, 'utils.renewal', renewal)
    namespace = {name: getattr(funding, name) for name in (
        'reserve_funding', 'finalize_funding', 'release_funding', 'remember_fulfillment', 'FundingUnavailable')}
    namespace.update({
        '_get_active_reseller_data': store.get_reseller_data, '_is_reseller_suspended': lambda r: False,
        '_resolve_reseller_renewal_offer_for_call': lambda *a: (offer, store.get_reseller_data(7)),
        '_reseller_order_funding': lambda *a: (split, 5, 5, {}),
        'RESELLER_FUNDING_PREVIEWS': {(7, 'renewal', 'token'): split},
        'get_reseller_data': store.get_reseller_data, 'bot': Mock(),
        'get_message_text': lambda *a: 'success', 'escape_markdown_code': str,
        'format_usd_amount': lambda n: f'{n:.2f}', 'funding_text': lambda *a: 'split',
        'build_credit_summary': lambda *a, **kw: 'journey',
        'safe_edit_message_text': Mock(), 'safe_send_message': Mock(),
    })
    run = load_function('core/scripts/telegrambot/utils/reseller_handlers.py', '_process_reseller_renewal_confirm_job', namespace)
    call = types.SimpleNamespace(message=types.SimpleNamespace(chat=types.SimpleNamespace(id=7), message_id=1))
    run(call, 7, 'en', 'token')
    record = store.get_reseller_data(7)
    assert record['debt'] == 3 and record['total_paid'] == 2
    assert len(record['configs'][0]['renewals']) == 1
    assert renewal.execute_reseller_renewal.call_count == (1 if kind == 'immediate' else 0)


def test_failure_after_order_write_rolls_back_every_financial_effect(app, monkeypatch):
    store, funding, wallet = app
    store.record_reseller_credit_outcome(7, 'default', 'test', 'default')
    wallet.credit_wholesale_balance(7, 10, 'topup')
    funding.reserve_funding(7, 'order', 10)
    original = funding._save

    def fail_completion(connection, reseller_id, saved):
        if saved['status'] == 'completed':
            raise RuntimeError('Injected write failure')
        return original(connection, reseller_id, saved)
    with monkeypatch.context() as patch:
        patch.setattr(funding, '_save', fail_completion)
        with pytest.raises(RuntimeError):
            funding.finalize_funding(7, 'order', {'username': 'alice', 'price': 10})
    record = store.get_reseller_data(7)
    assert record['configs'] == [] and record['total_paid'] == 0
    assert record['credit_recovery']['penalty'] == 2
    assert record['credit_recovery']['spent_cents'] == 0
    assert wallet.get_wholesale_balance(7)['reserved'] == 10
    funding.finalize_funding(7, 'order', {'username': 'alice', 'price': 10})
    assert store.get_reseller_data(7)['credit_recovery']['penalty'] == 0


def test_good_settlements_never_age_out_an_active_penalty(app):
    store, _, _ = app
    store.record_reseller_credit_outcome(7, 'late', 'test', 'late')
    for i in range(10):
        store.record_reseller_credit_outcome(7, 'good', 'on_time_settlement', f'good-{i}')
    policy = store.get_reseller_credit_policy(store.get_reseller_data(7))
    assert policy['mode'] == 'half_credit'
    assert policy['recovery']['spent_cents'] == 0


@pytest.mark.parametrize('saved', [None, 'legacy', {'version': 1, 'penalty': [], 'spent_cents': 'bad'}])
def test_legacy_malformed_recovery_fields_do_not_break_accounts(app, saved):
    store, _, _ = app
    record = {'status': 'approved', 'configs': [], 'credit_recovery': saved,
              'credit_outcomes': None, 'credit_policy_history': None}
    normalized = store._ensure_reseller_defaults(record)
    assert store.get_reseller_credit_policy(normalized)['mode'] == 'credit'
    assert normalized['credit_policy_history'] == []
