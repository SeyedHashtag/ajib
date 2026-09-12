import json
import time

import pytest


@pytest.fixture
def invoice(storage):
    from utils import database
    from utils.web_orders import save_payment
    from utils.web_services import Services
    record = {'user_id': 123, 'status': 'pending', 'type': 'purchase', 'price': '10.00',
              'currency': 'USD', 'payment_method': 'Crypto', 'gateway_order_id': 'web_test_order',
              'gateway_payment_id': 'provider-uuid', 'gateway_merchant_id': 'merchant-test',
              'fulfillment_owner': 'web', 'account_credit_reserved': 2}
    with database.transaction() as db:
        save_payment(db, 'main', 'web_test_order', record)
        db.execute('INSERT INTO web_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                   ('web_test_order', 'main', '123', 'key', 'hash', 'purchase', 'pending', '{}', 1, 1))
    return Services(), record


def provider(**changes):
    class Gateway:
        merchant_id = 'merchant-test'
        calls = []
        def check_payment_status(self, payment_id=None, *, order_id=None):
            self.calls.append((payment_id, order_id))
            return {'result': {'uuid': 'provider-uuid', 'order_id': 'web_test_order',
                    'currency': 'USD', 'amount': '10.00', 'payment_amount_usd': '10.00',
                    'status': 'paid', 'url': 'https://example.com/invoice', **changes}}
    return Gateway()


def test_valid_invoice_approved_once(invoice):
    from utils.web_gateway import poll
    from utils import database
    services, _ = invoice
    gateway = provider()
    poll(services, gateway)
    poll(services, gateway)
    assert services.payment(123, 'main', 'web_test_order')['status'] == 'approved'
    assert len(gateway.calls) == 1
    assert database.get_connection().execute("SELECT count(*) FROM web_audit WHERE action='gateway.reconciled'").fetchone()[0] == 1


@pytest.mark.parametrize('changes', [
    {'uuid': 'wrong'}, {'order_id': 'wrong'}, {'currency': 'EUR'}, {'amount': '20'},
    {'amount': 'NaN'}, {'payment_amount_usd': 'NaN'}, {'payment_amount_usd': '9.99'},
    {'payment_amount_usd': None}, {'payment_amount_usd': 'Infinity'}])
def test_invalid_or_underpaid_invoice_preserves_reservations(invoice, changes):
    from utils.web_gateway import poll
    services, _ = invoice
    poll(services, provider(**changes))
    record = services.payment(123, 'main', 'web_test_order')
    assert record['status'] == 'uncertain'
    assert record['account_credit_reserved'] == 2


def test_lost_creation_response_is_reconciled_by_persisted_order_id(invoice):
    from utils.web_gateway import poll
    from utils.web_orders import save_payment
    from utils import database
    services, _ = invoice
    with database.transaction() as db:
        save_payment(db, 'main', 'web_test_order', {'status': 'uncertain', 'gateway_payment_id': None,
                     'web_attention_reason': 'gateway_creation_uncertain'})
        db.execute("UPDATE web_operations SET status='uncertain',updated_at=1")
    gateway = provider()
    poll(services, gateway)
    assert gateway.calls == [(None, 'web_test_order')]
    assert services.payment(123, 'main', 'web_test_order')['gateway_payment_id'] == 'provider-uuid'


def test_uncertain_panel_fulfillment_is_never_reapproved_by_payment_poll(invoice):
    from utils.web_gateway import poll
    from utils.web_orders import save_payment
    from utils import database
    services, _ = invoice
    with database.transaction() as db:
        save_payment(db, 'main', 'web_test_order', {'status': 'uncertain', 'web_attention_reason': 'worker_interrupted'})
        db.execute("UPDATE web_operations SET status='uncertain',updated_at=1")
    gateway = provider()
    poll(services, gateway)
    assert gateway.calls == []
    assert services.payment(123, 'main', 'web_test_order')['status'] == 'uncertain'


def test_merchant_change_never_queries_or_approves_old_invoice(invoice):
    from utils.web_gateway import poll
    services, _ = invoice
    gateway = provider()
    gateway.merchant_id = 'different-merchant'
    poll(services, gateway)
    assert gateway.calls == []
    assert services.payment(123, 'main', 'web_test_order')['status'] == 'uncertain'


@pytest.mark.parametrize('response', [None, [], {'result': ['invalid']}, {'error': 'outage'}])
def test_malformed_or_unavailable_invoice_response_is_rate_limited_and_keeps_funds(invoice, response):
    from utils.web_gateway import poll
    from utils import database
    services, _ = invoice
    gateway = provider()
    calls = []
    def check(*args, **kwargs):
        assert not database.get_connection().in_transaction
        calls.append(True)
        return response
    gateway.check_payment_status = check
    poll(services, gateway)
    poll(services, gateway)
    assert calls == [True]
    record = services.payment(123, 'main', 'web_test_order')
    assert record['status'] == 'pending' and record['account_credit_reserved'] == 2


def test_old_uncertain_panel_operations_do_not_starve_invoice_reconciliation(invoice):
    from utils.web_gateway import poll
    from utils.web_orders import save_payment
    from utils import database
    services, record = invoice
    with database.transaction() as db:
        for index in range(25):
            key = f'uncertain-{index}'
            save_payment(db, 'main', key, {**record, 'status': 'uncertain', 'web_attention_reason': 'worker_interrupted'})
            db.execute('INSERT INTO web_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (key, 'main', '123', key, 'hash', 'purchase', 'uncertain', '{}', 0, 0))
    poll(services, provider())
    assert services.payment(123, 'main', 'web_test_order')['status'] == 'approved'


def test_provider_client_reuses_merchant_order_id_and_does_not_mutate_metadata(storage, monkeypatch):
    from utils.payments import CryptoPayment
    import utils.payments as payments
    monkeypatch.setenv('CRYPTO_MERCHANT_ID', 'merchant-test')
    monkeypatch.setenv('CRYPTO_API_KEY', 'synthetic')
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {'result': {'uuid': 'provider-uuid'}}
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(payments.requests, 'post', post)
    gateway = CryptoPayment()
    metadata = {'web_order_id': 'web_test_order'}
    gateway.create_payment('10', '100', 123, additional_data=metadata, order_id='web_test_order')
    gateway.create_payment('10', '100', 123, additional_data=metadata, order_id='web_test_order')
    gateway.check_payment_status(order_id='web_test_order')
    assert [call['order_id'] for call in calls] == ['web_test_order'] * 3
    assert metadata == {'web_order_id': 'web_test_order'}
