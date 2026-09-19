import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils import database, web_store, customer_payments, account_operations
from utils.web_orders import Orders, save_payment
from utils.web_services import Services, ServiceError
from utils.customer_options import renewal_options
from utils.customer_progress import progress
from utils.state_store import _save_payment_record


@pytest.fixture
def card(monkeypatch):
    from utils import receipt_checker, exchange_rate
    monkeypatch.setattr(receipt_checker, 'get_card_number_for_receipt_type', lambda _: '0000 0000 0000 0000')
    monkeypatch.setattr(exchange_rate, 'get_exchange_rate', lambda: 100000)


def photo():
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(output, format='PNG')
    return output.getvalue()


def owned_panel(expired=False):
    started = datetime.now(timezone.utc) - timedelta(days=40 if expired else 2)
    user = {'username': 's123a', 'status': 'offline', 'blocked': expired,
            'account_creation_date': started.isoformat(), 'expiration_days': 30,
            'max_download_bytes': 40 * 1024**3, 'upload_bytes': 0, 'download_bytes': 0}
    client = SimpleNamespace(server_id='primary')
    panels = SimpleNamespace(resolve_unique_user=lambda *a, **kw: (
        client, user, {'status': 'found', 'uniqueness_verified': True, 'actual_server_id': 'primary'}))
    _save_payment_record(database.get_connection(), 'main', 'old-sale', {
        'user_id': 123, 'username': 's123a', 'server_id': 'primary', 'status': 'completed',
        'plan_gb': '40', 'days': 30, 'price': 1.2, 'created_at': started.isoformat(),
        'completed_at': started.isoformat(), 'updated_at': started.isoformat()})
    return Services(panels), user


def bot_checkout():
    from utils.purchase_incentives import reserve_order_checkout
    state = {'card_checkout_id': 'bot-original-payment', 'plan_gb': '40', 'language': 'fa',
             'converted_amount': 120000, 'converted_currency': 'Tomans', 'exchange_rate': 100000,
             'incentive_metadata': reserve_order_checkout(123, 'bot-original-payment', 1.2, 'card')}
    customer_payments.persist_bot_checkout(123, state, {'days': 30}, '0000 0000 0000 0000')
    return state['payment_id']


def test_methods_authentication_language_configuration_and_pause(client, login, card, monkeypatch):
    assert client.get('/api/v1/payment-methods').status_code == 401
    login(client)
    assert {m['id']: m['available'] for m in client.get('/api/v1/payment-methods').json()} == {'crypto': True, 'card': False}
    client.put('/api/v1/me/language', json={'language': 'fa'})
    assert all(m['available'] for m in client.get('/api/v1/payment-methods').json())
    monkeypatch.delenv('CRYPTO_API_KEY')
    assert not client.get('/api/v1/payment-methods').json()[0]['available']
    from utils import web_release
    with database.transaction() as db:
        web_release.save(db, {'access': 'public', 'pilot_users': [], 'accept_writes': False,
                             'process_existing': True, 'revision': 'synthetic', 'pilot_started_at': None})
    assert all(m['reason'] == 'writes_paused' for m in client.get('/api/v1/payment-methods').json())
    assert client.post('/api/v1/orders', json={'plan_id': '40', 'method': 'card'},
                       headers={'Idempotency-Key': 'paused-original-key'}).status_code == 503


@pytest.mark.parametrize('expired,mode', [(True, 'immediate'), (False, 'reserved')])
def test_renewal_choices_are_read_only_and_submission_revalidates(storage, card, expired, mode):
    services, user = owned_panel(expired)
    before = database.get_connection().total_changes
    options = renewal_options(services, '123', 'main', 'primary', 's123a')
    choices = [c for c in options['choices'] if c['available']]
    assert choices and {c['mode'] for c in choices} == {mode}
    assert database.get_connection().total_changes == before
    user['max_download_bytes'] = 1
    with pytest.raises(ServiceError, match='options changed'):
        Orders(services).create('123', 'main', 'changed-renewal-key', '40', 'card', 'fa',
                                's123a', 'primary', mode == 'reserved')
    assert not database.get_connection().execute('SELECT 1 FROM web_operations').fetchone()


def test_renewal_options_ownership_claims_and_existing_reservation(storage):
    services, _ = owned_panel()
    with pytest.raises(ServiceError) as denied:
        renewal_options(services, '999', 'main', 'primary', 's123a')
    assert denied.value.status == 404
    account_operations.execute('other', 'primary', 's123a', 'update', {}, lambda: {'success': True})
    assert renewal_options(services, '123', 'main', 'primary', 's123a')['reason'] == 'account_busy'
    account_operations.complete('other')
    save_payment(database.get_connection(), 'main', 'queued-renewal', {
        'user_id': 123, 'status': 'completed', 'renewal_mode': 'reserved',
        'renewal_status': 'reserved', 'renewal_username': 's123a', 'server_id': 'primary'})
    options = renewal_options(services, '123', 'main', 'primary', 's123a')
    assert options['reservation']['payment_id'] == 'queued-renewal'
    assert not any(c['available'] for c in options['choices'])


