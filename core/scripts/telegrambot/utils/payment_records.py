import sqlite3

from utils.atomic_store import locked_json, read_json
from utils.payment_lifecycle import PAID_STATUSES
from utils.time_utils import format_utc_timestamp


PAYMENTS_FILE = '/etc/ajib/core/scripts/telegrambot/payments.json'


def _now():
    return format_utc_timestamp()


def _payment_store():
    return locked_json(PAYMENTS_FILE, {})


def _valid_payments(payments):
    return isinstance(payments, dict)


def load_payments():
    payments = read_json(PAYMENTS_FILE, {})
    return payments if _valid_payments(payments) else {}


def save_payments(payments):
    if not _valid_payments(payments):
        raise ValueError("Payment database must contain a JSON object.")

    with _payment_store() as current:
        if not _valid_payments(current):
            raise ValueError("Payment database must contain a JSON object.")
        current.clear()
        current.update(payments)


def add_payment_record(payment_id, data):
    if not isinstance(data, dict):
        raise ValueError("Payment record must contain a JSON object.")

    with _payment_store() as payments:
        if not _valid_payments(payments):
            raise ValueError("Payment database must contain a JSON object.")
        data['created_at'] = _now()
        data['updates'] = []
        payments[payment_id] = data


def update_payment_status(payment_id, status):
    try:
        with _payment_store() as payments:
            if not _valid_payments(payments) or payment_id not in payments:
                return False

            current_time = _now()
            payment = payments[payment_id]
            update = {
                'status': status,
                'timestamp': current_time,
                'previous_status': payment.get('status', 'unknown'),
            }
            payment['status'] = status
            payment['updated_at'] = current_time
            if str(status or '').strip().lower() in PAID_STATUSES and not payment.get('completed_at'):
                payment['completed_at'] = current_time
            updates = payment.setdefault('updates', [])
            if not isinstance(updates, list):
                updates = []
                payment['updates'] = updates
            updates.append(update)
            return True
    except (OSError, TypeError, ValueError, sqlite3.OperationalError):
        return False


def update_payment_record_fields(payment_id, fields):
    if not isinstance(fields, dict):
        return False

    try:
        with _payment_store() as payments:
            if not _valid_payments(payments) or payment_id not in payments:
                return False

            payments[payment_id].update(fields)
            payments[payment_id]['updated_at'] = _now()
            return True
    except (OSError, TypeError, ValueError, sqlite3.OperationalError):
        return False


def complete_payment_record(payment_id, fields, status='completed'):
    if not isinstance(fields, dict):
        return False

    try:
        with _payment_store() as payments:
            if not _valid_payments(payments) or payment_id not in payments:
                return False

            payment = payments[payment_id]
            current_time = _now()
            previous_status = payment.get('status', 'unknown')
            payment.update(fields)
            payment['status'] = status
            payment['updated_at'] = current_time
            if str(status or '').strip().lower() in PAID_STATUSES and not payment.get('completed_at'):
                payment['completed_at'] = current_time

            updates = payment.setdefault('updates', [])
            if not isinstance(updates, list):
                updates = []
                payment['updates'] = updates
            updates.append({
                'status': status,
                'timestamp': current_time,
                'previous_status': previous_status,
            })
            return True
    except (OSError, TypeError, ValueError, sqlite3.OperationalError):
        return False


def claim_payment_for_processing(payment_id, allowed_statuses=None):
    if allowed_statuses is None:
        allowed_statuses = {'pending'}
    else:
        allowed_statuses = {str(status) for status in allowed_statuses}

    try:
        try:
            from utils import state_store
        except ImportError:
            state_store = None

        if state_store is not None and state_store.is_managed_path(PAYMENTS_FILE):
            return state_store.claim_payment_for_processing(
                PAYMENTS_FILE,
                payment_id,
                allowed_statuses,
                _now(),
            )
        with _payment_store() as payments:
            if not _valid_payments(payments):
                return False

            payment = payments.get(payment_id)
            if not payment:
                return False

            current_status = str(payment.get('status', ''))
            if current_status not in allowed_statuses:
                return False

            current_time = _now()
            update = {
                'status': 'processing',
                'timestamp': current_time,
                'previous_status': current_status,
            }
            payment['status'] = 'processing'
            payment['updated_at'] = current_time
            updates = payment.setdefault('updates', [])
            if not isinstance(updates, list):
                updates = []
                payment['updates'] = updates
            updates.append(update)
            return True
    except (OSError, TypeError, ValueError, sqlite3.OperationalError):
        return False


def get_payment_record(payment_id):
    payments = load_payments()
    return payments.get(payment_id)


def get_user_payments(user_id):
    payments = load_payments()
    user_payments = {}
    for payment_id, payment_data in payments.items():
        if payment_data.get('user_id') == user_id:
            user_payments[payment_id] = payment_data
    return user_payments
