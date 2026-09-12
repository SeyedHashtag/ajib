"""Reconcile signed provider responses against the persisted merchant invoice."""
from decimal import Decimal, InvalidOperation
import time

from . import database, web_store
from .web_orders import save_payment


def _amount(value):
    try:
        result = Decimal(str(value))
        if result.is_finite() and result >= 0:
            return result
    except InvalidOperation:
        pass
    return None


def poll(services, gateway):
    now = int(time.time())
    rows = list(database.get_connection().execute('''SELECT o.id,o.scope,o.user_id,o.status,o.updated_at
        FROM web_operations o JOIN payments p ON p.scope=o.scope AND p.payment_id=o.id
        WHERE p.payment_method='Crypto' AND json_extract(p.payload_json,'$.fulfillment_owner')='web'
        AND ((o.status='pending' AND o.updated_at<?) OR (o.status='creating' AND o.updated_at<?)
          OR (o.status='uncertain' AND o.updated_at<? AND json_extract(p.payload_json,'$.web_attention_reason')
            IN ('gateway_creation_uncertain','gateway_creation_interrupted','gateway_response_invalid')))
        ORDER BY o.updated_at,o.id LIMIT 20''', (now - 15, now - 600, now - 600)))
    for row in rows:
        record = services.payment(row['user_id'], row['scope'], row['id'])
        if record.get('payment_method') != 'Crypto' or record.get('fulfillment_owner') != 'web':
            continue
        uuid, order_id = record.get('gateway_payment_id'), record.get('gateway_order_id')
        if row['status'] == 'uncertain' and record.get('web_attention_reason') not in {
                'gateway_creation_uncertain', 'gateway_creation_interrupted', 'gateway_response_invalid'}:
            continue
        if not uuid and not order_id:
            continue
        # Bound retries and rotate unavailable invoices so one old order cannot
        # starve the rest of the queue. This transaction ends before provider I/O.
        with database.transaction(operation='web_gateway_poll_claim') as connection:
            claimed = connection.execute('UPDATE web_operations SET updated_at=? WHERE id=? AND status=? AND updated_at=?',
                (now, row['id'], row['status'], row['updated_at'])).rowcount
        if not claimed:
            continue
        # A changed merchant must not reconcile or fulfill an old merchant's order.
        merchant_matches = bool(record.get('gateway_merchant_id') and
                                record['gateway_merchant_id'] == gateway.merchant_id)
        response = gateway.check_payment_status(uuid) if merchant_matches and uuid else (
            gateway.check_payment_status(order_id=order_id) if merchant_matches else {})
        if not isinstance(response, dict) or response.get('error'):
            continue  # Provider outage is not proof that the invoice does not exist.
        result = response.get('result') or {}
        if not isinstance(result, dict):
            continue
        if not result and merchant_matches:
            continue
        amount, expected = _amount(result.get('amount')), _amount(record.get('price'))
        valid = (merchant_matches and result.get('uuid') and
                 (not uuid or result['uuid'] == uuid) and bool(order_id) and result.get('order_id') == order_id
                 and str(result.get('currency', '')).upper() == record.get('currency', 'USD')
                 and expected is not None and amount == expected)
        status = str(result.get('status') or result.get('payment_status') or '').lower()
        changes = {}
        if not valid:
            changes = {'status': 'uncertain', 'web_attention_reason': 'gateway_response_invalid'}
        elif status in {'paid', 'paid_over'}:
            # The invoice amount is not the amount actually received. For USD
            # invoices use the provider's explicit USD payment amount.
            paid = _amount(result.get('payment_amount_usd')) if record.get('currency', 'USD') == 'USD' else None
            changes = ({'status': 'approved', 'web_attention_reason': None} if paid is not None and paid >= expected else
                       {'status': 'uncertain', 'web_attention_reason': 'gateway_paid_amount_unverified'})
            if changes['status'] == 'approved':
                changes['gateway_verified_payment_amount_usd'] = str(paid)
        elif status in {'cancel', 'fail', 'system_fail', 'wrong_amount'}:
            changes = {'status': 'uncertain', 'web_attention_reason': 'gateway_' + status}
        elif status in {'check', 'confirm_check', 'process', 'paid_partial', 'wrong_amount_waiting'}:
            changes = {'status': 'pending', 'web_attention_reason': None}
        if valid:
            changes.update(gateway_payment_id=result['uuid'])
            url = str(result.get('url', ''))
            if url.startswith('https://'):
                changes['payment_url'] = url
        if not changes:
            continue
        with database.transaction(operation='web_gateway_reconcile') as connection:
            current = connection.execute('SELECT status,updated_at FROM web_operations WHERE id=?', (row['id'],)).fetchone()
            if not current or current['status'] != row['status'] or current['updated_at'] != now:
                continue  # A concurrent review/recovery already advanced this order.
            updated = save_payment(connection, row['scope'], row['id'], changes)
            connection.execute('UPDATE web_operations SET status=?,updated_at=? WHERE id=?',
                               (updated['status'], now, row['id']))
            web_store.audit(connection, 'worker', row['scope'], 'gateway.reconciled', row['id'],
                            {'status': updated['status'], 'reason': updated.get('web_attention_reason')})