def test_bot_purchase_continues_in_browser_without_new_owner_or_order(client, login, card):
    ident = bot_checkout()
    login(client)
    result = client.get('/api/v1/payments/' + ident).json()
    assert result['progress']['actions'] == ['upload_receipt', 'cancel']
    response = client.post(f'/api/v1/payments/{ident}/receipt', files={'file': ('receipt.png', photo(), 'image/png')})
    assert response.status_code == 200, response.text
    duplicate = client.post(f'/api/v1/payments/{ident}/receipt', files={'file': ('receipt.png', photo(), 'image/png')})
    assert duplicate.json() == response.json()
    record = Services().payment('123', 'main', ident)
    assert record['fulfillment_owner'] == 'bot' and record['status'] == 'pending_approval'
    assert customer_payments.receipt_bytes(ident, record).startswith(b'\xff\xd8')
    assert not database.get_connection().execute('SELECT 1 FROM web_operations').fetchone()
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_receipts').fetchone()[0] == 1
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 1
    assert not Orders(Services()).process_one()
    login(client, 999)
    assert client.get('/api/v1/receipts/' + response.json()['id']).status_code == 404


def test_web_purchase_continues_through_shared_bot_receipt_and_checker_review(storage, card, monkeypatch):
    monkeypatch.setenv('RECEIPT_CHECKER_USER_ID', '7')
    from utils import receipt_checker
    monkeypatch.setattr(receipt_checker, 'should_route_to_receipt_checker', lambda _: True)
    monkeypatch.setattr(receipt_checker, 'get_receipt_checker_types', lambda: {'regular'})
    orders = Orders(Services())
    payment = orders.create('123', 'main', 'cross-interface-key', '40', 'card', 'fa')
    customer_payments.submit_receipt('123', 'main', payment['id'], photo())
    with pytest.raises(ServiceError):
        orders.review('8', 'main', payment['id'], True, 'not the checker')
    assert orders.review('7', 'main', payment['id'], True, 'reviewed in bot')['status'] == 'approved'
    record = Services().payment('123', 'main', payment['id'])
    assert record['fulfillment_owner'] == 'web' and record['reviewed_by_role'] == 'checker'
    assert 'checker_share_amount_toman' in record
    with pytest.raises(ServiceError):
        orders.review('7', 'main', payment['id'], True, 'duplicate event')
    assert database.get_connection().execute("SELECT COUNT(*) FROM web_outbox WHERE id LIKE 'review:%'").fetchone()[0] == 1


def test_receipt_accounting_failure_rolls_back_everything(storage, card, monkeypatch):
    ident = bot_checkout()
    monkeypatch.setattr(web_store, 'enqueue', lambda *a: (_ for _ in ()).throw(RuntimeError('injected')))
    with pytest.raises(RuntimeError):
        customer_payments.submit_receipt('123', 'main', ident, photo())
    assert Services().payment('123', 'main', ident)['status'] == 'waiting_receipt'
    assert not database.get_connection().execute('SELECT 1 FROM web_receipts').fetchone()


def test_competing_receipt_and_cancellation_commit_one_transition(storage, card):
    ident = bot_checkout()
    def run(action):
        try:
            if action == 'receipt':
                customer_payments.submit_receipt('123', 'main', ident, photo())
            else:
                customer_payments.cancel('123', 'main', ident)
            return True
        except ServiceError:
            return False
        finally:
            for db in database._connection_map().values(): db.close()
            database._connection_map().clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(run, ['receipt', 'cancel'])) == 1


@pytest.mark.parametrize('status,renewal,code,poll', [
    ('completed', 'reserved', 'renewal_reserved', True),
    ('completed', 'processing', 'renewal_activating', True),
    ('completed', 'attention', 'needs_attention', True),
    ('completed', 'applied', 'completed', False),
    ('uncertain', None, 'needs_attention', True),
    ('rejected', None, 'rejected', False)])
def test_payment_settlement_does_not_hide_reserved_progress(status, renewal, code, poll):
    result = progress({'status': status, 'renewal_mode': 'reserved', 'renewal_status': renewal,
                       'web_attention_reason': '/private/path/exception'})
    assert result['code'] == code and result['poll'] == poll
    assert 'private' not in json.dumps(result)


def test_bot_can_deliver_outbox_without_http_or_website_worker(storage):
    web_store.enqueue(database.get_connection(), 'main-event', 'main', '123', 'Your connection is ready.')
    web_store.enqueue(database.get_connection(), 'hosted-event', 'hosted:7', '123', 'Hosted connection is ready.')
    calls = []
    bot = SimpleNamespace(send_message=lambda *args: calls.append(args))
    assert customer_payments.deliver_bot_notifications(bot)
    assert not customer_payments.deliver_bot_notifications(bot)
    assert len(calls) == 1
    assert database.get_connection().execute("SELECT status FROM web_outbox WHERE id='hosted-event'").fetchone()[0] == 'pending'


