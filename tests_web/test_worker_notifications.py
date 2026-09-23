from types import SimpleNamespace

import pytest


def test_financial_pause_delivers_committed_notification_without_processing_orders(storage, monkeypatch):
    from core.web.worker import run_once
    from utils import database, web_store, web_orders
    web_store.initialize()
    with database.transaction() as db:
        web_store.enqueue(db, 'completed-synthetic-order', 'main', 123, 'Your service is ready.')
    monkeypatch.setattr(web_orders.Orders, 'process_one', lambda *a: (_ for _ in ()).throw(AssertionError('financial processing paused')))
    sent = []
    def post(url, **kwargs):
        assert not database.get_connection().in_transaction
        sent.append(kwargs['json'])
        return SimpleNamespace(status_code=200, json=lambda: {'ok': True})
    monkeypatch.setattr('requests.post', post)
    services = SimpleNamespace(bot_token=lambda scope: 'synthetic-token')
    assert run_once(services, writes_enabled=False)
    assert not run_once(services, writes_enabled=False)
    assert sent == [{'chat_id': '123', 'text': 'Your service is ready.'}]


def test_notification_failure_retains_retry_without_touching_financial_state(storage, monkeypatch):
    from core.web.worker import run_once
    from utils import database, web_store
    web_store.initialize()
    with database.transaction() as db:
        web_store.enqueue(db, 'completed-synthetic-order', 'main', 123, 'Your service is ready.')
    monkeypatch.setattr('requests.post', lambda *a, **k: (_ for _ in ()).throw(TimeoutError('synthetic failure')))
    assert run_once(SimpleNamespace(bot_token=lambda scope: 'synthetic-token'), writes_enabled=False)
    row = database.get_connection().execute('SELECT status,attempts,last_error FROM web_outbox').fetchone()
    assert row['status'] == 'pending' and row['attempts'] == 1 and row['last_error'] == 'TimeoutError'


@pytest.mark.parametrize(('code', 'stored'), [(400, 'telegram_bad_request'),
    (401, 'telegram_unauthorized'), (403, 'telegram_forbidden'),
    (429, 'telegram_rate_limited'), (502, 'telegram_upstream_error')])
def test_notification_rejection_stores_only_safe_code(storage, monkeypatch, code, stored):
    from core.web.worker import deliver_notification
    from utils import database, web_store
    web_store.initialize()
    with database.transaction() as db:
        web_store.enqueue(db, 'event', 'main', 123, 'Your service is ready.')
    monkeypatch.setattr('requests.post', lambda *a, **k: SimpleNamespace(
        status_code=code, json=lambda: {'ok': False, 'error_code': code,
                                        'description': 'private provider response'}))
    assert deliver_notification(SimpleNamespace(bot_token=lambda scope: 'synthetic-token'))
    row = database.get_connection().execute('SELECT status,attempts,last_error FROM web_outbox').fetchone()
    expected_status = 'undeliverable' if code == 403 else 'pending'
    assert (row['status'], row['attempts'], row['last_error']) == (expected_status, 1, stored)


def test_prior_forbidden_retry_is_retained_without_another_send(storage, monkeypatch):
    from core.web.worker import run_once
    from utils import database, web_store
    web_store.initialize()
    with database.transaction() as db:
        web_store.enqueue(db, 'old-event', 'main', 123, 'Your service is ready.')
        db.execute("UPDATE web_outbox SET last_error='telegram_forbidden',attempts=5 WHERE id='old-event'")
    monkeypatch.setattr('requests.post', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not retry')))
    assert not run_once(SimpleNamespace(bot_token=lambda scope: 'synthetic-token'), writes_enabled=False)
    row = database.get_connection().execute('SELECT status,attempts,last_error FROM web_outbox').fetchone()
    assert (row['status'], row['attempts'], row['last_error']) == ('undeliverable', 5, 'telegram_forbidden')
