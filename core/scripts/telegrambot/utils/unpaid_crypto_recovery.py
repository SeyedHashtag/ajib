"""Operator-only closure of a provider-cancelled, unfunded web invoice.

Provider inspection is read-only and happens outside the database transaction.
The evidence digest binds that inspection to the original payment and operation.
"""
import hashlib
import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import database, web_store


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _snapshot(connection, payment_id):
    connection.row_factory = sqlite3.Row
    payment = connection.execute(
        "SELECT * FROM payments WHERE scope='main' AND payment_id=?", (payment_id,)).fetchone()
    operation = connection.execute(
        "SELECT * FROM web_operations WHERE scope='main' AND id=?", (payment_id,)).fetchone()
    if not payment or not operation:
        raise ValueError('The original web payment and operation are required.')
    record = json.loads(payment['payload_json'])
    verified_raw = record.get('gateway_verified_payment_amount_usd')
    verified = _money(verified_raw) if verified_raw not in (None, '') else Decimal(0)
    if (payment['status'] != 'uncertain' or operation['status'] != 'uncertain'
            or record.get('status') != 'uncertain'
            or record.get('web_attention_reason') != 'gateway_cancel'
            or payment['payment_method'] != 'Crypto'
            or record.get('payment_method') != 'Crypto'
            or record.get('fulfillment_owner') != 'web'
            or record.get('gateway_order_id') != payment_id
            or not record.get('gateway_payment_id')
            or not record.get('gateway_merchant_id')
            or str(payment['user_id']) != str(operation['user_id'])
            or str(payment['user_id']) != str(record.get('user_id'))
            or operation['kind'] not in {'purchase', 'renewal'}
            or record.get('type') != operation['kind']
            or payment['kind'] != operation['kind']
            or payment['currency'] != record.get('currency', 'USD')
            or not _money(record.get('price'))
            or Decimal(payment['amount_cents']) != _money(record.get('price')) * 100
            or verified != 0):
        raise ValueError('The original invoice is not an eligible unpaid cancellation.')
    if connection.execute("SELECT 1 FROM account_operations WHERE operation_id=? LIMIT 1",
                          ('main-payment:' + payment_id,)).fetchone():
        raise ValueError('The original payment has account-operation history; keep it reserved.')
    if connection.execute("""SELECT 1 FROM payment_events WHERE scope='main' AND payment_id=?
        AND status IN ('approved','processing','completed','paid','succeeded') LIMIT 1""",
        (payment_id,)).fetchone():
        raise ValueError('The payment has fulfillment history; keep it reserved.')
    reservation = record.get('incentive_reservation_id') or payment_id
    if record.get('account_credit_reservation_id') not in (None, '', reservation):
        raise ValueError('Checkout reservation provenance differs from the original payment.')
    for origin in {payment_id, 'credit-' + payment_id, str(reservation)}:
        claimed = connection.execute('''SELECT 1 FROM account_operation_details
            WHERE phase!='completed' AND json_extract(origin_json,'$.scope')='main'
            AND (json_extract(origin_json,'$.id')=? OR EXISTS (
                SELECT 1 FROM json_each(json_extract(origin_json,'$.reservation_ids')) WHERE value=?))
            LIMIT 1''', (origin, origin)).fetchone()
        legacy = connection.execute('''SELECT 1 FROM account_operations o
            LEFT JOIN account_operation_details d ON d.operation_id=o.operation_id
            WHERE o.operation_id=? AND d.operation_id IS NULL LIMIT 1''',
            ('main-payment:' + origin,)).fetchone()
        if claimed or legacy:
            raise ValueError('An account operation still owns the invoice reservation.')
    bound = json.dumps({
        'payment': dict(payment), 'operation': dict(operation),
    }, sort_keys=True, separators=(',', ':'), default=str)
    return record, reservation, hashlib.sha256(bound.encode()).hexdigest()


def _read(payment_id):
    path = Path(database.database_path()).as_uri() + '?mode=ro'
    with sqlite3.connect(path, uri=True) as connection:
        return _snapshot(connection, payment_id)


