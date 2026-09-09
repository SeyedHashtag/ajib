"""Shared, transactional prepaid-first reservations for both reseller runtimes."""

import json
import logging
from utils import database, reseller as store
from utils.reseller_credit import cents
from utils.time_utils import parse_utc_timestamp
from utils.reseller_wholesale_credit import (
    get_wholesale_balance, reserve_wholesale_balance,
    consume_wholesale_balance, release_wholesale_balance,
)


class FundingUnavailable(ValueError):
    pass


class FundingChanged(FundingUnavailable):
    def __init__(self, quote):
        super().__init__('Funding changed; review the updated breakdown')
        self.quote = quote


def _saved(connection, reseller_id, operation_id):
    row = connection.execute('SELECT payload_json FROM reseller_order_funding WHERE reseller_id=? AND operation_id=?',
                             (str(reseller_id), str(operation_id))).fetchone()
    return json.loads(row['payload_json']) if row else None


def get_funding(reseller_id, operation_id):
    return _saved(database.get_connection(), reseller_id, operation_id)


def _save(connection, reseller_id, funding):
    connection.execute('''INSERT INTO reseller_order_funding
        (reseller_id, operation_id, status, debt_cents, payload_json) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(reseller_id, operation_id) DO UPDATE SET
        status=excluded.status, debt_cents=excluded.debt_cents, payload_json=excluded.payload_json''',
        (str(reseller_id), funding['operation_id'], funding['status'], funding['debt_cents'], json.dumps(funding)))


def borrowing_reserved(reseller_id):
    connection = database.get_connection()
    pending = connection.execute("SELECT COALESCE(SUM(debt_cents),0) FROM reseller_order_funding WHERE reseller_id=? AND status='reserved'",
                                 (str(reseller_id),)).fetchone()[0]
    legacy = connection.execute('SELECT COALESCE(SUM(amount_cents),0) FROM credit_reservations WHERE reseller_id=?',
                                (str(reseller_id),)).fetchone()[0]
    return (pending + legacy) / 100


def quote_funding(reseller_id, amount, *, record=None, balance=None):
    total = cents(amount)
    record = store.get_reseller_data(reseller_id) if record is None else record
    record = record or {}
    policy = store.get_reseller_credit_policy(record)
    balance = get_wholesale_balance(reseller_id) if balance is None else balance
    available = cents(balance.get('available', 0))
    prepaid = min(total, available)
    debt = total - prepaid
    reserved = cents(borrowing_reserved(reseller_id))
    remaining = max(0, cents(policy['effective_limit']) - cents(record.get('debt', 0)) - reserved)
    started = parse_utc_timestamp(record.get('debt_since'))
    overdue = (started is not None and not store._is_debt_fully_settled(record.get('debt', 0))
               and (store.utc_now() - started).total_seconds() >= store.get_reseller_debt_deadlines(record)['suspend_hours'] * 3600)
    return {'version': 1, 'total_cents': total, 'prepaid_cents': prepaid, 'debt_cents': debt,
            'external_cents': 0, 'available_prepaid_cents': available,
            'remaining_credit_cents': remaining, 'reserved_credit_cents': reserved,
            'limit': policy['effective_limit'], 'mode': 'mixed' if prepaid and debt else 'prepaid' if prepaid else 'debt',
            'allowed': total > 0 and debt <= remaining and record.get('status') == 'approved' and not overdue}


def reserve_funding(reseller_id, operation_id, amount, *, expected=None, metadata=None):
    if not operation_id or len(str(operation_id)) > 128:
        raise FundingUnavailable('A stable operation ID is required')
    # Production financial state is managed by the same SQLite database. Do not
    # pretend a JSON write and a wallet transaction can commit atomically.
    if not store._sqlite_managed():
        raise FundingUnavailable('Transactional reseller storage is required')
    with store.reseller_lock, database.write_transaction(operation='reseller_funding_reserve') as connection:
        existing = _saved(connection, reseller_id, operation_id)
        if existing and existing['status'] in {'reserved', 'completed'}:
            if existing['total_cents'] != cents(amount):
                raise FundingUnavailable('Operation amount does not match')
            return existing
        quote = quote_funding(reseller_id, amount)
        if expected and any(quote[k] != expected[k] for k in ('total_cents', 'prepaid_cents', 'debt_cents')):
            raise FundingChanged(quote)
        if not quote['allowed']:
            raise FundingUnavailable('Insufficient funding or selling unavailable')
        if quote['prepaid_cents']:
            reserved = reserve_wholesale_balance(reseller_id, operation_id, quote['prepaid_cents'] / 100)
            if cents(reserved) != quote['prepaid_cents']:
                raise FundingUnavailable('Prepaid reservation changed')
        quote.update(operation_id=str(operation_id), status='reserved', created_at=store._now_str(), metadata=metadata or {})
        _save(connection, reseller_id, quote)
        return quote


