"""Accounting-only completion shared by transports and operator recovery."""
import json

from . import account_operations, database


def funding_operations(reseller_id, reservation_id):
    """Finish only funding-owned operations, in their accounting transaction."""
    if not account_operations.enabled():
        return
    db = database.get_connection()
    for row in db.execute("""SELECT operation_id FROM account_operation_details
        WHERE json_extract(origin_json,'$.type')='funding'
        AND json_extract(origin_json,'$.reseller_id')=? AND json_extract(origin_json,'$.id')=?""",
        (str(reseller_id), str(reservation_id))).fetchall():
        account_operations.complete(row[0])


def main_payment(payment_id, fields, *, notify=False):
    from .web_orders import save_payment
    from .purchase_incentives import finalize_main_checkout
    with database.transaction(operation='main_obligation_complete') as db:
        row = db.execute("SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?", (payment_id,)).fetchone()
        if not row:
            raise ValueError('Payment not found')
        record = json.loads(row[0])
        if record.get('status') in {'rejected', 'cancelled', 'canceled'}:
            raise ValueError('Payment obligation is no longer authorized')
        ident = 'main-payment:' + payment_id
        metadata = account_operations.details(ident)
        operation = account_operations.existing(ident)
        if operation:
            if not metadata or operation['status'] != 'succeeded':
                raise account_operations.AccountBusy('Panel outcome requires investigation')
            origin = json.loads(metadata['origin_json'])
            if account_operations.payment_terms(record) != origin.get('terms_digest'):
                raise account_operations.AccountBusy('Payment terms changed after dispatch')
            if operation['kind'] == 'renewal':
                from .renewal import _mark_payment_record_renewed, mark_cleanup_state_renewed
                result = json.loads(operation['result_json'])
                fields = {**fields, 'renewal_after_state': result.get('after_state'),
                          'renewal_before_state': result.get('before_state')}
                _mark_payment_record_renewed(record.get('renewal_base_record_id'), result.get('after_state'))
                mark_cleanup_state_renewed(operation['username'], operation['server_id'])
                if record.get('renewal_mode') == 'reserved':
                    from .time_utils import format_utc_timestamp
                    fields.update(renewal_status='applied', renewal_applied_at=format_utc_timestamp(),
                                  renewal_claim_id=None, renewal_claimed_at=None,
                                  renewal_attention_reason=None, renewal_last_error=None, renewal_next_attempt_at=None)
        record = save_payment(db, 'main', payment_id, {**fields, 'status': 'completed'})
        result = finalize_main_checkout(payment_id, record)
        account_operations.complete('main-payment:' + payment_id)
        if record.get('fulfillment_owner') == 'web':
            db.execute("UPDATE web_operations SET status='completed',updated_at=strftime('%s','now') WHERE id=?", (payment_id,))
        if notify:
            from . import web_store
            messages = {
                'en': 'Your service is ready. Open My connections in Telegram or on the website.',
                'fa': 'سرویس شما آماده است. اتصال‌های من را در تلگرام یا وب‌سایت باز کنید.',
                'ru': 'Ваш сервис готов. Откройте «Мои подключения» в Telegram или на сайте.',
                'tk': 'Hyzmatyňyz taýýar. Telegramda ýa-da web sahypasynda birikmeleriňizi açyň.',
            }
            web_store.enqueue(db, 'complete:' + payment_id, 'main', record['user_id'],
                              messages.get(record.get('language'), messages['en']))
        return record, result