def test_renewal_endpoint_is_typed_scoped_and_preserves_protected_blocks(client, login):
    from utils import state_store
    services, _ = owned_panel(expired=True)
    client.app.state.services._panels = services.panels
    login(client)
    path = '/api/v1/accounts/primary/s123a/renewal-options'
    response = client.get(path)
    assert response.status_code == 200
    assert any(c['mode'] == 'immediate' and c['available'] for c in response.json()['choices'])
    state_store._save_reseller_record(database.get_connection(), '7', {'status': 'approved', 'configs': [
        {'username': 's123a', 'server_id': 'primary', 'admin_blocked': True}]})
    assert client.get(path).json()['reason'] == 'protected_account'
    login(client, 999)
    assert client.get(path).status_code == 404


def test_receipt_rejection_releases_credit_once_and_revoked_checker_cannot_read(client, login, card, monkeypatch):
    from utils import receipt_checker
    from utils.account_credit import credit_account, get_account_credit
    monkeypatch.setenv('RECEIPT_CHECKER_USER_ID', '7')
    monkeypatch.setattr(receipt_checker, 'should_route_to_receipt_checker', lambda _: True)
    monkeypatch.setattr(receipt_checker, 'get_receipt_checker_types', lambda: {'regular'})
    credit_account(123, 0.5, 'partial-credit')
    payment = Orders(Services()).create('123', 'main', 'rejected-credit-key', '40', 'card', 'fa')
    receipt = customer_payments.submit_receipt('123', 'main', payment['id'], photo())
    login(client, 7)
    assert client.get('/api/v1/receipts/' + receipt['id']).status_code == 200
    assert Orders(Services()).review('7', 'main', payment['id'], False, 'not paid')['status'] == 'rejected'
    assert get_account_credit(123)['reserved'] == 0
    assert get_account_credit(123)['available'] == 0.5
    monkeypatch.setenv('RECEIPT_CHECKER_USER_ID', '8')
    assert client.get('/api/v1/receipts/' + receipt['id']).status_code == 404
    assert client.post('/api/v1/admin/payments/' + payment['id'] + '/review',
                       json={'approve': True, 'reason': 'old checker'}).status_code == 403


def test_uncertain_or_unproven_legacy_receipt_cannot_release_or_replace_payment(storage, card):
    ident = bot_checkout()
    account_operations.execute('main-payment:' + ident, 'primary', 'some-account', 'create', {}, lambda: {'success': True})
    from utils.web_services import payment_public
    assert payment_public(ident, Services().payment('123', 'main', ident))['progress']['actions'] == ['contact_support']
    for action in (lambda: customer_payments.submit_receipt('123', 'main', ident, photo()),
                   lambda: customer_payments.cancel('123', 'main', ident)):
        with pytest.raises(ServiceError, match='support'):
            action()
    assert Services().payment('123', 'main', ident)['status'] == 'waiting_receipt'
    save_payment(database.get_connection(), 'main', 'legacy-no-proof', {'user_id': 123,
        'status': 'waiting_receipt', 'price': 1, 'payment_method': 'Card to Card'})
    assert progress(Services().payment('123', 'main', 'legacy-no-proof'))['actions'] == ['contact_support']
    with pytest.raises(ServiceError):
        customer_payments.cancel('123', 'main', 'legacy-no-proof')


def test_telegram_adapter_uses_private_receipt_without_http(storage, card):
    import ast
    from pathlib import Path
    ident = bot_checkout()
    customer_payments.submit_receipt('123', 'main', ident, photo())
    source = Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot/utils/purchase_plan.py'
    # Compile the actual adapter without importing/registering the full polling bot.
    node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == '_send_receipt_confirmation')
    sent = []
    namespace = {'io': io, '_build_receipt_approval_markup': lambda _: 'review-buttons',
                 '_format_pending_receipt_caption': lambda *a: 'Review receipt',
                 'bot': SimpleNamespace(send_photo=lambda *args, **kwargs: sent.append((args, kwargs)))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    namespace['_send_receipt_confirmation']('1', ident, Services().payment('123', 'main', ident))
    assert sent[0][0][1].getvalue().startswith(b'\xff\xd8')
    assert sent[0][1]['reply_markup'] == 'review-buttons'


@pytest.mark.parametrize('language', ['en', 'fa', 'ru', 'tk'])
def test_notifications_keep_recipient_language_and_neutral_content(storage, card, language):
    from utils.customer_messages import message
    from utils.public_branding import contains_private
    Services().set_language('1', 'main', language)
    ident = bot_checkout()
    customer_payments.submit_receipt('123', 'main', ident, photo())
    text = database.get_connection().execute("SELECT text FROM web_outbox WHERE recipient='1'").fetchone()[0]
    assert text.startswith(message(language, 'review_receipt'))
    assert not contains_private(text)