def release_funding(reseller_id, operation_id):
    with store.reseller_lock, database.write_transaction(operation='reseller_funding_release') as connection:
        saved = _saved(connection, reseller_id, operation_id)
        if not saved or saved['status'] != 'reserved':
            return False
        release_wholesale_balance(reseller_id, operation_id)
        saved['status'] = 'released'
        _save(connection, reseller_id, saved)
        return True


def remember_fulfillment(reseller_id, operation_id, data=None, *, kind='config', username=None, server_id=None):
    """Persist panel success before accounting, so a restart never repeats it."""
    with store.reseller_lock, database.write_transaction(operation='reseller_funding_fulfillment') as connection:
        saved = _saved(connection, reseller_id, operation_id)
        if not saved or saved['status'] != 'reserved':
            raise FundingUnavailable('Funding reservation missing')
        saved['fulfillment_started_at'] = store._now_str()
        if data is not None:
            saved['fulfillment'] = {'data': data, 'kind': kind, 'username': username, 'server_id': server_id}
        _save(connection, reseller_id, saved)


def pending_funding(reseller_id=None):
    query = "SELECT reseller_id, payload_json FROM reseller_order_funding WHERE status='reserved'"
    values = ()
    if reseller_id is not None:
        query += ' AND reseller_id=?'
        values = (str(reseller_id),)
    return [dict(json.loads(row['payload_json']), reseller_id=row['reseller_id'])
            for row in database.get_connection().execute(query, values)]


def reconcile_funding(*, reseller_id=None, origin='main', active_ids=(), now=None):
    """Retry committed panel work; release only abandoned, unstarted orders."""
    current = now or store.utc_now()
    recovered = []
    for saved in pending_funding(reseller_id):
        if saved.get('metadata', {}).get('origin') != origin or saved['operation_id'] in active_ids:
            continue
        age = (current - store._parse_time(saved['created_at'])).total_seconds()
        if age < 300:
            continue
        fulfillment = saved.get('fulfillment')
        if fulfillment:
            try:
                finalize_funding(saved['reseller_id'], saved['operation_id'], **fulfillment)
                recovered.append(saved)
            except Exception:
                logging.getLogger('ajib.reseller_funding').exception(
                    'funding_recovery_failed reseller=%s operation=%s', saved['reseller_id'], saved['operation_id'])
        elif not saved.get('fulfillment_started_at') and age >= 86400:
            release_funding(saved['reseller_id'], saved['operation_id'])
    return recovered


def finalize_funding(reseller_id, operation_id, data, *, kind='config', username=None, server_id=None):
    with store.reseller_lock, database.write_transaction(operation='reseller_funding_finalize') as connection:
        saved = _saved(connection, reseller_id, operation_id)
        if not saved or saved['status'] not in {'reserved', 'completed'}:
            raise FundingUnavailable('Funding reservation missing')
        if saved['status'] == 'completed':
            return saved.get('result', True)
        record = dict(data, retail_order_id=str(operation_id), funding=dict(saved))
        record['funding']['status'] = 'completed'
        total, debt = saved['total_cents'] / 100, saved['debt_cents'] / 100
        if saved['prepaid_cents']:
            consumed = consume_wholesale_balance(reseller_id, operation_id)
            if cents(consumed) != saved['prepaid_cents']:
                raise FundingUnavailable('Prepaid reservation could not be consumed')
        if kind == 'reserved_renewal':
            ok, result = store.reserve_reseller_renewal(reseller_id, username, total, record,
                server_id=server_id, funded=True, enforce_credit=False, debt_amount=debt)
        elif kind == 'renewal':
            ok = store.record_funded_reseller_renewal(reseller_id, username, total, record,
                                                     server_id=server_id, debt_amount=debt)
            result = True
        else:
            ok = store.record_funded_reseller_config(reseller_id, total, record, debt_amount=debt)
            result = True
        if not ok:
            raise FundingUnavailable('Order accounting failed')
        saved.update(status='completed', completed_at=store._now_str(), result=result)
        _save(connection, reseller_id, saved)
    store._update_recruitment_milestone(str(reseller_id), store.get_reseller_data(reseller_id) or {})
    return result
