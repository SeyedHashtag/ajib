import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest


def card_setup(monkeypatch):
    from utils import receipt_checker, exchange_rate
    monkeypatch.setattr(receipt_checker, "get_card_number_for_receipt_type", lambda _: "0000 0000 0000 0000")
    monkeypatch.setattr(exchange_rate, "get_exchange_rate", lambda: 100000)


def test_card_checkout_duplicate_and_receipt_access(client, login, monkeypatch):
    card_setup(monkeypatch)
    login(client)
    assert client.put('/api/v1/me/language', json={'language':'fa'}).status_code == 200
    request = {'plan_id':'40','method':'card'}
    headers = {'Idempotency-Key':'test-checkout-key-001'}
    first = client.post('/api/v1/orders',json=request,headers=headers)
    assert first.status_code == 200, first.text
    duplicate = client.post('/api/v1/orders',json=request,headers=headers)
    assert duplicate.json()['id'] == first.json()['id']
    assert client.post('/api/v1/orders',json={**request,'plan_id':'60'},headers=headers).status_code == 409
    payment_id = first.json()['id']
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB',(20,20),'white').save(image,format='PNG')
    result=client.post(f'/api/v1/payments/{payment_id}/receipt',files={'file':('receipt.png',image.getvalue(),'image/png')})
    assert result.status_code == 200, result.text
    receipt_id=result.json()['id']
    assert client.get(f'/api/v1/receipts/{receipt_id}').status_code == 200
    login(client,999)
    assert client.get(f'/api/v1/receipts/{receipt_id}').status_code == 404
    login(client,1)
    assert client.get(f'/api/v1/receipts/{receipt_id}').status_code == 200
    assert client.post(f'/api/v1/admin/payments/{payment_id}/review',json={'approve':True,'reason':'Verified transfer'}).status_code == 200
    assert client.post(f'/api/v1/admin/payments/{payment_id}/review',json={'approve':True,'reason':'Duplicate attempt'}).status_code == 409


def test_parallel_checkout_only_reserves_once(storage, monkeypatch):
    card_setup(monkeypatch)
    from utils import database
    from utils.web_orders import Orders
    from utils.web_services import Services
    def run():
        try:
            return Orders(Services()).create('123','main','same-key-123456789','40','card','fa')['id']
        finally:
            for connection in database._connection_map().values(): connection.close()
            database._connection_map().clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(lambda _:run(),range(4)))
    assert len(set(ids)) == 1
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_operations').fetchone()[0] == 1


def test_uncertain_provision_is_not_replayed(storage, monkeypatch):
    card_setup(monkeypatch)
    from utils import database
    from utils.web_orders import Orders, save_payment
    from utils.web_services import Services
    calls=[]
    panel=SimpleNamespace(server_id='primary',add_user=lambda *a,**kw:calls.append(a) or None)
    services=Services(SimpleNamespace(select_server_for_new_user=lambda:panel))
    orders=Orders(services)
    result=orders.create('123','main','create-timeout-key1','40','card','fa')
    with database.transaction() as connection:
        save_payment(connection,'main',result['id'],{'status':'approved'})
        connection.execute("UPDATE web_operations SET status='approved'")
    assert orders.process_one()
    assert not orders.process_one()
    assert len(calls)==1
    assert services.payment('123','main',result['id'])['status']=='uncertain'


def test_outbox_failure_retries_without_losing_message(storage):
    from utils import database, web_store
    with database.transaction() as connection:
        web_store.enqueue(connection,'event-1','main',123,'Synthetic notification')
        web_store.enqueue(connection,'event-1','main',123,'Duplicate')
    first=web_store.claim_notification()
    assert first['text']=='Synthetic notification'
    assert web_store.claim_notification() is None
    web_store.finish_notification(first,'Timeout')
    row=database.get_connection().execute('SELECT * FROM web_outbox').fetchone()
    assert row['status']=='pending' and row['attempts']==1


def test_cancel_releases_reservations(client, login, monkeypatch):
    card_setup(monkeypatch)
    login(client)
    client.put('/api/v1/me/language',json={'language':'fa'})
    result=client.post('/api/v1/orders',json={'plan_id':'40','method':'card'},headers={'Idempotency-Key':'cancel-checkout-123'}).json()
    response=client.post(f"/api/v1/payments/{result['id']}/cancel")
    assert response.status_code==200, response.text
    assert response.json()['status']=='cancelled'


def test_receipt_rejects_non_images(client, login, monkeypatch):
    card_setup(monkeypatch)
    login(client)
    client.put('/api/v1/me/language',json={'language':'fa'})
    result=client.post('/api/v1/orders',json={'plan_id':'40','method':'card'},headers={'Idempotency-Key':'receipt-checkout-123'}).json()
    response=client.post(f"/api/v1/payments/{result['id']}/receipt",files={'file':('payload.jpg',b'<script>bad</script>','image/jpeg')})
    assert response.status_code==400


def test_credit_funded_web_order_keeps_bot_discount_rules(storage):
    from utils.account_credit import credit_account, get_account_credit
    from utils.web_orders import Orders
    from utils.web_services import Services
    credit_account(123, 2, 'synthetic-credit')
    # There is no crypto payment, so the bot's 5% crypto incentive is removed.
    result = Orders(Services()).create('123', 'main', 'credit-funded-order-123', '40', 'crypto', 'en')
    assert result['price'] == 0
    assert result['account_credit_reserved'] == 1.2
    assert result['discount_amount'] == 0
    assert result['status'] == 'approved'
    assert get_account_credit(123)['reserved'] == 1.2


def test_shared_renewal_quote_preserves_combined_discount(storage):
    from utils.purchase_incentives import reserve_order_checkout
    quote = reserve_order_checkout(123, 'synthetic-renewal', 10, 'crypto',
        renewal_discount_percent=10, discount_cap_percent=15, allow_invite_discount=False)
    assert quote['price'] == 8.5
    assert quote['renewal_discount_amount'] == 1
    assert quote['crypto_discount_amount'] == 0.5


def test_bot_cannot_claim_worker_owned_payment(storage, monkeypatch):
    card_setup(monkeypatch)
    from utils.web_orders import Orders
    from utils.web_services import Services
    from utils.payment_records import claim_payment_for_processing, PAYMENTS_FILE
    from utils import state_store
    payment = Orders(Services()).create('123', 'main', 'worker-owned-checkout', '40', 'card', 'fa')
    assert not claim_payment_for_processing(payment['id'], {'waiting_receipt'})
    assert not state_store.claim_payment_for_processing(PAYMENTS_FILE, payment['id'], {'waiting_receipt'}, '2026-01-01T00:00:00Z')


def test_successful_fulfillment_consumes_credit_only_once(storage):
    from utils.account_credit import credit_account, get_account_credit
    from utils.web_orders import Orders
    from utils.web_services import Services
    from utils import database
    credit_account(123, 2, 'synthetic-completion-credit')
    calls=[]
    def create(*args, **kwargs):
        assert not database.get_connection().in_transaction
        calls.append(args)
        return {'username':args[0]}
    panel=SimpleNamespace(server_id='primary',add_user=create)
    services=Services(SimpleNamespace(select_server_for_new_user=lambda:panel))
    orders=Orders(services)
    payment=orders.create('123','main','successful-order-key','40','crypto','en')
    assert orders.process_one()
    assert not orders.process_one()
    assert len(calls)==1
    assert services.payment('123','main',payment['id'])['status']=='completed'
    balance=get_account_credit(123)
    assert balance['reserved']==0 and balance['available']==0.8
