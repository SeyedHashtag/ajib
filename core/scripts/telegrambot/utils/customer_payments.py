"""Shared customer receipt/cancellation transitions; fulfillment owner is immutable."""
import io
import json
import os
import secrets
import time

from . import database, web_store
from .customer_progress import receipt_eligible
from .web_services import ServiceError, Services, payment_public


def validate_receipt(contents):
    from PIL import Image, UnidentifiedImageError
    if len(contents) > 5 * 1024 * 1024:
        raise ServiceError('Receipt must be smaller than 5 MB', 413)
    try:
        source = Image.open(io.BytesIO(contents))
        if source.format not in {'PNG', 'JPEG'} or source.width * source.height > 20_000_000:
            raise ValueError()
        source.load()
        result = io.BytesIO()
        source.convert('RGB').save(result, format='JPEG', quality=88)
        return result.getvalue()
    except (ValueError, OSError, Image.DecompressionBombError, UnidentifiedImageError):
        raise ServiceError('Upload a valid PNG or JPEG receipt') from None


def _assert_unclaimed(scope, payment_id):
    from .account_operations import assert_obligation_releasable, AccountBusy
    try:
        assert_obligation_releasable(scope, payment_id)
    except AccountBusy:
        raise ServiceError('This payment requires support review', 409) from None


def _web_pause(record, db):
    # Bot-only operation remains independent of website availability. A web-owned
    # order continued in Telegram must still respect the live web write gate.
    if record.get('fulfillment_owner') == 'web':
        row = db.execute('SELECT accept_writes FROM web_release_control WHERE id=1').fetchone()
        if row and not row[0]:
            raise ServiceError('Changes are temporarily paused', 503)


def customer_actions(payment_id, record):
    from .customer_progress import safe_progress
    allowed = True
    if record.get('fulfillment_owner') == 'web':
        row = database.get_connection().execute('SELECT accept_writes FROM web_release_control WHERE id=1').fetchone()
        allowed = not row or bool(row[0])
    return safe_progress(payment_id, record, writes=allowed)['actions']


def submit_receipt(user_id, scope, payment_id, contents):
    from .web_orders import save_payment
    if scope != 'main':
        raise ServiceError('This storefront action is unavailable', 409)
    contents = validate_receipt(contents)
    with database.transaction(operation='customer_receipt') as db:
        record = Services().payment(user_id, scope, payment_id)
        _web_pause(record, db)
        # A repeated upload acknowledges the first committed receipt; it cannot
        # replace evidence while a reviewer is using it.
        if record.get('status') == 'pending_approval' and record.get('web_receipt_id'):
            return {'id': record['web_receipt_id'], 'status': 'pending_approval'}
        if not receipt_eligible(record):
            raise ServiceError('This payment cannot accept a receipt; contact support', 409)
        _assert_unclaimed(scope, payment_id)
        receipt_id = secrets.token_hex(16)
        db.execute('INSERT INTO web_receipts VALUES (?,?,?,?,?,?,?)',
                   (receipt_id, scope, payment_id, str(user_id), 'image/jpeg', contents, int(time.time())))
        save_payment(db, scope, payment_id, {'status': 'pending_approval', 'web_receipt_id': receipt_id})
        db.execute("UPDATE web_operations SET status='pending_approval',updated_at=? WHERE id=? AND scope=?",
                   (int(time.time()), payment_id, scope))
        web_store.audit(db, user_id, scope, 'receipt.upload', payment_id)
        recipients = {str(actor) for actor in json.loads(os.getenv('ADMIN_USER_IDS', '[]'))}
        if record.get('routed_to_checker') and record.get('receipt_checker_user_id'):
            recipients.add(str(record['receipt_checker_user_id']))
        for recipient in recipients:
            from .customer_messages import message, language_for
            language = language_for(recipient, scope)
            web_store.enqueue(db, f'receipt:{receipt_id}:{recipient}', scope, recipient,
                              message(language, 'review_receipt') + '\n' + payment_id)
        return {'id': receipt_id, 'status': 'pending_approval'}


def cancel(user_id, scope, payment_id):
    from .web_orders import save_payment
    from .purchase_incentives import release_main_checkout
    if scope != 'main':
        raise ServiceError('This storefront action is unavailable', 409)
    with database.transaction(operation='customer_payment_cancel') as db:
        record = Services().payment(user_id, scope, payment_id)
        _web_pause(record, db)
        if record.get('status') == 'cancelled':
            return payment_public(payment_id, record)
        if not receipt_eligible(record):
            raise ServiceError('This payment cannot be cancelled automatically', 409)
        _assert_unclaimed(scope, payment_id)
        release_main_checkout(user_id, record.get('incentive_reservation_id') or payment_id)
        record = save_payment(db, scope, payment_id, {'status': 'cancelled'})
        db.execute("UPDATE web_operations SET status='cancelled',updated_at=? WHERE id=? AND scope=?",
                   (int(time.time()), payment_id, scope))
        web_store.audit(db, user_id, scope, 'checkout.cancel', payment_id)
        return payment_public(payment_id, record)


def persist_bot_checkout(user_id, state, plan, card_number):
    """Persist the original bot quote before displaying payment instructions."""
    from .receipt_checker import should_route_to_receipt_checker, get_receipt_checker_user_id
    from .web_orders import save_payment
    web_store.initialize()
    ident = state['card_checkout_id']
    routed = should_route_to_receipt_checker('regular')
    record = {**(state.get('renewal_metadata') or {}), **state['incentive_metadata'],
              'user_id': int(user_id), 'language': state['language'], 'plan_gb': state['plan_gb'],
              'days': plan['days'], 'unlimited': plan.get('unlimited', False),
              'payment_id': ident, 'fulfillment_owner': 'bot', 'status': 'waiting_receipt',
              'payment_method': 'Card to Card', 'receipt_type': 'regular', 'currency': 'USD',
              'card_number': card_number, 'converted_amount': state['converted_amount'],
              'converted_currency': state['converted_currency'], 'exchange_rate': state['exchange_rate'],
              'routed_to_checker': routed,
              'receipt_checker_user_id': get_receipt_checker_user_id() if routed else None}
    with database.transaction(operation='bot_checkout_persist') as db:
        if db.execute("SELECT 1 FROM payments WHERE scope='main' AND payment_id=?", (ident,)).fetchone():
            raise ServiceError('This checkout already exists', 409)
        save_payment(db, 'main', ident, record)
    state['payment_id'] = ident
    return ident


def receipt_bytes(payment_id, record):
    row = database.get_connection().execute(
        "SELECT contents FROM web_receipts WHERE id=? AND scope='main' AND payment_id=? AND user_id=?",
        (record.get('web_receipt_id'), payment_id, str(record.get('user_id')))).fetchone()
    if not row:
        raise ServiceError('Receipt unavailable', 404)
    return bytes(row[0])


def deliver_bot_notifications(bot):
    """Bot can deliver its outbox while HTTP/website worker is unavailable."""
    from .public_branding import require_public
    item = web_store.claim_notification(scope='main')
    if item:
        try:
            bot.send_message(item['recipient'], require_public(item['text']))
        except Exception as error:
            web_store.finish_notification(item, type(error).__name__)
        else:
            web_store.finish_notification(item)
    return bool(item)