def inspect(payment_id, gateway):
    record, _, row_digest = _read(payment_id)
    if record['gateway_merchant_id'] != gateway.merchant_id:
        raise ValueError('The invoice belongs to a different merchant; keep it reserved.')
    response = gateway.check_payment_status(payment_id=record['gateway_payment_id'])
    if (not isinstance(response, dict) or response.get('error')
            or response.get('state', 0) != 0 or not isinstance(response.get('result'), dict)):
        raise ValueError('Provider evidence is unavailable; keep the invoice reserved.')
    result = response['result']
    amount = _money(result.get('amount'))
    received = _money(result.get('payment_amount_usd')) if 'payment_amount_usd' in result else None
    paid_amount = _money(result.get('payment_amount')) if 'payment_amount' in result else None
    if (result.get('uuid') != record['gateway_payment_id']
            or result.get('order_id') != payment_id
            or str(result.get('currency', '')).upper() != record.get('currency', 'USD')
            or amount != _money(record['price'])
            or str(result.get('status') or result.get('payment_status') or '').lower() != 'cancel'
            or (result.get('payment_status') is not None
                and str(result['payment_status']).lower() != 'cancel')
            or result.get('is_final') is not True
            or received != 0
            or ('payment_amount' in result and paid_amount != 0)):
        raise ValueError('Provider identity, terms, terminal status or zero receipt is unproven.')
    evidence = {'row_digest': row_digest, 'merchant': record['gateway_merchant_id'],
                'invoice': record['gateway_payment_id'], 'order': payment_id,
                'currency': record.get('currency', 'USD'), 'amount': str(amount),
                'received_usd': str(received), 'status': 'cancel', 'is_final': True}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return {'payment_id': payment_id, 'status': 'provider_cancelled_unfunded',
            'evidence_digest': digest, 'row_digest': row_digest, 'provider_received_usd': '0',
            'panel_request_needed': False}


def apply(payment_id, evidence_digest, gateway):
    if not evidence_digest:
        raise ValueError('Inspect the invoice and supply its evidence digest.')
    # A repeated invocation after the commit acknowledges the same original
    # operation without releasing benefits or writing another audit event.
    with sqlite3.connect(Path(database.database_path()).as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT payload_json,status FROM payments WHERE scope='main' AND payment_id=?", (payment_id,)).fetchone()
        if row and row[1] == 'cancelled':
            record = json.loads(row[0])
            if record.get('gateway_cancel_evidence_digest') == evidence_digest:
                return {'payment_id': payment_id, 'status': 'already_reconciled'}
    report = inspect(payment_id, gateway)
    if report['evidence_digest'] != evidence_digest:
        raise ValueError('Evidence changed; inspect again before applying.')
    from .purchase_incentives import release_main_checkout
    from .web_orders import save_payment
    with database.transaction(operation='unpaid_crypto_reconcile') as db:
        gate = db.execute('SELECT accept_writes FROM web_release_control WHERE id=1').fetchone()
        if not gate or gate[0]:
            raise ValueError('Pause new customer writes before reconciliation.')
        record, reservation, current_digest = _snapshot(db, payment_id)
        if current_digest != report['row_digest']:
            raise ValueError('Payment or operation changed; inspect again.')
        release_main_checkout(record['user_id'], reservation)
        save_payment(db, 'main', payment_id, {
            'status': 'cancelled', 'web_attention_reason': 'gateway_cancel_verified',
            'gateway_cancel_evidence_digest': evidence_digest})
        db.execute("UPDATE web_operations SET status='cancelled',updated_at=? WHERE id=? AND status='uncertain'",
                   (int(time.time()), payment_id))
        web_store.audit(db, 'cli', 'main', 'gateway.cancelled_unfunded', payment_id,
                        {'evidence_digest': evidence_digest, 'provider_received_usd': '0'})
    return {'payment_id': payment_id, 'status': 'cancelled', 'panel_request_sent': False}
