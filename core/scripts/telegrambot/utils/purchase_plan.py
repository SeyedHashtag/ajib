import json
import datetime
import html
from telebot import types
from utils.command import bot, ADMIN_USER_IDS, is_admin
from utils.common import create_main_markup
try:
    from utils.common import record_main_growth_event
except ImportError:
    def record_main_growth_event(*args, **kwargs):
        return False
from utils.edit_plans import load_plans
from utils.payments import CryptoPayment
from utils.payment_records import (
    add_payment_record,
    update_payment_status,
    get_payment_record,
    load_payments,
    claim_payment_for_processing,
    get_user_payments,
    update_payment_record_fields,
    complete_payment_record,
)
from utils.api_client import APIClient, MultiServerAPI
from utils.translations import BUTTON_TRANSLATIONS, get_message_text, get_button_text
from utils.language import get_user_language
from utils.referral import add_referral_reward, get_pending_withdrawal_requests
from utils.reseller import evaluate_reseller_debt_policies, DEBT_WARNING_THRESHOLD, DEBT_SUSPEND_THRESHOLD
from utils.currency_format import format_toman_amount, format_usd_amount
from utils.exchange_rate import get_exchange_rate
from utils.receipt_checker import (
    RECEIPT_TYPE_REGULAR,
    RECEIPT_TYPE_SETTLEMENT,
    calculate_checker_share_amount,
    calculate_checker_share_amount_toman,
    can_review_receipt,
    get_card_number_for_receipt_type,
    get_receipt_checker_user_id,
    get_receipt_checker_share_percent,
    get_receipt_type_label,
    is_receipt_checker,
    should_route_to_receipt_checker,
)
import qrcode
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
import uuid
import logging
from utils.username_utils import (
    allocate_username,
    build_user_note,
    extract_existing_usernames,
    format_username_timestamp,
    load_recorded_usernames,
    RecordedUsernameLoadError,
)
from utils.telegram_safe import safe_answer_callback_query, safe_send_message
from utils.download_guidance import send_download_prompt_safely

# New: Global dictionary for user states
user_data = {}

TELEGRAM_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
CRYPTO_PAYMENT_DISCOUNT_PERCENT = 5
PAYMENT_JOB_INFLIGHT = set()
PAYMENT_JOB_LOCK = threading.Lock()
PURCHASE_DISCLOSURES_FILE = os.getenv(
    "AJIB_PURCHASE_DISCLOSURES_FILE",
    "/etc/ajib/core/scripts/telegrambot/purchase_disclosures.json",
)
_PURCHASE_DISCLOSURE_FALLBACK = set()
CARD_CHECKOUT_REMINDERS_FILE = os.getenv(
    "AJIB_CARD_CHECKOUT_REMINDERS_FILE",
    "/etc/ajib/core/scripts/telegrambot/card_checkout_reminders.json",
)
_CARD_CHECKOUT_FALLBACK = {}
_CARD_CHECKOUT_FALLBACK_LOCK = threading.RLock()
CHECKOUT_REMINDER_DELAY = datetime.timedelta(minutes=30)


def _int_env(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


PAYMENT_JOB_EXECUTOR = ThreadPoolExecutor(
    max_workers=_int_env("AJIB_PAYMENT_JOB_WORKERS", 2),
    thread_name_prefix="ajib-payment",
)


def apply_crypto_discount(amount):
    value = Decimal(str(amount))
    multiplier = Decimal('1') - (Decimal(str(CRYPTO_PAYMENT_DISCOUNT_PERCENT)) / Decimal('100'))
    return float((value * multiplier).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def build_crypto_discount_metadata(original_amount):
    original_decimal = Decimal(str(original_amount))
    original_price = float(original_decimal)
    discounted_price = apply_crypto_discount(original_decimal)
    discount_amount = float(
        (original_decimal - Decimal(str(discounted_price))).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP,
        )
    )
    return {
        'price': discounted_price,
        'original_price': original_price,
        'discount_percent': CRYPTO_PAYMENT_DISCOUNT_PERCENT,
        'discount_amount': discount_amount,
    }


def _reserve_checkout_incentives(
    user_id,
    reservation_id,
    original_price,
    payment_method,
    *,
    allow_account_credit=True,
):
    """Build and reserve the auditable main-store checkout quote."""
    try:
        from utils.purchase_incentives import release_main_checkout, reserve_main_checkout
    except ImportError:
        # Compatibility for isolated deployments/tests during a rolling update.
        legacy = (
            build_crypto_discount_metadata(original_price)
            if payment_method == 'crypto'
            else {
                'price': float(original_price),
                'original_price': float(original_price),
                'discount_percent': 0.0,
                'discount_amount': 0.0,
            }
        )
        return {
            **legacy,
            'incentive_reservation_id': str(reservation_id),
            'collected_amount': legacy['price'],
            'referral_reward_base': legacy['price'],
            'payment_discount_percent': (
                CRYPTO_PAYMENT_DISCOUNT_PERCENT if payment_method == 'crypto' else 0.0
            ),
            'invite_discount_percent': 0.0,
            'account_credit_reserved': 0.0,
        }

    quote = reserve_main_checkout(
        user_id,
        reservation_id,
        original_price,
        payment_method=payment_method,
        payment_discount_percent=(
            CRYPTO_PAYMENT_DISCOUNT_PERCENT if payment_method == 'crypto' else 0
        ),
        payments=get_user_payments(user_id),
        allow_account_credit=allow_account_credit,
    )
    if (
        payment_method == 'crypto'
        and float(quote.get('price', 0) or 0) <= 0
        and float(quote.get('account_credit_reserved', 0) or 0) > 0
    ):
        # A crypto discount is earned only when some crypto is actually paid.
        # Requote without that discount. Credit may still fund part of the
        # order, with only the remaining amount sent to the crypto gateway.
        release_main_checkout(user_id, reservation_id)
        quote = reserve_main_checkout(
            user_id,
            reservation_id,
            original_price,
            payment_method='account_credit',
            payment_discount_percent=0,
            payments=get_user_payments(user_id),
            allow_account_credit=allow_account_credit,
        )
    quote['fully_credit_funded'] = bool(
        float(quote.get('price', 0) or 0) <= 0
        and float(quote.get('account_credit_reserved', 0) or 0) > 0
    )
    quote['incentive_reservation_id'] = str(reservation_id)
    return quote


def _release_checkout_incentives(user_id, reservation_id):
    try:
        from utils.purchase_incentives import release_main_checkout
    except ImportError:
        return False
    release_main_checkout(user_id, reservation_id)
    return True


def _close_unpaid_gateway_checkout(payment_id, payment_record, gateway_status):
    """Close terminal unpaid crypto checkouts and return reserved benefits."""
    normalized = str(gateway_status or '').strip().lower()
    terminal_statuses = {
        'cancel': 'canceled',
        'cancelled': 'canceled',
        'canceled': 'canceled',
        'expired': 'expired',
        'failed': 'failed',
        'rejected': 'rejected',
    }
    local_status = terminal_statuses.get(normalized)
    if not local_status:
        return False
    update_payment_status(payment_id, local_status)
    _release_checkout_incentives(
        payment_record.get('user_id'),
        payment_record.get('incentive_reservation_id')
        or payment_record.get('account_credit_reservation_id'),
    )
    return True


def _format_checkout_incentives(language, quote):
    lines = []
    invite_percent = float(quote.get('invite_discount_percent', 0) or 0)
    if invite_percent > 0:
        lines.append(get_message_text(language, 'invite_discount_summary').format(
            percent=f"{invite_percent:g}",
            discount_amount=format_usd_amount(quote.get('invite_discount_amount', 0)),
        ))
    credit = float(quote.get('account_credit_reserved', 0) or 0)
    if credit > 0:
        lines.append(get_message_text(language, 'account_credit_summary').format(
            amount=format_usd_amount(credit),
        ))
    return "\n".join(lines)


def _finalize_checkout_incentives(payment_id, payment_record):
    """Finalize idempotent ledgers after fulfillment has succeeded."""
    try:
        from utils.purchase_incentives import finalize_main_checkout
    except ImportError:
        return None
    result = finalize_main_checkout(payment_id, payment_record)
    update_payment_record_fields(payment_id, {
        'account_credit_consumed': result.get('credit_consumed', 0),
        'invite_discount_redeemed': bool(result.get('invite_redeemed')),
        'referral_reward': result.get('reward_amount', 0),
        'reward_calculation_base': result.get('reward_base', 0),
        'incentives_finalized_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })
    if result.get('reward_created') and float(result.get('reward_amount', 0) or 0) > 0:
        try:
            referrer_id = int(result['referrer_id'])
            referrer_language = get_user_language(referrer_id)
            safe_send_message(
                bot,
                referrer_id,
                get_message_text(referrer_language, 'referral_purchase_rewarded').format(
                    amount=format_usd_amount(result['reward_amount']),
                ),
            )
        except Exception:
            logging.getLogger('ajib.referrals').exception(
                "Could not notify referrer for completed payment %s",
                payment_id,
            )
    return result


def _reconcile_completed_checkout_incentives():
    for payment_id, record in load_payments().items():
        if not isinstance(record, dict) or record.get('status') != 'completed':
            continue
        if record.get('incentives_finalized_at'):
            continue
        if not any(record.get(key) for key in (
            'incentive_reservation_id',
            'account_credit_reservation_id',
            'referrer_id',
        )):
            continue
        try:
            _finalize_checkout_incentives(payment_id, record)
        except Exception:
            logging.getLogger('ajib.payments').exception(
                "Failed to reconcile incentives for payment %s",
                payment_id,
            )


def build_crypto_discount_display(language, discount_metadata):
    crypto_percent = discount_metadata.get(
        'payment_discount_percent',
        discount_metadata.get('discount_percent', CRYPTO_PAYMENT_DISCOUNT_PERCENT),
    )
    crypto_discount_amount = discount_metadata.get(
        'crypto_discount_amount',
        discount_metadata.get('payment_discount_amount', discount_metadata['discount_amount']),
    )
    crypto_only_total = round(
        float(discount_metadata['original_price']) - float(crypto_discount_amount or 0),
        2,
    )
    return {
        'summary': get_message_text(language, "crypto_discount_summary").format(
            percent=f"{float(crypto_percent):g}",
            original_price=format_usd_amount(discount_metadata['original_price']),
            discounted_price=format_usd_amount(crypto_only_total),
            discount_amount=format_usd_amount(crypto_discount_amount),
        ),
        'button_text': get_crypto_discount_button_text(language),
    }


def get_crypto_discount_button_text(language):
    return get_message_text(language, "crypto_discount_button").format(
        percent=CRYPTO_PAYMENT_DISCOUNT_PERCENT
    )


def _customer_plan_items(plans):
    items = []
    for gb, details in (plans or {}).items():
        if not isinstance(details, dict) or details.get('target', 'both') == 'reseller':
            continue
        try:
            numeric_gb = Decimal(str(gb))
            price = Decimal(str(details['price']))
            days = int(details['days'])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if numeric_gb <= 0 or price < 0 or days <= 0:
            continue
        items.append((str(gb), details))
    return sorted(items, key=lambda item: (Decimal(str(item[0])), Decimal(str(item[1]['price']))))


def select_quick_pick_plans(plans, configured_plan_id=None):
    """Select factual, deduplicated entry plans for a lower-friction catalog."""
    items = _customer_plan_items(plans)
    if not items:
        return []

    by_id = dict(items)
    cheapest = min(items, key=lambda item: (Decimal(str(item[1]['price'])), Decimal(str(item[0]))))
    best_value = min(
        items,
        key=lambda item: (
            Decimal(str(item[1]['price'])) / Decimal(str(item[0])),
            Decimal(str(item[1]['price'])),
        ),
    )

    recommended_id = str(configured_plan_id or os.getenv("AJIB_RECOMMENDED_PLAN_ID") or "").strip()
    recommended = by_id.get(recommended_id)
    if recommended is None:
        recommended = next(
            (item for item in items if item[1].get("recommended") is True),
            None,
        )
    recommendation_label = "quick_pick_recommended"
    if recommended is None:
        recommended = items[len(items) // 2]
        recommendation_label = "quick_pick_balanced"

    selected = []
    seen = set()
    for label_key, item in (
        ("quick_pick_cheapest", cheapest),
        (recommendation_label, recommended),
        ("quick_pick_best_value", best_value),
    ):
        if item[0] in seen:
            continue
        seen.add(item[0])
        selected.append((label_key, item[0], item[1]))
    return selected


def _plan_price_pair(language, price, exchange_rate):
    usd = format_usd_amount(price)
    toman = format_toman_amount(float(price) * exchange_rate)
    key = "plan_price_pair_toman_first" if language == "fa" else "plan_price_pair_usd_first"
    return get_message_text(language, key).format(usd=usd, toman=toman)


def _plan_button_text(language, gb, details, exchange_rate, label_key=None):
    label = f"{get_message_text(language, label_key)} · " if label_key else ""
    return get_message_text(language, "customer_plan_button").format(
        label=label,
        plan_gb=gb,
        price_pair=_plan_price_pair(language, details['price'], exchange_rate),
        days=details['days'],
    )


def build_plan_payment_totals(
    language,
    plan_gb,
    price,
    exchange_rate,
    *,
    invite_discount_percent=0,
):
    original = Decimal(str(price))
    invite_percent = Decimal(str(invite_discount_percent or 0))
    try:
        from utils.referral import stacked_discount_percent

        crypto_percent = Decimal(str(stacked_discount_percent(
            invite_percent,
            CRYPTO_PAYMENT_DISCOUNT_PERCENT,
        )))
    except ImportError:
        crypto_percent = min(
            Decimal('10'),
            invite_percent + Decimal(str(CRYPTO_PAYMENT_DISCOUNT_PERCENT)),
        )
    card_total = (
        original * (Decimal('1') - invite_percent / Decimal('100'))
    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    crypto_total = (
        original * (Decimal('1') - crypto_percent / Decimal('100'))
    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    key = "plan_payment_totals_toman_first" if language == "fa" else "plan_payment_totals_usd_first"
    return get_message_text(language, key).format(
        plan_gb=plan_gb,
        card_total=format_toman_amount(float(card_total) * exchange_rate),
        crypto_total=format_usd_amount(crypto_total),
        original_usd=format_usd_amount(price),
        crypto_percent=f"{float(crypto_percent):g}",
    )


_PAYMENT_STATUS_TRANSLATION_KEYS = {
    'completed': 'payment_status_completed',
    'complete': 'payment_status_completed',
    'processing': 'payment_status_processing',
    'pending': 'payment_status_pending_label',
    'paid': 'payment_status_paid',
    'failed': 'payment_status_failed',
    'expired': 'payment_status_expired',
    'rejected': 'payment_status_rejected',
    'canceled': 'payment_status_canceled',
    'cancelled': 'payment_status_canceled',
}


def _localized_payment_status(language, status):
    key = _PAYMENT_STATUS_TRANSLATION_KEYS.get(
        str(status or '').strip().lower(),
        'payment_status_unknown',
    )
    return get_message_text(language, key)


def _localized_ipv4_info(language, ipv4_url):
    if not ipv4_url:
        return ''
    return get_message_text(language, 'renewal_ipv4_line').format(ipv4_url=ipv4_url)


def _invite_discount_preview(user_id):
    try:
        from utils.referral import (
            combined_discount_cap_percent,
            get_invitee_discount_eligibility,
        )

        eligibility = get_invitee_discount_eligibility(
            user_id,
            payments=get_user_payments(user_id),
        )
        if not eligibility.get('eligible'):
            return 0.0
        return min(
            float(eligibility.get('percent', 0) or 0),
            float(combined_discount_cap_percent()),
        )
    except Exception:
        return 0.0


def _consume_purchase_disclosure(user_id):
    """Return True once per customer and persist that the disclosure was shown."""
    key = str(user_id)
    try:
        from utils.atomic_store import locked_json

        with locked_json(PURCHASE_DISCLOSURES_FILE, {}) as stored:
            if not isinstance(stored, dict):
                raise ValueError("Purchase disclosure store must contain an object.")
            if key in stored:
                return False
            stored[key] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return True
    except Exception:
        if key in _PURCHASE_DISCLOSURE_FALLBACK:
            return False
        _PURCHASE_DISCLOSURE_FALLBACK.add(key)
        return True


def _checkout_reminders_enabled():
    try:
        from utils.growth_features import REMINDERS, is_growth_feature_enabled

        return is_growth_feature_enabled(REMINDERS)
    except (ImportError, ModuleNotFoundError, AttributeError):
        # Compatibility with isolated tests and rolling upgrades that have not
        # loaded the shared growth feature module yet.
        return str(os.getenv("AJIB_BUYER_REMINDERS_ENABLED", "true")).strip().lower() not in {
            "0", "false", "no", "off",
        }
    except ValueError as error:
        logging.getLogger('ajib.payments').error(
            "Checkout reminders disabled because the feature flag is invalid: %s",
            error,
        )
        return False


def _mutate_card_checkouts(mutator):
    """Atomically mutate durable pre-receipt card checkout state."""
    try:
        from utils.atomic_store import locked_json

        with locked_json(CARD_CHECKOUT_REMINDERS_FILE, {}) as records:
            if not isinstance(records, dict):
                raise ValueError("Card checkout reminder store must contain an object.")
            return mutator(records)
    except (ImportError, ModuleNotFoundError):
        # Some compatibility tests intentionally load purchase_plan without
        # the repository-backed storage layer.
        with _CARD_CHECKOUT_FALLBACK_LOCK:
            return mutator(_CARD_CHECKOUT_FALLBACK)


def _register_card_checkout(
    user_id,
    chat_id,
    plan_gb,
    final_amount,
    resume_callback,
    *,
    now=None,
    checkout_id=None,
):
    current = now or datetime.datetime.now()
    checkout_id = str(checkout_id or uuid.uuid4())
    timestamp = current.strftime('%Y-%m-%d %H:%M:%S')
    superseded = []

    def register(records):
        for record in records.values():
            if (
                isinstance(record, dict)
                and str(record.get('user_id')) == str(user_id)
                and record.get('status') == 'waiting_receipt'
            ):
                record['status'] = 'superseded'
                record['closed_at'] = timestamp
                superseded.append(str(record.get('checkout_id') or ''))
        records[checkout_id] = {
            'checkout_id': checkout_id,
            'user_id': user_id,
            'chat_id': chat_id,
            'plan_gb': str(plan_gb),
            'final_amount': float(final_amount),
            'resume_callback': str(resume_callback),
            'status': 'waiting_receipt',
            'created_at': timestamp,
        }

    _mutate_card_checkouts(register)
    for previous_id in superseded:
        if previous_id and previous_id != checkout_id:
            _release_checkout_incentives(user_id, previous_id)
    return checkout_id


def _close_card_checkout(checkout_id, status, *, now=None):
    if not checkout_id:
        return False
    timestamp = (now or datetime.datetime.now()).strftime('%Y-%m-%d %H:%M:%S')

    def close(records):
        record = records.get(str(checkout_id))
        if not isinstance(record, dict) or record.get('status') != 'waiting_receipt':
            return False
        record['status'] = str(status)
        record['closed_at'] = timestamp
        return True

    return _mutate_card_checkouts(close)


def send_due_card_checkout_reminders(now=None):
    """Send one durable reminder for card checkouts awaiting a receipt."""
    if not _checkout_reminders_enabled():
        return 0
    current = now or datetime.datetime.now()
    timestamp = current.strftime('%Y-%m-%d %H:%M:%S')
    due = []
    expired = []

    def reserve_due(records):
        for checkout_id, record in records.items():
            if not isinstance(record, dict) or record.get('status') != 'waiting_receipt':
                continue
            try:
                created_at = datetime.datetime.strptime(
                    str(record.get('created_at')),
                    '%Y-%m-%d %H:%M:%S',
                )
            except (TypeError, ValueError):
                continue
            elapsed = current - created_at
            if elapsed >= datetime.timedelta(hours=24):
                record['status'] = 'expired'
                record['closed_at'] = timestamp
                expired.append((checkout_id, record.get('user_id')))
                continue
            if elapsed < CHECKOUT_REMINDER_DELAY or record.get('checkout_reminded_at'):
                continue
            # Persist before I/O so concurrent pollers and restarts remain
            # at-most-once even if Telegram delivery fails.
            record['checkout_reminded_at'] = timestamp
            due.append(dict(record))

    _mutate_card_checkouts(reserve_due)

    for checkout_id, user_id in expired:
        _release_checkout_incentives(user_id, checkout_id)
        state = user_data.get(user_id)
        if isinstance(state, dict) and state.get('card_checkout_id') == checkout_id:
            user_data.pop(user_id, None)

    sent = 0
    for record in due:
        user_id = record.get('user_id')
        if user_id is None:
            continue
        language = get_user_language(user_id)
        markup = types.InlineKeyboardMarkup(row_width=1)
        resume_callback = record.get('resume_callback')
        if resume_callback:
            markup.add(types.InlineKeyboardButton(
                get_button_text(language, "card_to_card"),
                callback_data=resume_callback,
            ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "support"),
            callback_data="purchase_support",
        ))
        try:
            bot.send_message(
                record.get('chat_id') or user_id,
                get_message_text(language, "abandoned_card_checkout_reminder").format(
                    plan_gb=record.get('plan_gb'),
                    final_amount=format_toman_amount(record.get('final_amount', 0)),
                ),
                reply_markup=markup,
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            logging.getLogger('ajib.payments').exception(
                "Failed to send card checkout reminder to user %s",
                user_id,
            )
    return sent


def maybe_send_checkout_reminder(payment_id, record, now=None):
    """Send one durable reminder for an abandoned customer crypto checkout."""
    if not _checkout_reminders_enabled() or not isinstance(record, dict):
        return False
    if record.get('status') != 'pending' or record.get('checkout_reminded_at'):
        return False
    if record.get('type') == 'settlement' or record.get('plan_gb') == 'Settlement':
        return False
    created_at_value = record.get('created_at')
    try:
        created_at = datetime.datetime.strptime(str(created_at_value), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return False
    current = now or datetime.datetime.now()
    if current - created_at < CHECKOUT_REMINDER_DELAY or current - created_at >= datetime.timedelta(hours=24):
        return False

    user_id = record.get('user_id')
    if user_id is None:
        return False
    language = get_user_language(user_id)
    final_amount = format_usd_amount(record.get('price', 0))
    markup = types.InlineKeyboardMarkup(row_width=1)
    payment_url = record.get('payment_url')
    if payment_url:
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "payment_link"),
            url=payment_url,
        ))
    markup.add(types.InlineKeyboardButton(
        get_button_text(language, "check_status"),
        callback_data=f"check_payment:{payment_id}",
    ))
    markup.add(types.InlineKeyboardButton(
        get_button_text(language, "support"),
        callback_data="purchase_support",
    ))
    reminder_timestamp = current.strftime('%Y-%m-%d %H:%M:%S')
    if not update_payment_record_fields(payment_id, {
        'checkout_reminded_at': reminder_timestamp,
    }):
        return False
    try:
        bot.send_message(
            user_id,
            get_message_text(language, "abandoned_checkout_reminder").format(
                plan_gb=record.get('plan_gb'),
                final_amount=final_amount,
            ),
            reply_markup=markup,
            parse_mode="Markdown",
        )
    except Exception:
        return False
    return True


def _queue_payment_job(job_key, target, *args, **kwargs):
    with PAYMENT_JOB_LOCK:
        if job_key in PAYMENT_JOB_INFLIGHT:
            return False
        PAYMENT_JOB_INFLIGHT.add(job_key)

    def run():
        try:
            target(*args, **kwargs)
        finally:
            with PAYMENT_JOB_LOCK:
                PAYMENT_JOB_INFLIGHT.discard(job_key)

    try:
        PAYMENT_JOB_EXECUTOR.submit(run)
    except Exception:
        with PAYMENT_JOB_LOCK:
            PAYMENT_JOB_INFLIGHT.discard(job_key)
        raise
    return True


def _queue_customer_crypto_payment(call, plan_gb):
    user_id = getattr(getattr(call, "from_user", None), "id", None)
    job_key = ("customer_crypto", user_id, str(plan_gb))
    return _queue_payment_job(job_key, handle_crypto_payment, call, plan_gb, False)


def _debt_state_label_key(debt_state):
    if debt_state == 'suspended':
        return 'debt_state_suspended'
    if debt_state == 'warning':
        return 'debt_state_warning'
    return 'debt_state_active'


def _receipt_type_from_record(payment_record):
    receipt_type = payment_record.get('receipt_type')
    if receipt_type:
        return receipt_type
    if payment_record.get('type') == 'settlement' or payment_record.get('plan_gb') == 'Settlement':
        return RECEIPT_TYPE_SETTLEMENT
    return RECEIPT_TYPE_REGULAR


def _settlement_credit_amount(payment_record):
    return payment_record.get(
        'settlement_amount',
        payment_record.get('original_price', payment_record.get('price', 0))
    )


def _apply_reseller_settlement_payment(user_id, payment_record):
    from utils.reseller import apply_reseller_payment

    credited_amount = _settlement_credit_amount(payment_record)
    try:
        result = apply_reseller_payment(
            user_id,
            credited_amount,
            payment_id=payment_record.get('payment_id') or payment_record.get('order_id'),
        )
    except TypeError:
        # Compatibility for integrations that still expose the legacy two-argument helper.
        result = apply_reseller_payment(user_id, credited_amount)
    success = bool(result[0]) if isinstance(result, tuple) and result else bool(result)
    if success:
        try:
            from utils.reseller_level_ui import present_pending_reseller_level

            present_pending_reseller_level(
                bot,
                user_id,
                get_user_language(user_id),
                allow_introduction=False,
            )
        except Exception:
            pass
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), credited_amount, result[1]
    return True, credited_amount, None


def _remaining_reseller_debt(user_id, fallback=None):
    if fallback is not None:
        try:
            return float(fallback)
        except (TypeError, ValueError):
            pass
    try:
        from utils.reseller import get_reseller_data
        reseller_data = get_reseller_data(user_id) or {}
        return float(reseller_data.get('debt', 0.0))
    except Exception:
        return 0.0


def _settlement_approved_message(language, user_id, credited_amount, remaining_debt):
    return get_message_text(language, "settlement_payment_approved").format(
        amount=format_usd_amount(credited_amount),
        remaining_debt=format_usd_amount(_remaining_reseller_debt(user_id, remaining_debt)),
    )


def _send_reseller_settlement_admin_notification(
    user_id,
    payment_id,
    payment_record,
    credited_amount=None,
    payment_method="Crypto",
    telegram_username=None,
):
    if telegram_username is None:
        try:
            chat = bot.get_chat(user_id)
            telegram_username = chat.username
        except Exception:
            telegram_username = None

    price = payment_record.get('price')
    if price is None:
        price = credited_amount if credited_amount is not None else _settlement_credit_amount(payment_record)

    notification_kwargs = {"telegram_username": telegram_username}
    if payment_record.get('converted_amount') is not None:
        notification_kwargs.update({
            "converted_amount": payment_record.get('converted_amount'),
            "converted_currency": payment_record.get('converted_currency'),
            "exchange_rate": payment_record.get('exchange_rate'),
        })

    send_admin_payment_notification(
        user_id,
        "Settlement",
        "Settlement",
        price,
        payment_id,
        payment_method,
        **notification_kwargs,
    )


def _renewal_reason_text(language, reason):
    key = reason or "renewal_generic_unavailable_reason"
    translated = get_message_text(language, key)
    if not translated or translated == key:
        return get_message_text(language, "renewal_generic_unavailable_reason")
    return translated


def _process_customer_renewal_payment(payment_id, payment_record, notify_chat_id=None, payment_method=None, telegram_username=None):
    from utils.renewal import execute_customer_renewal, format_renewal_success

    user_id = payment_record.get('user_id')
    language = get_user_language(user_id)
    notify_chat_id = notify_chat_id or user_id
    payment_method = payment_method or payment_record.get('payment_method', 'Card to Card')

    if payment_record.get('renewal_mode') == 'reserved':
        from utils.renewal import mark_payment_renewal_reserved
        import utils.payment_records as payment_records_store

        username = payment_record.get('renewal_username')
        server_id = payment_record.get('renewal_server_id')
        if not mark_payment_renewal_reserved(
            payment_id,
            payments_file=getattr(payment_records_store, 'PAYMENTS_FILE', None),
            fields={
                'username': username,
                'server_id': server_id,
                'renewal_before_state': payment_record.get('renewal_before_state'),
            },
        ):
            _notify_sale_completion_persistence_failure(payment_id, user_id, username, server_id)
            return False
        finalized = _finalize_checkout_incentives(
            payment_id,
            get_payment_record(payment_id) or payment_record,
        )
        if finalized is None:
            add_referral_reward(user_id, payment_record.get('price', 0), payment_id)
        send_admin_payment_notification(
            user_id,
            username,
            payment_record.get('plan_gb'),
            payment_record.get('price', 0),
            payment_id,
            payment_method,
            telegram_username=telegram_username,
            converted_amount=payment_record.get('converted_amount'),
            converted_currency=payment_record.get('converted_currency'),
            exchange_rate=payment_record.get('exchange_rate'),
            server_id=server_id,
        )
        reserved_text = get_message_text(language, 'renewal_reserved_success')
        bot.send_message(notify_chat_id, reserved_text, parse_mode='Markdown')
        return True

    result = execute_customer_renewal(payment_record)
    if not result.get('success'):
        update_payment_record_fields(payment_id, {
            "renewal_failure_reason": result.get('reason'),
            "renewal_before_state": result.get('before_state', payment_record.get('renewal_before_state')),
        })
        update_payment_status(payment_id, 'renewal_failed')
        _release_checkout_incentives(
            user_id,
            payment_record.get('incentive_reservation_id')
            or payment_record.get('account_credit_reservation_id'),
        )
        update_payment_record_fields(payment_id, {
            'incentives_released_after_failed_renewal': True,
        })
        bot.send_message(
            notify_chat_id,
            get_message_text(language, "renewal_failed").format(
                reason=_renewal_reason_text(language, result.get('reason'))
            ),
            parse_mode="Markdown"
        )
        return False

    username = result.get('username') or payment_record.get('renewal_username')
    api_client = result.get('api_client')
    plan_gb = payment_record.get('plan_gb')
    days = payment_record.get('days')

    completion_fields = {
        "username": username,
        "server_id": result.get('server_id') or getattr(api_client, 'server_id', None),
        "renewal_after_state": result.get('after_state'),
        "renewal_before_state": result.get('before_state', payment_record.get('renewal_before_state')),
    }
    if not _complete_sale_payment_or_notify(
        payment_id,
        user_id,
        username,
        api_client,
        fields=completion_fields,
    ):
        return False
    add_referral_reward(user_id, payment_record.get('price', 0), payment_id)

    send_admin_payment_notification(
        user_id,
        username,
        plan_gb,
        payment_record.get('price', 0),
        payment_id,
        payment_method,
        telegram_username=telegram_username,
        converted_amount=payment_record.get('converted_amount'),
        converted_currency=payment_record.get('converted_currency'),
        exchange_rate=payment_record.get('exchange_rate'),
        server_name=getattr(api_client, 'server_name', None),
        server_id=result.get('server_id') or getattr(api_client, 'server_id', None),
    )

    user_uri_data = api_client.get_user_uri(username) if api_client else None
    sub_url = user_uri_data.get('normal_sub') if user_uri_data else None
    ipv4_url = user_uri_data.get('ipv4', '') if user_uri_data else ''
    success_message = format_renewal_success(language, result, plan_gb, days, sub_url=sub_url, ipv4_url=ipv4_url)

    if sub_url:
        qr = qrcode.make(ipv4_url or sub_url)
        bio = io.BytesIO()
        qr.save(bio, 'PNG')
        bio.seek(0)
        bot.send_photo(
            notify_chat_id,
            photo=bio,
            caption=success_message,
            parse_mode="Markdown"
        )
        send_download_prompt_safely(bot, notify_chat_id, language)
    else:
        bot.send_message(notify_chat_id, success_message, parse_mode="Markdown")
    return True


def _build_receipt_approval_markup(payment_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"admin_approval:approve:{payment_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"admin_approval:reject:{payment_id}")
    )
    return markup


def _format_pending_receipt_caption(payment_id, payment_record, telegram_username=None):
    receipt_type = _receipt_type_from_record(payment_record)
    user_id = payment_record.get('user_id')
    if receipt_type == RECEIPT_TYPE_SETTLEMENT:
        plan_label = "Settlement"
    elif payment_record.get('type') == 'renewal':
        plan_label = f"Renewal {payment_record.get('plan_gb')} GB"
    else:
        plan_label = f"{payment_record.get('plan_gb')} GB"
    caption = (
        f"⏳ New Pending Payment\n\n"
        f"A user has submitted a receipt for a 'Card to Card' payment.\n\n"
        f"🧾 <b>Receipt Type:</b> {get_receipt_type_label(receipt_type)}\n"
        f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
    )
    if telegram_username:
        caption += f"📱 <b>Telegram Username:</b> @{telegram_username}\n"
    caption += (
        f"📊 <b>Plan:</b> {plan_label}\n"
        f"💵 <b>Amount:</b> ${format_usd_amount(payment_record.get('price', 0))}\n"
    )
    if payment_record.get('converted_amount') is not None:
        currency_label = payment_record.get('converted_currency') or "Tomans"
        caption += f"💱 <b>Converted Amount:</b> {format_toman_amount(payment_record.get('converted_amount'))} {currency_label}\n"
    if payment_record.get('created_at'):
        caption += f"📅 <b>Submitted:</b> {payment_record.get('created_at')}\n"
    caption += f"🔑 <b>Payment ID:</b> <code>{payment_id}</code>"
    return caption


def _send_receipt_confirmation(chat_id, payment_id, payment_record, caption=None):
    caption = caption or _format_pending_receipt_caption(payment_id, payment_record)
    markup = _build_receipt_approval_markup(payment_id)
    receipt_path = payment_record.get('receipt_path')
    if receipt_path and os.path.exists(receipt_path):
        with open(receipt_path, 'rb') as photo:
            sent_message = bot.send_photo(
                chat_id,
                photo,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML"
            )
    else:
        sent_message = bot.send_message(
            chat_id,
            caption + "\n\nReceipt image is not available on disk.",
            reply_markup=markup,
            parse_mode="HTML"
        )
    return sent_message


def _build_referral_withdrawal_markup(request_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("✅ Mark as Paid", callback_data=f"admin_pay_ref:{request_id}"))
    return markup


def _format_pending_referral_withdrawal(withdrawal_request):
    username = str(withdrawal_request.get("telegram_username") or "").strip().lstrip("@")
    telegram_line = f"Telegram: `@{username}`\n" if username else ""
    return (
        "💸 **Pending Referral Withdrawal**\n\n"
        f"Request ID: `{withdrawal_request.get('id')}`\n"
        f"User ID: `{withdrawal_request.get('user_id')}`\n"
        f"{telegram_line}"
        f"Amount: ${float(withdrawal_request.get('amount', 0) or 0):.2f}\n"
        f"Wallet: `{withdrawal_request.get('wallet')}`\n\n"
        "📊 **Referral Stats**\n"
        f"Invited Users: {withdrawal_request.get('invited_count', 0)}\n"
        f"Total Earnings: ${float(withdrawal_request.get('total_earnings', 0) or 0):.2f}\n"
        f"Remaining Balance: ${float(withdrawal_request.get('available_balance_after', 0) or 0):.2f}\n"
        f"Requested At: {withdrawal_request.get('requested_at', '')}"
    )


def _send_referral_withdrawal_confirmation(chat_id, withdrawal_request):
    return bot.send_message(
        chat_id,
        _format_pending_referral_withdrawal(withdrawal_request),
        reply_markup=_build_referral_withdrawal_markup(withdrawal_request.get("id")),
        parse_mode="Markdown"
    )


def _save_receipt_message_refs(payment_id, refs):
    if refs:
        update_payment_record_fields(payment_id, {"receipt_message_refs": refs})


def _update_receipt_message_refs(payment_id, payment_record, final_caption):
    refs = payment_record.get('receipt_message_refs') or []
    for ref in refs:
        try:
            chat_id = ref.get('chat_id')
            message_id = ref.get('message_id')
            content_type = ref.get('content_type')
            if content_type == 'photo':
                bot.edit_message_caption(
                    caption=final_caption,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None
                )
            else:
                bot.edit_message_text(
                    final_caption,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                    parse_mode="HTML"
                )
        except Exception as e:
            print(f"Failed to update receipt message {payment_id}: {str(e)}")

    if not refs:
        try:
            bot.edit_message_reply_markup(
                chat_id=payment_record.get('last_receipt_chat_id'),
                message_id=payment_record.get('last_receipt_message_id'),
                reply_markup=None
            )
        except Exception:
            pass


def _is_confirmation_viewer(user_id):
    return is_admin(user_id) or is_receipt_checker(user_id)


def _record_review_audit(payment_id, call, action, reviewer_role):
    update_payment_record_fields(payment_id, {
        "reviewed_by_user_id": call.from_user.id,
        "reviewed_by_role": reviewer_role,
        "reviewed_action": action,
        "reviewed_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


def _record_checker_share_audit(payment_id, payment_record):
    if not payment_record.get('routed_to_checker'):
        return
    share_percent = get_receipt_checker_share_percent()
    fields = {
        "checker_share_percent": share_percent,
        "checker_share_amount": calculate_checker_share_amount(payment_record.get('price', 0), share_percent),
    }
    if payment_record.get('converted_amount') is not None:
        fields.update({
            "checker_accounting_amount_toman": payment_record.get('converted_amount'),
            "checker_share_amount_toman": calculate_checker_share_amount_toman(payment_record.get('converted_amount'), share_percent),
        })
    update_payment_record_fields(payment_id, fields)


def _answer_payment_already_processed(call, language, payment_id, default_status='unknown'):
    latest_record = get_payment_record(payment_id) or {}
    latest_status = latest_record.get('status', default_status)
    bot.answer_callback_query(
        call.id,
        text=get_message_text(language, "payment_already_processed").format(status=latest_status)
    )


def _claim_payment_or_answer(call, language, payment_id, allowed_statuses):
    if claim_payment_for_processing(payment_id, allowed_statuses=allowed_statuses):
        return True
    _answer_payment_already_processed(call, language, payment_id)
    return False


def _can_access_payment_record(user_id, payment_record):
    owner_id = payment_record.get('user_id')
    if owner_id is None:
        return is_admin(user_id)
    return is_admin(user_id) or str(owner_id) == str(user_id)


def _payment_owner_id(payment_record):
    owner_id = payment_record.get('user_id')
    try:
        return int(owner_id)
    except (TypeError, ValueError):
        return owner_id


def _payment_owner_username(payment_record, fallback=None):
    owner_id = _payment_owner_id(payment_record)
    if owner_id is None:
        return fallback
    try:
        chat = bot.get_chat(owner_id)
        return chat.username
    except Exception:
        return fallback


def _record_processing_error(payment_id, error):
    update_payment_record_fields(payment_id, {
        "processing_error": str(error),
        "processing_failed_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


def _release_processing_for_retry(payment_id, retry_status, error):
    _record_processing_error(payment_id, error)
    update_payment_status(payment_id, retry_status)


def _record_sale_creation_failure(payment_id, username=None, api_client=None):
    error = "failed_to_create_user"
    if not username or api_client is None:
        _release_processing_for_retry(payment_id, 'pending_approval', error)
    else:
        _record_processing_error(payment_id, error)


def _record_crypto_sale_creation_failure(payment_id, username=None, api_client=None):
    error = "failed_to_create_user"
    if not username or api_client is None:
        _release_processing_for_retry(payment_id, 'pending', error)
    else:
        _record_processing_error(payment_id, error)


def _notify_sale_completion_persistence_failure(payment_id, user_id, username, server_id):
    message = (
        "Payment completion persistence failed.\n\n"
        f"Payment ID: {payment_id}\n"
        f"User ID: {user_id}\n"
        f"Username: {username or 'N/A'}\n"
        f"Server ID: {server_id or 'N/A'}\n\n"
        "The VPN user was created or renewed, but the payment record was not completed. "
        "The record was left in processing for manual follow-up."
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            bot.send_message(admin_id, message)
        except Exception:
            pass


def _complete_sale_payment_or_notify(payment_id, user_id, username, api_client, fields=None):
    server_id = getattr(api_client, 'server_id', None) if api_client is not None else None
    completion_fields = dict(fields or {})
    completion_fields.setdefault("username", username)
    completion_fields.setdefault("server_id", server_id)
    if complete_payment_record(payment_id, completion_fields):
        try:
            completed_record = get_payment_record(payment_id) or {
                'user_id': user_id,
                **completion_fields,
            }
            _finalize_checkout_incentives(payment_id, completed_record)
        except Exception as error:
            update_payment_record_fields(payment_id, {
                'incentive_finalization_error': str(error)[:500],
            })
            logging.getLogger('ajib.payments').exception(
                "Checkout incentive finalization failed for payment %s",
                payment_id,
            )
        return True

    error = f"payment_completion_persistence_failed username={username} server_id={server_id}"
    _record_processing_error(payment_id, error)
    logging.getLogger('ajib.payments').error(
        "Payment completion persistence failed. payment_id=%s user_id=%s username=%s server_id=%s",
        payment_id,
        user_id,
        username,
        server_id,
    )
    _notify_sale_completion_persistence_failure(payment_id, user_id, username, server_id)
    return False


def create_sale_username(api_client, user_id):
    recorded_usernames = load_recorded_usernames()
    if isinstance(api_client, set):
        return allocate_username("s", user_id, set(api_client) | recorded_usernames)
    multi_api = MultiServerAPI()
    creation = multi_api.prepare_new_user_creation()
    usernames = creation.get("existing_usernames") or set()
    if not usernames and api_client is not None:
        users = api_client.get_users()
        usernames = extract_existing_usernames(users)
    return allocate_username("s", user_id, set(usernames) | recorded_usernames)


def create_sale_user_with_note(api_client, user_id, plan_gb, days, unlimited):
    try:
        recorded_usernames = load_recorded_usernames()
    except RecordedUsernameLoadError as exc:
        logging.getLogger("ajib.usernames").error(
            "Sale user creation blocked because username history could not be loaded. user_id=%s error=%s",
            user_id,
            exc,
        )
        return None, None, None

    multi_api = MultiServerAPI()

    def allocate(existing_usernames):
        return allocate_username(
            "s",
            user_id,
            set(existing_usernames) | recorded_usernames,
        )

    def create(target_client, username):
        note_payload = build_user_note(
            username=username,
            traffic_limit=plan_gb,
            expiration_days=days,
            unlimited=unlimited,
            note_text="sale",
        )
        result = target_client.add_user(
            username,
            int(plan_gb),
            int(days),
            unlimited=unlimited,
            note=note_payload,
        )
        if result is None:
            result = target_client.add_user(username, int(plan_gb), int(days), unlimited=unlimited)
            if result is not None:
                logging.getLogger("ajib.usernames").warning(
                    "Created sale user without note fallback. user_id=%s username=%s",
                    user_id,
                    username,
                )
        return result

    return multi_api.create_user_with_retry(allocate, create, fallback_client=api_client)


def _record_checkout_started(payment_id, record):
    record_main_growth_event(
        "checkout_started",
        record.get('user_id'),
        language=record.get('language'),
        plan_id=record.get('plan_gb'),
        deduplication_key=f"main:checkout_started:{payment_id}",
        payment_method=record.get('payment_method'),
        referral_campaign=record.get('referral_campaign'),
    )


def _fulfill_credit_funded_purchase(call, plan_gb, plan, quote):
    """Provision a main-store purchase fully funded by reserved AJIB credit."""
    user_id = call.from_user.id
    language = get_user_language(user_id)
    reservation_id = quote['incentive_reservation_id']
    payment_id = f"credit-{reservation_id}"
    record = {
        'user_id': user_id,
        'language': language,
        'plan_gb': plan_gb,
        'days': plan['days'],
        'unlimited': plan.get('unlimited', False),
        'payment_id': payment_id,
        'order_id': reservation_id,
        'status': 'processing',
        'payment_method': 'Account Credit',
        **quote,
    }
    try:
        add_payment_record(payment_id, record)
        _record_checkout_started(payment_id, record)
    except Exception:
        _release_checkout_incentives(user_id, reservation_id)
        raise

    api_client = APIClient()
    username, result, api_client = create_sale_user_with_note(
        api_client,
        user_id,
        plan_gb,
        plan['days'],
        plan.get('unlimited', False),
    )
    if not result:
        update_payment_status(payment_id, 'failed')
        _release_checkout_incentives(user_id, reservation_id)
        safe_answer_callback_query(
            bot,
            call.id,
            get_message_text(language, 'failed_to_create_user'),
            show_alert=True,
        )
        return False
    if not _complete_sale_payment_or_notify(
        payment_id,
        user_id,
        username,
        api_client,
    ):
        return False

    send_admin_payment_notification(
        user_id,
        username,
        plan_gb,
        0,
        payment_id,
        "Account Credit",
        telegram_username=getattr(call.from_user, 'username', None),
        server_name=getattr(api_client, 'server_name', None),
        server_id=getattr(api_client, 'server_id', None),
    )
    uri_data = api_client.get_user_uri(username) if api_client else None
    sub_url = uri_data.get('normal_sub') if uri_data else None
    ipv4_url = uri_data.get('ipv4', '') if uri_data else ''
    ipv4_info = _localized_ipv4_info(language, ipv4_url)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    if sub_url:
        qr = qrcode.make(ipv4_url or sub_url)
        bio = io.BytesIO()
        qr.save(bio, 'PNG')
        bio.seek(0)
        bot.send_photo(
            call.message.chat.id,
            photo=bio,
            caption=get_message_text(language, 'payment_completed').format(
                plan_gb=plan_gb,
                username=username,
                sub_url=sub_url,
                ipv4_info=ipv4_info,
            ),
            parse_mode='Markdown',
        )
        send_download_prompt_safely(bot, call.message.chat.id, language)
    else:
        bot.send_message(
            call.message.chat.id,
            get_message_text(language, 'payment_completed_no_url'),
            parse_mode='Markdown',
        )
    return True


def _fulfill_credit_funded_renewal(call, offer, quote):
    """Execute or reserve a renewal paid entirely with AJIB purchase credit."""
    from utils.renewal import customer_payment_metadata

    user_id = call.from_user.id
    language = get_user_language(user_id)
    reservation_id = quote['incentive_reservation_id']
    payment_id = f"credit-renewal-{reservation_id}"
    record = {
        'user_id': user_id,
        'language': language,
        'plan_gb': offer['plan_gb'],
        'days': offer['days'],
        'unlimited': offer.get('unlimited', False),
        'payment_id': payment_id,
        'order_id': reservation_id,
        'status': 'processing',
        'payment_method': 'Account Credit',
        **customer_payment_metadata(offer),
        **quote,
    }
    try:
        add_payment_record(payment_id, record)
        _record_checkout_started(payment_id, record)
    except Exception:
        _release_checkout_incentives(user_id, reservation_id)
        raise

    success = _process_customer_renewal_payment(
        payment_id,
        record,
        notify_chat_id=call.message.chat.id,
        payment_method='Account Credit',
        telegram_username=getattr(call.from_user, 'username', None),
    )
    if not success:
        latest = get_payment_record(payment_id) or {}
        if latest.get('status') == 'renewal_failed':
            _release_checkout_incentives(user_id, reservation_id)
            update_payment_record_fields(payment_id, {
                'incentives_released_after_failed_renewal': True,
            })
        return False
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    return True

def send_admin_payment_notification(
    user_id,
    username,
    plan_gb,
    price,
    payment_id,
    payment_method,
    telegram_username=None,
    converted_amount=None,
    converted_currency=None,
    exchange_rate=None,
    server_name=None,
    server_id=None,
):
    """Send a notification to all admins about a successful payment"""
    try:
        for admin_id in ADMIN_USER_IDS:
            admin_language = get_user_language(admin_id)
            notification_message = (
                f"💰 <b>{get_message_text(admin_language, 'payment_notification_title')}</b>\n\n"
                f"✅ <b>{get_message_text(admin_language, 'successful_payment_received')}</b>\n\n"
                f"👤 <b>{get_message_text(admin_language, 'user_id')}:</b> <code>{user_id}</code>\n"
            )
            
            if telegram_username:
                 notification_message += f"📱 <b>Telegram Username:</b> @{telegram_username}\n"
            
            notification_message += (
                f"📱 <b>{get_message_text(admin_language, 'username')}:</b> <code>{username}</code>\n"
            )
            normalized_server_name = str(server_name or '').strip()
            normalized_server_id = str(server_id or '').strip()
            if normalized_server_name or normalized_server_id:
                safe_server_name = html.escape(normalized_server_name)
                safe_server_id = html.escape(normalized_server_id)
                if normalized_server_name and normalized_server_id and normalized_server_name != normalized_server_id:
                    server_display = f"{safe_server_name} (<code>{safe_server_id}</code>)"
                elif normalized_server_id:
                    server_display = f"<code>{safe_server_id}</code>"
                else:
                    server_display = safe_server_name
                notification_message += (
                    f"🌐 <b>{get_message_text(admin_language, 'server')}:</b> {server_display}\n"
                )
            notification_message += (
                f"📊 <b>{get_message_text(admin_language, 'plan_size')}:</b> {plan_gb} GB\n"
                f"💵 <b>{get_message_text(admin_language, 'amount')}:</b> ${format_usd_amount(price)}\n"
            )
            if converted_amount is not None:
                currency_label = converted_currency or "Tomans"
                notification_message += f"💱 <b>Converted Amount:</b> {format_toman_amount(converted_amount)} {currency_label}\n"

            notification_message += (
                f"💳 <b>{get_message_text(admin_language, 'payment_method_label')}:</b> {payment_method}\n"
                f"🔑 <b>{get_message_text(admin_language, 'payment_id_label')}:</b> <code>{payment_id}</code>\n"
                f"📅 <b>{get_message_text(admin_language, 'timestamp')}:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            try:
                bot.send_message(
                    admin_id,
                    notification_message,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Failed to send notification to admin {admin_id}: {str(e)}")
    except Exception as e:
        print(f"Error in send_admin_payment_notification: {str(e)}")

def show_plans(chat_id, user_id, message_id=None, show_all=False):
    language = get_user_language(user_id)
    record_main_growth_event(
        "plan_viewed",
        user_id,
        language=language,
        deduplication_key=f"main:plan_viewed:{user_id}:{'all' if show_all else 'quick'}",
        catalog="all" if show_all else "quick",
    )
    plans = load_plans()
    exchange_rate = get_exchange_rate()
    customer_plans = _customer_plan_items(plans)
    markup = types.InlineKeyboardMarkup(row_width=1)
    visible_plans = (
        [(None, gb, details) for gb, details in customer_plans]
        if show_all
        else select_quick_pick_plans(plans)
    )
    for label_key, gb, details in visible_plans:
        button_text = _plan_button_text(
            language,
            gb,
            details,
            exchange_rate,
            label_key=label_key,
        )
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"purchase:{gb}"))

    if not show_all and len(customer_plans) > len(visible_plans):
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_all_plans"),
            callback_data="show_all_plans",
        ))

    text = get_message_text(
        language,
        "all_plans_title" if show_all else "quick_plans_title",
    )
    
    if message_id:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )
    else:
        bot.send_message(
            chat_id,
            text,
            reply_markup=markup,
            parse_mode="Markdown",
        )

@bot.message_handler(func=lambda message: any(
    message.text == get_button_text(get_user_language(message.from_user.id), "purchase_plan") for lang in BUTTON_TRANSLATIONS
))
def purchase_plan(message):
    show_plans(message.chat.id, message.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_plans")
def back_to_plans(call):
    try:
        safe_answer_callback_query(bot, call.id)
        show_plans(call.message.chat.id, call.from_user.id, call.message.message_id)
    except Exception as e:
        print(f"Error in back_to_plans: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "show_all_plans")
def handle_show_all_plans(call):
    try:
        safe_answer_callback_query(bot, call.id)
        show_plans(
            call.message.chat.id,
            call.from_user.id,
            call.message.message_id,
            show_all=True,
        )
    except Exception as e:
        print(f"Error showing all plans: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('purchase:'))
def handle_purchase_selection(call):
    try:
        safe_answer_callback_query(bot, call.id)
        user_id = call.from_user.id
        language = get_user_language(user_id)
        plan_gb = call.data.split(':')[1]
        plans = load_plans()
        if plan_gb in plans:
            plan = plans[plan_gb]
            if plan.get('target', 'both') == 'reseller':
                safe_answer_callback_query(
                    bot,
                    call.id,
                    text=get_message_text(language, 'customer_reseller_only_plan'),
                )
                return
            record_main_growth_event(
                "plan_selected",
                user_id,
                language=language,
                plan_id=plan_gb,
                deduplication_key=f"main:plan_selected:{user_id}:{plan_gb}",
            )
            unlimited_text = get_button_text(language, "yes" if plan.get("unlimited") else "no")
            price = float(plan['price'])
            exchange_rate = get_exchange_rate()
            invite_discount_percent = _invite_discount_preview(user_id)
            load_dotenv(TELEGRAM_ENV_PATH, override=True)
            crypto_configured = all(os.getenv(key) for key in ['CRYPTO_MERCHANT_ID', 'CRYPTO_API_KEY'])
            card_to_card_configured = get_card_number_for_receipt_type(RECEIPT_TYPE_REGULAR)
            message = get_message_text(language, "purchase_progress_payment") + "\n\n"
            message += get_message_text(language, "plan_details")
            message += get_message_text(language, "data").format(plan_gb=plan_gb)
            message += get_message_text(language, "duration").format(days=plan['days'])
            message += get_message_text(language, "unlimited").format(unlimited_text=unlimited_text)
            message += build_plan_payment_totals(
                language,
                plan_gb,
                price,
                exchange_rate,
                invite_discount_percent=invite_discount_percent,
            )
            if invite_discount_percent > 0:
                message += "\n" + _format_checkout_incentives(language, {
                    'invite_discount_percent': invite_discount_percent,
                    'invite_discount_amount': round(
                        price * invite_discount_percent / 100,
                        2,
                    ),
                    'account_credit_reserved': 0,
                })

            # Check configured payment methods
            
            # Always show card-to-card if configured
            show_card_to_card = bool(card_to_card_configured)
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            methods_count = 0
            if crypto_configured:
                markup.add(types.InlineKeyboardButton(get_crypto_discount_button_text(language), callback_data=f"payment_method:crypto:{plan_gb}"))
                methods_count += 1
            if show_card_to_card:
                markup.add(types.InlineKeyboardButton(get_button_text(language, "card_to_card"), callback_data=f"payment_method:card_to_card:{plan_gb}"))
                methods_count += 1
            
            if methods_count == 0:
                 safe_answer_callback_query(bot, call.id, text=get_message_text(language, "no_payment_methods"))
                 return

            message += "\n\n" + get_message_text(language, "purchase_delivery_note")
            if _consume_purchase_disclosure(user_id):
                message += get_message_text(language, "purchase_connection_warning")
            message += get_message_text(language, "select_payment_method")

            markup.add(types.InlineKeyboardButton(
                get_button_text(language, "support"),
                callback_data="purchase_support",
            ))
            markup.add(types.InlineKeyboardButton(get_button_text(language, "back"), callback_data="back_to_plans"))

            bot.edit_message_text(
                message,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
                parse_mode="Markdown",
            )
        else:
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "plan_not_found"))
    except Exception as e:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))


@bot.callback_query_handler(func=lambda call: call.data == "purchase_support")
def handle_purchase_support(call):
    language = get_user_language(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    try:
        from utils.edit_support import get_support_text

        support_text = get_support_text()
    except Exception:
        support_text = get_message_text(language, "support_unavailable")
    bot.send_message(
        call.message.chat.id,
        get_message_text(language, "purchase_support_intro") + "\n\n" + support_text,
        parse_mode="Markdown",
    )

@bot.callback_query_handler(func=lambda call: call.data == "cancel_purchase")
def handle_cancel_purchase(call):
    user_id = call.from_user.id
    language = get_user_language(user_id)
    safe_answer_callback_query(bot, call.id)
    bot.delete_message(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )
    # New: Clear user state if it exists (prevents lingering receipt waiting mode)
    if user_id in user_data:
        checkout_id = user_data[user_id].get('card_checkout_id')
        _close_card_checkout(checkout_id, 'canceled')
        _release_checkout_incentives(user_id, checkout_id)
        del user_data[user_id]
    bot.send_message(
        chat_id=call.message.chat.id,
        text=get_message_text(language, "purchase_canceled")
    )


def _resolve_customer_renewal_offer_for_call(call, token):
    from utils.renewal import resolve_customer_renewal_token

    return resolve_customer_renewal_token(call.from_user.id, token, load_plans())


@bot.callback_query_handler(func=lambda call: call.data.startswith('renew_plan:'))
def handle_customer_renewal_start(call):
    try:
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        language = get_user_language(user_id)
        token = call.data.split(':', 1)[1]
        offer = _resolve_customer_renewal_offer_for_call(call, token)
        if not offer.get('eligible'):
            bot.edit_message_text(
                get_message_text(language, "renewal_unavailable").format(
                    reason=_renewal_reason_text(language, offer.get('reason'))
                ),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="Markdown"
            )
            return

        load_dotenv(TELEGRAM_ENV_PATH, override=True)
        crypto_configured = all(os.getenv(key) for key in ['CRYPTO_MERCHANT_ID', 'CRYPTO_API_KEY'])
        card_to_card_configured = get_card_number_for_receipt_type(RECEIPT_TYPE_REGULAR)
        markup = types.InlineKeyboardMarkup(row_width=1)
        methods_count = 0
        if crypto_configured:
            markup.add(types.InlineKeyboardButton(get_crypto_discount_button_text(language), callback_data=f"renew_payment_method:crypto:{token}"))
            methods_count += 1
        if card_to_card_configured:
            markup.add(types.InlineKeyboardButton(get_button_text(language, "card_to_card"), callback_data=f"renew_payment_method:card_to_card:{token}"))
            methods_count += 1
        if methods_count == 0:
            bot.edit_message_text(
                get_message_text(language, "no_payment_methods"),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
            return
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "support"),
            callback_data="purchase_support",
        ))
        markup.add(types.InlineKeyboardButton(get_button_text(language, "cancel"), callback_data="cancel_purchase"))

        from utils.renewal import format_renewal_offer
        exchange_rate = get_exchange_rate()
        offer_message = get_message_text(language, "purchase_progress_payment") + "\n\n"
        offer_message += format_renewal_offer(language, offer, include_payment_prompt=True)
        offer_message += "\n\n" + build_plan_payment_totals(
            language,
            offer['plan_gb'],
            offer['price'],
            exchange_rate,
        )
        offer_message += "\n\n" + get_message_text(language, "purchase_delivery_note")
        bot.edit_message_text(
            offer_message,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        language = get_user_language(call.from_user.id)
        bot.answer_callback_query(call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))


@bot.callback_query_handler(func=lambda call: call.data.startswith('renew_payment_method:'))
def handle_customer_renewal_payment_method(call):
    try:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        _, method, token = call.data.split(':', 2)
        offer = _resolve_customer_renewal_offer_for_call(call, token)
        if not offer.get('eligible'):
            bot.answer_callback_query(
                call.id,
                text=get_message_text(language, "renewal_unavailable").format(
                    reason=_renewal_reason_text(language, offer.get('reason'))
                ),
                show_alert=True
            )
            return
        if method == 'crypto':
            _handle_customer_renewal_crypto(call, offer)
        elif method == 'card_to_card':
            _handle_customer_renewal_card_to_card(call, offer)
        else:
            bot.answer_callback_query(call.id, text=get_message_text(language, "invalid_payment_method"))
    except Exception as e:
        language = get_user_language(call.from_user.id)
        bot.answer_callback_query(call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))


def _handle_customer_renewal_crypto(call, offer):
    from utils.renewal import customer_payment_metadata

    user_id = call.from_user.id
    language = get_user_language(user_id)
    incentive_reservation_id = uuid.uuid4().hex
    discount_metadata = _reserve_checkout_incentives(
        user_id,
        incentive_reservation_id,
        offer['price'],
        'crypto',
    )
    discounted_price = discount_metadata['price']
    safe_answer_callback_query(bot, call.id)
    if discount_metadata.get('fully_credit_funded'):
        _fulfill_credit_funded_renewal(call, offer, discount_metadata)
        return
    payment_handler = CryptoPayment()
    try:
        payment_response = payment_handler.create_payment(discounted_price, offer['plan_gb'], user_id)
    except Exception:
        _release_checkout_incentives(user_id, incentive_reservation_id)
        raise
    if "error" in payment_response:
        _release_checkout_incentives(user_id, incentive_reservation_id)
        bot.edit_message_text(
            get_message_text(language, "error_creating_payment").format(error=payment_response['error']),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return
    payment_data = payment_response.get('result', {})
    payment_id = payment_data.get('uuid')
    payment_url = payment_data.get('url')
    gateway_order_id = payment_data.get('order_id')
    if not payment_id or not payment_url:
        _release_checkout_incentives(user_id, incentive_reservation_id)
        bot.edit_message_text(
            get_message_text(language, "invalid_payment_response"),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return

    payment_record = {
        'user_id': user_id,
        'language': language,
        'plan_gb': offer['plan_gb'],
        'days': offer['days'],
        'unlimited': offer.get('unlimited', False),
        'payment_id': payment_id,
        'order_id': gateway_order_id,
        'payment_url': payment_url,
        'status': 'pending',
        'payment_method': 'Crypto',
        **customer_payment_metadata(offer),
        **discount_metadata,
    }
    try:
        add_payment_record(payment_id, payment_record)
    except Exception:
        _release_checkout_incentives(user_id, incentive_reservation_id)
        raise
    _record_checkout_started(payment_id, payment_record)

    qr = qrcode.make(payment_url)
    bio = io.BytesIO()
    qr.save(bio, 'PNG')
    bio.seek(0)
    payment_message = get_message_text(language, "purchase_progress_payment") + "\n\n"
    payment_message += get_message_text(language, "crypto_checkout_summary").format(
        plan_gb=offer['plan_gb'],
        final_amount=format_usd_amount(discounted_price),
    ) + "\n\n"
    payment_message += get_message_text(language, "payment_instructions").format(
        price=format_usd_amount(discounted_price),
        payment_url=payment_url,
        payment_id=payment_id,
    )
    if float(discount_metadata.get('payment_discount_percent', 0) or 0) > 0:
        payment_message += "\n\n" + build_crypto_discount_display(language, discount_metadata)['summary']
    incentive_summary = _format_checkout_incentives(language, discount_metadata)
    if incentive_summary:
        payment_message += "\n\n" + incentive_summary
    payment_message += "\n\n" + get_message_text(language, "renewal_quota_reset_warning")
    payment_message += "\n\n" + get_message_text(language, "purchase_delivery_note")
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(get_button_text(language, "payment_link"), url=payment_url),
        types.InlineKeyboardButton(get_button_text(language, "check_status"), callback_data=f"check_payment:{payment_id}"),
        types.InlineKeyboardButton(get_button_text(language, "support"), callback_data="purchase_support"),
    )
    bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.send_photo(
        call.message.chat.id,
        photo=bio,
        caption=payment_message,
        reply_markup=markup,
        parse_mode="Markdown"
    )


def _handle_customer_renewal_card_to_card(call, offer):
    from utils.renewal import customer_payment_metadata

    user_id = call.from_user.id
    language = get_user_language(user_id)
    load_dotenv(TELEGRAM_ENV_PATH, override=True)
    card_number = get_card_number_for_receipt_type(RECEIPT_TYPE_REGULAR)
    exchange_rate = get_exchange_rate()
    if not card_number:
        bot.edit_message_text(
            get_message_text(language, "card_to_card_not_configured"),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return
    incentive_reservation_id = uuid.uuid4().hex
    checkout_persisted = False
    try:
        quote = _reserve_checkout_incentives(
            user_id,
            incentive_reservation_id,
            offer['price'],
            'card',
        )
        price = quote['price']
        if quote.get('fully_credit_funded'):
            safe_answer_callback_query(bot, call.id)
            _fulfill_credit_funded_renewal(call, offer, quote)
            return
        price_in_tomans = float(price) * exchange_rate
        message = get_message_text(language, "purchase_progress_payment") + "\n\n"
        message += get_message_text(language, "card_checkout_summary").format(
            plan_gb=offer['plan_gb'],
            final_amount=format_toman_amount(price_in_tomans),
        ) + "\n\n"
        message += get_message_text(language, "card_to_card_payment").format(
            price=format_toman_amount(price_in_tomans),
            exchange_rate=format_toman_amount(exchange_rate),
            card_number=card_number
        )
        incentive_summary = _format_checkout_incentives(language, quote)
        if incentive_summary:
            message += "\n\n" + incentive_summary
        message += "\n\n" + get_message_text(language, "renewal_quota_reset_warning")
        message += "\n\n" + get_message_text(language, "purchase_delivery_note")
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(get_button_text(language, "support"), callback_data="purchase_support"),
            types.InlineKeyboardButton(get_button_text(language, "cancel"), callback_data="cancel_purchase"),
        )
        card_checkout_id = _register_card_checkout(
            user_id,
            call.message.chat.id,
            offer['plan_gb'],
            price_in_tomans,
            call.data,
            checkout_id=incentive_reservation_id,
        )
        user_data[user_id] = {
            'state': 'waiting_receipt',
            'plan_gb': offer['plan_gb'],
            'price': price,
            'converted_amount': price_in_tomans,
            'converted_currency': 'Tomans',
            'exchange_rate': exchange_rate,
            'receipt_type': RECEIPT_TYPE_REGULAR,
            'renewal_metadata': customer_payment_metadata(offer),
            'receipt_prompt_message_id': call.message.message_id,
            'card_checkout_id': card_checkout_id,
            'incentive_metadata': dict(quote),
            'language': language,
        }
        checkout_persisted = True
        bot.edit_message_text(
            message,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception:
        if not checkout_persisted:
            _release_checkout_incentives(user_id, incentive_reservation_id)
        raise


@bot.callback_query_handler(func=lambda call: call.data.startswith('payment_method:'))
def handle_payment_method_selection(call, data=None):
    try:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        callback_data = data if data else call.data
        _, method, plan_gb = callback_data.split(':')
        if method == 'crypto':
            queued = _queue_customer_crypto_payment(call, plan_gb)
            safe_answer_callback_query(bot, call.id)
            if not queued:
                logging.getLogger('ajib.payments').info(
                    "Skipped duplicate crypto payment job for user %s plan %s",
                    user_id,
                    plan_gb,
                )
            return
        elif method == 'card_to_card':
            load_dotenv(TELEGRAM_ENV_PATH, override=True)
            card_to_card_mode = os.getenv('CARD_TO_CARD_MODE', 'on')
            if card_to_card_mode == 'previous_customers':
                try:
                    user_payments = get_user_payments(user_id)
                    has_completed = any(
                        p.get('status') == 'completed' for p in user_payments.values()
                    )
                    if not has_completed:
                        bot.answer_callback_query(call.id, text=get_message_text(language, "card_to_card_second_purchase"), show_alert=False)
                        return
                except Exception as e:
                    logging.getLogger('ajib.payments').warning(
                        f"Failed to determine previous customer status for user {user_id}: {e}"
                    )
            handle_card_to_card_payment(call, plan_gb)
        else:
            bot.answer_callback_query(call.id, text=get_message_text(language, "invalid_payment_method"))
    except Exception as e:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        bot.answer_callback_query(call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))

def handle_crypto_payment(call, plan_gb, answer_callback=True):
    incentive_reservation_id = None
    payment_persisted = False
    try:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        plans = load_plans()
        if plan_gb in plans:
            plan = plans[plan_gb]
            if plan.get('target', 'both') == 'reseller':
                bot.answer_callback_query(
                    call.id,
                    text=get_message_text(language, 'customer_reseller_only_plan'),
                )
                return
            incentive_reservation_id = uuid.uuid4().hex
            discount_metadata = _reserve_checkout_incentives(
                user_id,
                incentive_reservation_id,
                plan['price'],
                'crypto',
            )
            discounted_price = discount_metadata['price']
            if answer_callback:
                safe_answer_callback_query(bot, call.id)
            if discount_metadata.get('fully_credit_funded'):
                _fulfill_credit_funded_purchase(
                    call,
                    plan_gb,
                    plan,
                    discount_metadata,
                )
                return
            payment_handler = CryptoPayment()
            payment_response = payment_handler.create_payment(
                discounted_price, plan_gb, user_id
            )
            if "error" in payment_response:
                _release_checkout_incentives(user_id, incentive_reservation_id)
                bot.edit_message_text(
                    get_message_text(language, "error_creating_payment").format(error=payment_response['error']),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return
            payment_data = payment_response.get('result', {})
            payment_id = payment_data.get('uuid')
            payment_url = payment_data.get('url')
            gateway_order_id = payment_data.get('order_id')
            if not payment_id or not payment_url:
                _release_checkout_incentives(user_id, incentive_reservation_id)
                bot.edit_message_text(
                    get_message_text(language, "invalid_payment_response"),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return
            payment_record = {
                'user_id': user_id,
                'language': language,
                'plan_gb': plan_gb,
                'days': plan['days'],
                'unlimited': plan.get('unlimited', False),
                'payment_id': payment_id,
                'order_id': gateway_order_id,
                'payment_url': payment_url,
                'status': 'pending',
                'payment_method': 'Crypto',
                **discount_metadata,
            }
            try:
                add_payment_record(payment_id, payment_record)
            except Exception:
                _release_checkout_incentives(user_id, incentive_reservation_id)
                raise
            payment_persisted = True
            _record_checkout_started(payment_id, payment_record)
            qr = qrcode.make(payment_url)
            bio = io.BytesIO()
            qr.save(bio, 'PNG')
            bio.seek(0)
            payment_message = get_message_text(language, "purchase_progress_payment") + "\n\n"
            payment_message += get_message_text(language, "crypto_checkout_summary").format(
                plan_gb=plan_gb,
                final_amount=format_usd_amount(discounted_price),
            ) + "\n\n"
            payment_message += get_message_text(language, "payment_instructions").format(price=format_usd_amount(discounted_price), payment_url=payment_url, payment_id=payment_id)
            if float(discount_metadata.get('payment_discount_percent', 0) or 0) > 0:
                payment_message += "\n\n" + build_crypto_discount_display(language, discount_metadata)['summary']
            incentive_summary = _format_checkout_incentives(language, discount_metadata)
            if incentive_summary:
                payment_message += "\n\n" + incentive_summary
            payment_message += "\n\n" + get_message_text(language, "purchase_delivery_note")
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(get_button_text(language, "payment_link"), url=payment_url),
                types.InlineKeyboardButton(get_button_text(language, "check_status"), callback_data=f"check_payment:{payment_id}"),
                types.InlineKeyboardButton(get_button_text(language, "support"), callback_data="purchase_support"),
            )
            bot.delete_message(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            bot.send_photo(
                call.message.chat.id,
                photo=bio,
                caption=payment_message,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            bot.answer_callback_query(call.id, text=get_message_text(language, "plan_not_found"))
    except Exception as e:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        if incentive_reservation_id and not payment_persisted:
            _release_checkout_incentives(user_id, incentive_reservation_id)
        bot.answer_callback_query(call.id, text=get_message_text(language, "error_processing_payment").format(error=str(e)))

def handle_card_to_card_payment(call, plan_gb):
    incentive_reservation_id = None
    checkout_persisted = False
    try:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        load_dotenv(TELEGRAM_ENV_PATH, override=True)
        receipt_type = RECEIPT_TYPE_REGULAR
        card_number = get_card_number_for_receipt_type(receipt_type)
        exchange_rate = get_exchange_rate()
        if not card_number:
            bot.edit_message_text(
                get_message_text(language, "card_to_card_not_configured"),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return
        plans = load_plans()
        plan = plans[plan_gb]
        incentive_reservation_id = uuid.uuid4().hex
        quote = _reserve_checkout_incentives(
            user_id,
            incentive_reservation_id,
            plan['price'],
            'card',
        )
        price = quote['price']
        if quote.get('fully_credit_funded'):
            safe_answer_callback_query(bot, call.id)
            _fulfill_credit_funded_purchase(call, plan_gb, plan, quote)
            return
        # Convert price to tomans using the exchange rate
        price_in_tomans = float(price) * exchange_rate
        message = get_message_text(language, "purchase_progress_payment") + "\n\n"
        message += get_message_text(language, "card_checkout_summary").format(
            plan_gb=plan_gb,
            final_amount=format_toman_amount(price_in_tomans),
        ) + "\n\n"
        message += get_message_text(language, "card_to_card_payment").format(
            price=format_toman_amount(price_in_tomans),
            exchange_rate=format_toman_amount(exchange_rate),
            card_number=card_number
        )
        incentive_summary = _format_checkout_incentives(language, quote)
        if incentive_summary:
            message += "\n\n" + incentive_summary
        message += "\n\n" + get_message_text(language, "purchase_delivery_note")
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(get_button_text(language, "support"), callback_data="purchase_support"),
            types.InlineKeyboardButton(get_button_text(language, "cancel"), callback_data="cancel_purchase"),
        )
        card_checkout_id = _register_card_checkout(
            user_id,
            call.message.chat.id,
            plan_gb,
            price_in_tomans,
            f"payment_method:card_to_card:{plan_gb}",
            checkout_id=incentive_reservation_id,
        )
        user_data[user_id] = {
            'state': 'waiting_receipt',
            'plan_gb': plan_gb,
            'price': price,
            'converted_amount': price_in_tomans,
            'converted_currency': 'Tomans',
            'exchange_rate': exchange_rate,
            'receipt_type': receipt_type,
            'receipt_prompt_message_id': call.message.message_id,
            'card_checkout_id': card_checkout_id,
            'incentive_metadata': dict(quote),
            'language': language,
        }
        checkout_persisted = True
        bot.edit_message_text(
            message,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception as e:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        if incentive_reservation_id and not checkout_persisted:
            _release_checkout_incentives(user_id, incentive_reservation_id)
        bot.answer_callback_query(call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))

# Modified: Remove photo check and re-registration; assume called only on photos
def process_receipt_photo(message, plan_gb, price):
    try:
        user_id = message.from_user.id
        language = get_user_language(user_id)
        receipt_prompt_message_id = None
        converted_amount = None
        converted_currency = None
        exchange_rate = None
        renewal_metadata = None
        incentive_metadata = None
        card_checkout_id = None
        receipt_type = RECEIPT_TYPE_SETTLEMENT if plan_gb == 'Settlement' else RECEIPT_TYPE_REGULAR
        if user_id in user_data:
            receipt_prompt_message_id = user_data[user_id].get('receipt_prompt_message_id')
            converted_amount = user_data[user_id].get('converted_amount')
            converted_currency = user_data[user_id].get('converted_currency')
            exchange_rate = user_data[user_id].get('exchange_rate')
            receipt_type = user_data[user_id].get('receipt_type', receipt_type)
            settlement_amount = user_data[user_id].get('settlement_amount')
            renewal_metadata = user_data[user_id].get('renewal_metadata')
            incentive_metadata = user_data[user_id].get('incentive_metadata')
            card_checkout_id = user_data[user_id].get('card_checkout_id')
        else:
            settlement_amount = None
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        payment_id = str(uuid.uuid4())
        uploads_dir = 'uploads'
        if not os.path.exists(uploads_dir):
            os.makedirs(uploads_dir)
        photo_path = os.path.join(uploads_dir, f"{payment_id}.jpg")
        with open(photo_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        if plan_gb == 'Settlement':
             routed_to_checker = should_route_to_receipt_checker(RECEIPT_TYPE_SETTLEMENT)
             checker_id = get_receipt_checker_user_id() if routed_to_checker else None
             payment_record = {
                'user_id': user_id,
                'language': language,
                'plan_gb': plan_gb,
                'price': price,
                'days': 0,
                'payment_id': payment_id,
                'status': 'pending_approval',
                'receipt_path': photo_path,
                'type': 'settlement',
                'receipt_type': RECEIPT_TYPE_SETTLEMENT,
                'routed_to_checker': routed_to_checker,
                'receipt_checker_user_id': checker_id,
                'payment_method': 'Card to Card',
                'settlement_amount': settlement_amount if settlement_amount is not None else price,
            }
        else:
            plans = load_plans()
            plan = plans[plan_gb]
            routed_to_checker = should_route_to_receipt_checker(RECEIPT_TYPE_REGULAR)
            checker_id = get_receipt_checker_user_id() if routed_to_checker else None
            payment_record = {
                'user_id': user_id,
                'language': language,
                'plan_gb': plan_gb,
                'price': price,
                'days': plan['days'],
                'unlimited': plan.get('unlimited', False),
                'payment_id': payment_id,
                'status': 'pending_approval',
                'receipt_path': photo_path,
                'receipt_type': RECEIPT_TYPE_REGULAR,
                'routed_to_checker': routed_to_checker,
                'receipt_checker_user_id': checker_id,
                'payment_method': 'Card to Card'
            }
            if renewal_metadata:
                payment_record.update(renewal_metadata)
            if incentive_metadata:
                payment_record.update(incentive_metadata)
        if converted_amount is not None:
            payment_record['converted_amount'] = converted_amount
            payment_record['converted_currency'] = converted_currency or 'Tomans'
            payment_record['exchange_rate'] = exchange_rate
            
        add_payment_record(payment_id, payment_record)
        _record_checkout_started(payment_id, payment_record)
        _close_card_checkout(card_checkout_id, 'submitted')
        notification_message = _format_pending_receipt_caption(payment_id, payment_record, message.from_user.username)
        receipt_message_refs = []
        for admin_id in ADMIN_USER_IDS:
            try:
                sent_message = _send_receipt_confirmation(admin_id, payment_id, payment_record, notification_message)
                receipt_message_refs.append({
                    "chat_id": sent_message.chat.id,
                    "message_id": sent_message.message_id,
                    "recipient_id": admin_id,
                    "recipient_role": "admin",
                    "content_type": "photo" if getattr(sent_message, "photo", None) else "text",
                })
            except Exception as e:
                print(f"Failed to send notification to admin {admin_id}: {str(e)}")
        if checker_id and checker_id not in ADMIN_USER_IDS:
            try:
                sent_message = _send_receipt_confirmation(checker_id, payment_id, payment_record, notification_message)
                receipt_message_refs.append({
                    "chat_id": sent_message.chat.id,
                    "message_id": sent_message.message_id,
                    "recipient_id": checker_id,
                    "recipient_role": "checker",
                    "content_type": "photo" if getattr(sent_message, "photo", None) else "text",
                })
            except Exception as e:
                print(f"Failed to send notification to receipt checker {checker_id}: {str(e)}")
        _save_receipt_message_refs(payment_id, receipt_message_refs)
        if receipt_prompt_message_id:
            try:
                bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=receipt_prompt_message_id,
                    reply_markup=None
                )
            except Exception:
                pass
        bot.reply_to(message, get_message_text(language, "receipt_submitted"))
        # New: Clear state after processing
        if user_id in user_data:
            del user_data[user_id]
    except Exception as e:
        user_id = message.from_user.id
        language = get_user_language(user_id)
        bot.reply_to(message, get_message_text(language, "error_occurred").format(error=str(e)))

# New: State-aware handler for photos
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    if user_id in user_data and user_data[user_id]['state'] == 'waiting_receipt':
        plan_gb = user_data[user_id]['plan_gb']
        price = user_data[user_id]['price']
        _queue_payment_job(("receipt_photo", user_id), process_receipt_photo, message, plan_gb, price)
    # Optional: Handle non-state photos if needed (e.g., ignore or reply)

# New: Handler for text messages while waiting for receipt (reminds without looping)
@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['state'] == 'waiting_receipt')
def handle_text_while_waiting(message):
    language = get_user_language(message.from_user.id)
    cancel_callback = user_data[message.from_user.id].get('cancel_callback', 'cancel_purchase')
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(get_button_text(language, "cancel"), callback_data=cancel_callback))
    bot.reply_to(message, get_message_text(language, "upload_receipt"), reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == '✅ Confirmations' and _is_confirmation_viewer(message.from_user.id))
def show_pending_confirmations(message):
    user_id = message.from_user.id
    payments = load_payments()
    pending_items = []
    user_is_admin = is_admin(user_id)
    for payment_id, record in payments.items():
        if record.get('status') != 'pending_approval':
            continue
        if not can_review_receipt(user_id, record, is_admin_user=user_is_admin):
            continue
        pending_items.append((payment_id, record))
    pending_withdrawals = get_pending_withdrawal_requests() if user_is_admin else []

    if not pending_items and not pending_withdrawals:
        if not user_is_admin and is_receipt_checker(user_id):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("📊 My Stats", callback_data="checker_stats:my"))
            bot.reply_to(message, "No pending receipt confirmations.", reply_markup=markup)
        else:
            bot.reply_to(message, "No pending confirmations.", reply_markup=create_main_markup(is_admin=user_is_admin, user_id=user_id))
        return

    total_pending = len(pending_items) + len(pending_withdrawals)
    bot.reply_to(message, f"Pending confirmations: {total_pending}")
    for payment_id, record in pending_items:
        try:
            sent_message = _send_receipt_confirmation(message.chat.id, payment_id, record)
            refs = list(record.get('receipt_message_refs') or [])
            refs.append({
                "chat_id": sent_message.chat.id,
                "message_id": sent_message.message_id,
                "recipient_id": user_id,
                "recipient_role": "admin" if user_is_admin else "checker",
                "content_type": "photo" if getattr(sent_message, "photo", None) else "text",
            })
            _save_receipt_message_refs(payment_id, refs)
        except Exception as e:
            bot.send_message(message.chat.id, f"Failed to show receipt {payment_id}: {str(e)}")
    for withdrawal_request in pending_withdrawals:
        try:
            _send_referral_withdrawal_confirmation(message.chat.id, withdrawal_request)
        except Exception as e:
            bot.send_message(message.chat.id, f"Failed to show withdrawal {withdrawal_request.get('id')}: {str(e)}")

def _process_admin_approval_job(call, action, payment_id, payment_record, reviewer_role, language):
    try:
        if action == 'approve':
            _record_review_audit(payment_id, call, action, reviewer_role)
            _record_checker_share_audit(payment_id, payment_record)
            if payment_record.get('type') == 'settlement' or payment_record.get('plan_gb') == 'Settlement':
                 success, credited_amount, remaining_debt = _apply_reseller_settlement_payment(
                    payment_record['user_id'],
                    payment_record,
                 )
                 if not success:
                     _release_processing_for_retry(payment_id, 'pending_approval', "settlement credit failed")
                     safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_processing_payment").format(error="settlement credit failed"))
                     return
                 update_payment_status(payment_id, 'completed')
                 
                 user_to_notify = payment_record['user_id']
                 user_language = get_user_language(user_to_notify)
                 
                 telegram_username = None
                 try:
                    chat = bot.get_chat(user_to_notify)
                    telegram_username = chat.username
                 except:
                    pass

                 send_admin_payment_notification(
                    user_to_notify, 
                    "Settlement", 
                    "Settlement", 
                    payment_record['price'], 
                    payment_id, 
                    payment_record.get('payment_method', 'Card to Card'), 
                    telegram_username=telegram_username,
                    converted_amount=payment_record.get('converted_amount'),
                    converted_currency=payment_record.get('converted_currency'),
                    exchange_rate=payment_record.get('exchange_rate')
                 )

                 bot.send_message(
                    user_to_notify,
                    _settlement_approved_message(user_language, user_to_notify, credited_amount, remaining_debt),
                 )
                 _update_receipt_message_refs(
                    payment_id,
                    payment_record,
                    f"✅ Settlement Payment {payment_id} approved by {call.from_user.first_name}."
                )
                 return

            if payment_record.get('type') == 'renewal':
                user_to_notify = payment_record['user_id']
                telegram_username = None
                try:
                    chat = bot.get_chat(user_to_notify)
                    telegram_username = chat.username
                except:
                    pass
                success = _process_customer_renewal_payment(
                    payment_id,
                    payment_record,
                    notify_chat_id=user_to_notify,
                    payment_method=payment_record.get('payment_method', 'Card to Card'),
                    telegram_username=telegram_username,
                )
                if success:
                    _update_receipt_message_refs(
                        payment_id,
                        payment_record,
                        f"✅ Renewal Payment {payment_id} approved by {call.from_user.first_name}."
                    )
                else:
                    _update_receipt_message_refs(
                        payment_id,
                        payment_record,
                        f"⚠️ Renewal Payment {payment_id} was approved but renewal failed."
                    )
                return

            user_to_notify = payment_record['user_id']
            user_language = get_user_language(user_to_notify)
            plan_gb = payment_record['plan_gb']
            days = payment_record['days']
            
            unlimited = payment_record.get('unlimited')
            if unlimited is None:
                 plans = load_plans()
                 if plan_gb in plans:
                     unlimited = plans[plan_gb].get('unlimited', False)
                 else:
                     unlimited = False
            
            api_client = APIClient()
            username, result, api_client = create_sale_user_with_note(
                api_client,
                user_to_notify,
                plan_gb,
                days,
                unlimited,
            )
            if result:
                if not _complete_sale_payment_or_notify(payment_id, user_to_notify, username, api_client):
                    safe_answer_callback_query(
                        bot,
                        call.id,
                        text=get_message_text(language, "error_processing_payment").format(error="payment record update failed")
                    )
                    return
                add_referral_reward(
                    payment_record['user_id'],
                    payment_record['price'],
                    payment_id,
                )
                
                telegram_username = None
                try:
                    chat = bot.get_chat(user_to_notify)
                    telegram_username = chat.username
                except:
                    pass
                    
                send_admin_payment_notification(
                    user_to_notify, 
                    username, 
                    plan_gb, 
                    payment_record['price'], 
                    payment_id, 
                    payment_record.get('payment_method', 'Card to Card'), 
                    telegram_username=telegram_username,
                    converted_amount=payment_record.get('converted_amount'),
                    converted_currency=payment_record.get('converted_currency'),
                    exchange_rate=payment_record.get('exchange_rate'),
                    server_name=getattr(api_client, 'server_name', None),
                    server_id=getattr(api_client, 'server_id', None),
                )

                user_uri_data = api_client.get_user_uri(username)
                if user_uri_data and 'normal_sub' in user_uri_data:
                    sub_url = user_uri_data['normal_sub']
                    ipv4_url = user_uri_data.get('ipv4', '')
                    ipv4_info = _localized_ipv4_info(user_language, ipv4_url)

                    qr = qrcode.make(ipv4_url or sub_url)
                    bio = io.BytesIO()
                    qr.save(bio, 'PNG')
                    bio.seek(0)
                    success_message = get_message_text(user_language, "payment_approved").format(plan_gb=plan_gb, days=days, username=username, sub_url=sub_url, ipv4_info=ipv4_info)
                    bot.send_photo(
                        user_to_notify,
                        photo=bio,
                        caption=success_message,
                        parse_mode="Markdown"
                    )
                    send_download_prompt_safely(bot, user_to_notify, user_language)
                else:
                    bot.send_message(user_to_notify, get_message_text(user_language, "payment_approved_no_url"))
                _update_receipt_message_refs(
                    payment_id,
                    payment_record,
                    f"✅ Payment {payment_id} approved by {call.from_user.first_name}."
                )
            else:
                _record_sale_creation_failure(payment_id, username=username, api_client=api_client)
                safe_answer_callback_query(bot, call.id, text=get_message_text(language, "failed_to_create_user"))
                bot.send_message(user_to_notify, get_message_text(user_language, "payment_approved_user_error"))
        elif action == 'reject':
            _record_review_audit(payment_id, call, action, reviewer_role)
            update_payment_status(payment_id, 'rejected')
            user_to_notify = payment_record['user_id']
            _release_checkout_incentives(
                user_to_notify,
                payment_record.get('incentive_reservation_id')
                or payment_record.get('account_credit_reservation_id'),
            )
            user_language = get_user_language(user_to_notify)
            current_caption = call.message.caption or ""
            
            if payment_record.get('type') == 'settlement' or payment_record.get('plan_gb') == 'Settlement':
                 bot.send_message(user_to_notify, get_message_text(user_language, "settlement_payment_rejected"))
                 rejection_caption = f"{current_caption}\n\n❌ Settlement Payment {payment_id} rejected by {call.from_user.first_name}."
            elif payment_record.get('type') == 'renewal':
                 bot.send_message(user_to_notify, get_message_text(user_language, "payment_rejected"))
                 rejection_caption = f"{current_caption}\n\n❌ Renewal Payment {payment_id} rejected by {call.from_user.first_name}."
            else:
                 bot.send_message(user_to_notify, get_message_text(user_language, "payment_rejected"))
                 rejection_caption = f"{current_caption}\n\n❌ Payment {payment_id} rejected by {call.from_user.first_name}."
                 
            _update_receipt_message_refs(payment_id, payment_record, rejection_caption)
    except Exception as e:
        _record_processing_error(payment_id, e)
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_approval:'))
def handle_admin_approval(call):
    try:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        user_is_admin = is_admin(user_id)
        _, action, payment_id = call.data.split(':')
        payment_record = get_payment_record(payment_id)
        if not payment_record:
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "payment_record_not_found"))
            return
        if not can_review_receipt(user_id, payment_record, is_admin_user=user_is_admin):
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "not_authorized"))
            return
        reviewer_role = "admin" if user_is_admin else "checker"
        if payment_record['status'] != 'pending_approval':
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "payment_already_processed").format(status=payment_record['status']))
            return
        if action not in {'approve', 'reject'}:
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_occurred").format(error="invalid action"))
            return
        if not _claim_payment_or_answer(call, language, payment_id, {'pending_approval'}):
            return

        payment_record = get_payment_record(payment_id) or payment_record
        try:
            queued = _queue_payment_job(
                ("admin_approval", payment_id),
                _process_admin_approval_job,
                call,
                action,
                payment_id,
                payment_record,
                reviewer_role,
                language,
            )
        except Exception as enqueue_error:
            _release_processing_for_retry(payment_id, 'pending_approval', enqueue_error)
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_occurred").format(error=str(enqueue_error)))
            return
        if queued:
            safe_answer_callback_query(bot, call.id, text="Processing approval...")
        else:
            safe_answer_callback_query(bot, call.id, text=get_message_text(language, "payment_already_processed").format(status="processing"))
    except Exception as e:
        user_id = call.from_user.id
        language = get_user_language(user_id)
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_occurred").format(error=str(e)))

def _process_check_payment_job(call):
    caller_id = call.from_user.id
    language = get_user_language(caller_id)
    payment_id = call.data.split(':')[1]
    payment_record = get_payment_record(payment_id)
    if not payment_record:
        safe_send_message(bot, caller_id, get_message_text(language, "payment_record_not_found"))
        return
    if not _can_access_payment_record(caller_id, payment_record):
        safe_send_message(bot, caller_id, get_message_text(language, "not_authorized"))
        return
    if payment_record.get('status') == 'completed':
        safe_send_message(
            bot,
            caller_id,
            get_message_text(language, "payment_already_processed").format(
                status=_localized_payment_status(language, 'completed')
            ),
        )
        return
    if payment_record.get('status') == 'processing':
        safe_send_message(
            bot,
            caller_id,
            get_message_text(language, "payment_already_processed").format(
                status=_localized_payment_status(language, 'processing')
            ),
        )
        return
    payment_handler = CryptoPayment()
    payment_status_response = payment_handler.check_payment_status(payment_id)
    if "error" in payment_status_response:
        safe_send_message(bot, caller_id, get_message_text(language, "error_checking_payment").format(error=payment_status_response['error']))
        return
    payment_status_data = payment_status_response.get('result', {})
    status = payment_status_data.get('status') or payment_status_data.get('payment_status') or payment_status_data.get('paymentStatus')
    if status and status.lower() == 'paid':
        if not claim_payment_for_processing(payment_id, allowed_statuses={'pending'}):
            latest_record = get_payment_record(payment_id) or {}
            latest_status = latest_record.get('status', 'unknown')
            safe_send_message(
                bot,
                caller_id,
                get_message_text(language, "payment_already_processed").format(
                    status=_localized_payment_status(language, latest_status)
                ),
            )
            return

        payment_record = get_payment_record(payment_id) or payment_record
        user_id = _payment_owner_id(payment_record)
        user_language = get_user_language(user_id)
        if str(caller_id) == str(user_id):
            telegram_username = call.from_user.username
        else:
            telegram_username = _payment_owner_username(payment_record)
        plan_gb = payment_record.get('plan_gb')
        
        if payment_record.get('type') == 'settlement' or plan_gb == 'Settlement':
            success, credited_amount, remaining_debt = _apply_reseller_settlement_payment(user_id, payment_record)
            if not success:
                _release_processing_for_retry(payment_id, 'pending', "settlement credit failed")
                safe_send_message(
                    bot,
                    caller_id,
                    get_message_text(language, "settlement_credit_failed"),
                )
                return
            update_payment_status(payment_id, 'completed')
            _send_reseller_settlement_admin_notification(
                user_id,
                payment_id,
                payment_record,
                credited_amount=credited_amount,
                payment_method="Crypto",
                telegram_username=telegram_username,
            )
            bot.send_message(
                user_id,
                _settlement_approved_message(user_language, user_id, credited_amount, remaining_debt),
                parse_mode="Markdown"
            )
            return

        if payment_record.get('type') == 'renewal':
            _process_customer_renewal_payment(
                payment_id,
                payment_record,
                notify_chat_id=user_id,
                payment_method="Crypto",
                telegram_username=telegram_username,
            )
            return

        days = payment_record.get('days')
        price = payment_record.get('price')
        
        unlimited = payment_record.get('unlimited')
        if unlimited is None:
                plans = load_plans()
                if plan_gb in plans:
                    unlimited = plans[plan_gb].get('unlimited', False)
                else:
                    unlimited = False
        
        api_client = APIClient()
        username, result, api_client = create_sale_user_with_note(
            api_client,
            user_id,
            plan_gb,
            days,
            unlimited,
        )
        if result:
            if not _complete_sale_payment_or_notify(payment_id, user_id, username, api_client):
                bot.send_message(
                    user_id,
                    get_message_text(user_language, "payment_completed_user_error"),
                    parse_mode="Markdown"
                )
                return
            send_admin_payment_notification(
                user_id,
                username,
                plan_gb,
                price,
                payment_id,
                "Crypto",
                telegram_username=telegram_username,
                server_name=getattr(api_client, 'server_name', None),
                server_id=getattr(api_client, 'server_id', None),
            )
            add_referral_reward(user_id, price, payment_id)
            user_uri_data = api_client.get_user_uri(username)
            if user_uri_data and 'normal_sub' in user_uri_data:
                sub_url = user_uri_data['normal_sub']
                ipv4_url = user_uri_data.get('ipv4', '')
                ipv4_info = _localized_ipv4_info(user_language, ipv4_url)

                qr = qrcode.make(ipv4_url or sub_url)
                bio = io.BytesIO()
                qr.save(bio, 'PNG')
                bio.seek(0)
                success_message = get_message_text(user_language, "payment_completed").format(plan_gb=plan_gb, username=username, sub_url=sub_url, ipv4_info=ipv4_info)
                bot.send_photo(
                    user_id,
                    photo=bio,
                    caption=success_message,
                    parse_mode="Markdown"
                )
                send_download_prompt_safely(bot, user_id, user_language)
            else:
                bot.send_message(
                    user_id,
                    get_message_text(user_language, "payment_completed_no_url"),
                    parse_mode="Markdown"
                )
        else:
            bot.send_message(
                user_id,
                get_message_text(user_language, "payment_completed_user_error"),
                parse_mode="Markdown"
            )
            _record_crypto_sale_creation_failure(payment_id, username=username, api_client=api_client)
    elif status and status.lower() == 'pending':
        safe_send_message(bot, caller_id, get_message_text(language, "payment_pending"))
    else:
        _close_unpaid_gateway_checkout(payment_id, payment_record, status)
        safe_send_message(
            bot,
            caller_id,
            get_message_text(language, "payment_status").format(
                status=_localized_payment_status(language, status)
            ),
        )
    try:
        import logging
        logging.getLogger('ajib.payments').debug(f"Check payment response for {payment_id}: {payment_status_response}")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith('check_payment:'))
def handle_check_payment(call):
    caller_id = call.from_user.id
    language = get_user_language(caller_id)
    payment_id = call.data.split(':')[1]
    payment_record = get_payment_record(payment_id)
    if not payment_record:
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "payment_record_not_found"))
        return
    if not _can_access_payment_record(caller_id, payment_record):
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "not_authorized"), show_alert=True)
        return
    if payment_record.get('status') == 'completed':
        safe_answer_callback_query(
            bot,
            call.id,
            text=get_message_text(language, "payment_already_processed").format(
                status=_localized_payment_status(language, 'completed')
            ),
        )
        return
    if payment_record.get('status') == 'processing':
        safe_answer_callback_query(
            bot,
            call.id,
            text=get_message_text(language, "payment_already_processed").format(
                status=_localized_payment_status(language, 'processing')
            ),
        )
        return

    try:
        queued = _queue_payment_job(("check_payment", payment_id), _process_check_payment_job, call)
    except Exception as enqueue_error:
        safe_answer_callback_query(bot, call.id, text=get_message_text(language, "error_checking_payment").format(error=str(enqueue_error)))
        return
    if queued:
        safe_answer_callback_query(
            bot,
            call.id,
            text=get_message_text(language, 'payment_status_checking'),
        )
    else:
        safe_answer_callback_query(
            bot,
            call.id,
            text=get_message_text(language, 'payment_status_check_in_progress'),
        )


def process_payment_webhook(request_data):
    try:
        status = request_data.get('status') or request_data.get('payment_status') or request_data.get('paymentStatus')
        payments = load_payments()
        record_key = None
        if request_data.get('uuid'):
            record_key = request_data.get('uuid')
        elif request_data.get('order_id'):
            incoming_order = request_data.get('order_id')
            for k, v in payments.items():
                if v.get('order_id') == incoming_order or v.get('payment_id') == incoming_order:
                    record_key = k
                    break
        if not record_key:
            return False
        if status and status.lower() == 'paid':
            payment_record = get_payment_record(record_key)
            if payment_record and payment_record.get('status') == 'pending':
                if not claim_payment_for_processing(record_key, allowed_statuses={'pending'}):
                    return False
                payment_record = get_payment_record(record_key) or payment_record
                user_id = payment_record.get('user_id')
                user_language = get_user_language(user_id)
                plan_gb = payment_record.get('plan_gb')
                
                if payment_record.get('type') == 'settlement' or plan_gb == 'Settlement':
                    success, credited_amount, remaining_debt = _apply_reseller_settlement_payment(user_id, payment_record)
                    if not success:
                        _release_processing_for_retry(record_key, 'pending', "settlement credit failed")
                        return False
                    update_payment_status(record_key, 'completed')
                    _send_reseller_settlement_admin_notification(
                        user_id,
                        record_key,
                        payment_record,
                        credited_amount=credited_amount,
                        payment_method="Crypto",
                    )
                    bot.send_message(
                        user_id,
                        _settlement_approved_message(user_language, user_id, credited_amount, remaining_debt),
                        parse_mode="Markdown"
                    )
                    return True

                if payment_record.get('type') == 'renewal':
                    telegram_username = None
                    try:
                        chat = bot.get_chat(user_id)
                        telegram_username = chat.username
                    except:
                        pass
                    return _process_customer_renewal_payment(
                        record_key,
                        payment_record,
                        notify_chat_id=user_id,
                        payment_method="Crypto",
                        telegram_username=telegram_username,
                    )

                days = payment_record.get('days')
                price = payment_record.get('price')
                
                unlimited = payment_record.get('unlimited')
                if unlimited is None:
                    plans = load_plans()
                    if plan_gb in plans:
                        unlimited = plans[plan_gb].get('unlimited', False)
                    else:
                        unlimited = False
                
                api_client = APIClient()
                username, result, api_client = create_sale_user_with_note(
                    api_client,
                    user_id,
                    plan_gb,
                    days,
                    unlimited,
                )
                if result:
                    if not _complete_sale_payment_or_notify(record_key, user_id, username, api_client):
                        return False
                    payment_method = "Crypto" if "order_id" in payment_record else "Card to Card"
                    telegram_username = None
                    try:
                        chat = bot.get_chat(user_id)
                        telegram_username = chat.username
                    except:
                        pass
                    send_admin_payment_notification(
                        user_id,
                        username,
                        plan_gb,
                        price,
                        record_key,
                        payment_method,
                        telegram_username=telegram_username,
                        server_name=getattr(api_client, 'server_name', None),
                        server_id=getattr(api_client, 'server_id', None),
                    )
                    add_referral_reward(user_id, price, record_key)
                    
                    user_uri_data = api_client.get_user_uri(username)
                    sub_url = user_uri_data.get('normal_sub') if user_uri_data else None
                    ipv4_url = user_uri_data.get('ipv4', '') if user_uri_data else ''
                    ipv4_info = _localized_ipv4_info(user_language, ipv4_url)

                    success_message = get_message_text(user_language, "payment_completed").format(plan_gb=plan_gb, username=username, sub_url=sub_url, ipv4_info=ipv4_info)
                    bot.send_message(
                        user_id,
                        success_message,
                        parse_mode="Markdown"
                    )
                    if sub_url:
                        qr = qrcode.make(ipv4_url or sub_url)
                        bio = io.BytesIO()
                        qr.save(bio, 'PNG')
                        bio.seek(0)
                        bot.send_photo(
                            user_id,
                            photo=bio
                        )
                        send_download_prompt_safely(bot, user_id, user_language)
                    return True
                else:
                    _record_crypto_sale_creation_failure(record_key, username=username, api_client=api_client)
                    bot.send_message(
                        user_id,
                        get_message_text(user_language, "payment_completed_user_error"),
                        parse_mode="Markdown"
                    )
                    return False
            return False
        payment_record = get_payment_record(record_key)
        if payment_record and payment_record.get('status') == 'pending':
            _close_unpaid_gateway_checkout(record_key, payment_record, status)
        return False
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return False

def _reserved_renewal_review_markup(kind, event):
    markup = types.InlineKeyboardMarkup(row_width=2)
    reason = event.get('reason')
    if kind == 'p':
        identity = event['payment_id']
        prefix = f"rr:p"
    else:
        identity = f"{event['reseller_id']}:{event['reservation_id']}"
        prefix = "rr:r"
    if reason == 'external_renewal':
        markup.add(
            types.InlineKeyboardButton('Keep for next expiry', callback_data=f"{prefix}:wait:{identity}"),
            types.InlineKeyboardButton('Apply now', callback_data=f"{prefix}:apply:{identity}"),
        )
    elif reason == 'server_unavailable':
        markup.add(
            types.InlineKeyboardButton('Retry now', callback_data=f"{prefix}:retry:{identity}"),
        )
    else:
        markup.add(
            types.InlineKeyboardButton('Retry now', callback_data=f"{prefix}:retry:{identity}"),
            types.InlineKeyboardButton('Apply now', callback_data=f"{prefix}:apply:{identity}"),
        )
    return markup


def _deliver_reserved_renewal(event, recipient_id):
    from utils.renewal import format_renewal_success

    result = event.get('result') or {}
    record = event.get('record') or {}
    api_client = event.get('api_client') or result.get('api_client')
    username = result.get('username') or record.get('renewal_username') or record.get('username')
    plan = record.get('renewal_plan_snapshot') or {}
    plan_gb = plan.get('plan_gb') or record.get('plan_gb') or record.get('gb')
    days = plan.get('days') or record.get('days')
    language = get_user_language(recipient_id)
    uri_data = api_client.get_user_uri(username) if api_client and username else None
    sub_url = uri_data.get('normal_sub') if uri_data else None
    ipv4_url = uri_data.get('ipv4', '') if uri_data else ''
    message = format_renewal_success(
        language,
        result,
        plan_gb,
        days,
        sub_url=sub_url,
        ipv4_url=ipv4_url,
    )
    if sub_url:
        qr = qrcode.make(ipv4_url or sub_url)
        bio = io.BytesIO()
        qr.save(bio, 'PNG')
        bio.seek(0)
        bot.send_photo(recipient_id, photo=bio, caption=message, parse_mode='Markdown')
        send_download_prompt_safely(bot, recipient_id, language)
    else:
        bot.send_message(recipient_id, message, parse_mode='Markdown')


def _notify_reserved_renewal_attention(kind, event, recipient_id):
    from utils.renewal import mark_payment_renewal_alerted
    from utils.reseller import mark_reseller_renewal_alerted

    buyer_alert_due = event.get('buyer_alert_due', event.get('alert_due', False))
    operator_alert_due = event.get('operator_alert_due', event.get('alert_due', False))
    if not buyer_alert_due and not operator_alert_due:
        return
    record = event.get('record') or {}
    user_language = get_user_language(recipient_id)
    username = (
        record.get('renewal_username')
        or record.get('username')
        or get_message_text(user_language, 'value_unknown')
    )
    reason = event.get('reason') or 'renewal_reset_failed'
    reason_text = _renewal_reason_text(user_language, reason)
    if buyer_alert_due:
        message_key = 'renewal_reserved_server_unavailable' if reason == 'server_unavailable' else 'renewal_reserved_attention'
        user_text = get_message_text(user_language, message_key).format(
            username=username,
            reason=reason_text,
        )
        try:
            bot.send_message(recipient_id, user_text, parse_mode='Markdown')
        except Exception:
            pass

    if operator_alert_due:
        markup = _reserved_renewal_review_markup(kind, event)
        server_id = record.get('renewal_server_id') or record.get('server_id') or 'unknown'
        for admin_id in ADMIN_USER_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"Reserved renewal needs attention.\nUser: `{recipient_id}`\nConfig: `{username}`\nServer: `{server_id}`\nReason: {reason}",
                    reply_markup=markup,
                    parse_mode='Markdown',
                )
            except Exception:
                pass
    if kind == 'p':
        if buyer_alert_due:
            mark_payment_renewal_alerted(event['payment_id'], audience='buyer')
        if operator_alert_due:
            mark_payment_renewal_alerted(event['payment_id'], audience='operator')
    else:
        if buyer_alert_due:
            mark_reseller_renewal_alerted(event['reseller_id'], event['reservation_id'], audience='buyer')
        if operator_alert_due:
            mark_reseller_renewal_alerted(event['reseller_id'], event['reservation_id'], audience='operator')


def process_main_reserved_renewals(now=None):
    from utils.renewal import (
        list_payment_renewal_ids,
        process_payment_renewal_reservation,
        process_reseller_renewal_reservation,
    )
    from utils.reseller import list_reseller_renewal_reservations

    events = []
    for payment_id in list_payment_renewal_ids():
        try:
            event = process_payment_renewal_reservation(payment_id, now=now)
            if not event:
                continue
            events.append(event)
            record = event.get('record') or {}
            recipient_id = record.get('user_id')
            if event.get('status') == 'applied' and recipient_id is not None:
                _deliver_reserved_renewal(event, recipient_id)
            elif event.get('status') == 'attention' and recipient_id is not None:
                _notify_reserved_renewal_attention('p', event, recipient_id)
        except Exception as error:
            logging.getLogger('ajib.renewals').exception(
                'Failed to process payment renewal reservation %s: %s', payment_id, error
            )

    for item in list_reseller_renewal_reservations():
        reservation = item.get('reservation') or {}
        if reservation.get('renewal_source') == 'hosted_customer':
            continue
        reseller_id = item.get('reseller_id')
        reservation_id = reservation.get('reservation_id')
        if not reservation_id:
            continue
        try:
            event = process_reseller_renewal_reservation(
                reseller_id,
                reservation_id,
                now=now,
            )
            if not event:
                continue
            events.append(event)
            if event.get('status') == 'applied':
                _deliver_reserved_renewal(event, int(reseller_id))
            elif event.get('status') == 'attention':
                _notify_reserved_renewal_attention('r', event, int(reseller_id))
        except Exception as error:
            logging.getLogger('ajib.renewals').exception(
                'Failed to process reseller renewal reservation %s/%s: %s',
                reseller_id,
                reservation_id,
                error,
            )
    return events


@bot.callback_query_handler(func=lambda call: call.data.startswith('rr:'))
def handle_reserved_renewal_review(call):
    if not is_admin(call.from_user.id):
        safe_answer_callback_query(bot, call.id, text='Admin access required.', show_alert=True)
        return
    parts = call.data.split(':')
    if len(parts) not in {4, 5} or parts[1] not in {'p', 'r'} or parts[2] not in {'wait', 'apply', 'retry'}:
        safe_answer_callback_query(bot, call.id, text='Invalid renewal review action.', show_alert=True)
        return
    kind, action = parts[1], parts[2]
    try:
        from utils.renewal import (
            capture_user_state,
            lookup_renewal_user,
            process_payment_renewal_reservation,
            process_reseller_renewal_reservation,
            refresh_payment_renewal_baseline,
        )
        from utils.reseller import get_reseller_renewal_reservation, refresh_reseller_renewal_baseline

        if kind == 'p':
            payment_id = parts[3]
            record = get_payment_record(payment_id) or {}
            if action == 'wait':
                client, live, _lookup_result = lookup_renewal_user(
                    MultiServerAPI(),
                    record.get('renewal_username'),
                    server_id=record.get('renewal_server_id'),
                )
                success = bool(live) and refresh_payment_renewal_baseline(payment_id, live)
                event = None
            else:
                event = process_payment_renewal_reservation(
                    payment_id,
                    force=True,
                    force_apply=action == 'apply',
                )
                success = bool(event)
                if event and event.get('status') == 'applied':
                    _deliver_reserved_renewal(event, (event.get('record') or {}).get('user_id'))
        else:
            reseller_id, reservation_id = parts[3], parts[4]
            item = get_reseller_renewal_reservation(reseller_id, reservation_id) or {}
            if action == 'wait':
                config = item.get('config') or {}
                client, live, _lookup_result = lookup_renewal_user(
                    MultiServerAPI(),
                    config.get('username'),
                    server_id=config.get('server_id'),
                )
                success = bool(live) and refresh_reseller_renewal_baseline(
                    reseller_id,
                    reservation_id,
                    capture_user_state(live),
                )
                event = None
            else:
                event = process_reseller_renewal_reservation(
                    reseller_id,
                    reservation_id,
                    force=True,
                    force_apply=action == 'apply',
                )
                success = bool(event)
                if event and event.get('status') == 'applied':
                    _deliver_reserved_renewal(event, int(reseller_id))
        safe_answer_callback_query(
            bot,
            call.id,
            text='Renewal reservation updated.' if success else 'Renewal reservation could not be updated.',
            show_alert=True,
        )
    except Exception as error:
        logging.getLogger('ajib.renewals').exception('Renewal review failed: %s', error)
        safe_answer_callback_query(bot, call.id, text='Renewal review failed.', show_alert=True)


def check_pending_payments():
    try:
        try:
            process_main_reserved_renewals()
        except (ImportError, AttributeError):
            # Rolling upgrades and isolated compatibility tests may load only
            # the legacy renewal surface.
            pass
        try:
            _reconcile_completed_checkout_incentives()
        except Exception:
            logging.getLogger('ajib.payments').exception(
                "Failed while reconciling completed checkout incentives"
            )
        try:
            send_due_card_checkout_reminders()
        except Exception:
            logging.getLogger('ajib.payments').exception(
                "Failed while processing card checkout reminders"
            )
        payments = load_payments()
        payment_handler = CryptoPayment()
        
        for payment_id, record in payments.items():
            if record.get('status') == 'pending':
                # Check if payment is not too old (e.g., > 24 hours) — mark as expired
                created_at_str = record.get('created_at')
                if created_at_str:
                    try:
                        created_at = datetime.datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
                        if datetime.datetime.now() - created_at > datetime.timedelta(hours=24):
                            update_payment_status(payment_id, 'expired')
                            _release_checkout_incentives(
                                record.get('user_id'),
                                record.get('incentive_reservation_id')
                                or record.get('account_credit_reservation_id'),
                            )
                            continue
                    except ValueError:
                        pass

                # One event-triggered reminder; the persisted marker prevents repeats
                # across polling cycles and bot restarts.
                maybe_send_checkout_reminder(payment_id, record)

                # Check status
                try:
                    response = payment_handler.check_payment_status(payment_id)
                    if "error" in response:
                        continue
                        
                    result = response.get('result', {})
                    status = result.get('status') or result.get('payment_status') or result.get('paymentStatus')
                    if _close_unpaid_gateway_checkout(payment_id, record, status):
                        continue
                    
                    if status and status.lower() == 'paid':
                        if not claim_payment_for_processing(payment_id, allowed_statuses={'pending'}):
                            continue
                        record = get_payment_record(payment_id) or record
                        # Process payment
                        user_id = record.get('user_id')
                        plan_gb = record.get('plan_gb')
                        
                        if record.get('type') == 'settlement' or plan_gb == 'Settlement':
                            success, credited_amount, remaining_debt = _apply_reseller_settlement_payment(user_id, record)
                            if not success:
                                _release_processing_for_retry(payment_id, 'pending', "settlement credit failed")
                                continue
                            update_payment_status(payment_id, 'completed')
                            _send_reseller_settlement_admin_notification(
                                user_id,
                                payment_id,
                                record,
                                credited_amount=credited_amount,
                                payment_method="Crypto",
                            )
                            try:
                                user_language = get_user_language(user_id)
                                bot.send_message(
                                    user_id,
                                    _settlement_approved_message(user_language, user_id, credited_amount, remaining_debt),
                                    parse_mode="Markdown"
                                )
                            except:
                                pass
                            continue

                        if record.get('type') == 'renewal':
                            telegram_username = None
                            try:
                                chat = bot.get_chat(user_id)
                                telegram_username = chat.username
                            except:
                                pass
                            _process_customer_renewal_payment(
                                payment_id,
                                record,
                                notify_chat_id=user_id,
                                payment_method="Crypto",
                                telegram_username=telegram_username,
                            )
                            continue

                        days = record.get('days')
                        price = record.get('price')
                        
                        unlimited = record.get('unlimited')
                        if unlimited is None:
                            plans = load_plans()
                            if plan_gb in plans:
                                unlimited = plans[plan_gb].get('unlimited', False)
                            else:
                                unlimited = False
                        
                        api_client = APIClient()
                        username, add_result, api_client = create_sale_user_with_note(
                            api_client,
                            user_id,
                            plan_gb,
                            days,
                            unlimited,
                        )
                        
                        if add_result:
                            if not _complete_sale_payment_or_notify(payment_id, user_id, username, api_client):
                                continue
                            telegram_username = None
                            try:
                                chat = bot.get_chat(user_id)
                                telegram_username = chat.username
                            except:
                                pass
                            send_admin_payment_notification(
                                user_id,
                                username,
                                plan_gb,
                                price,
                                payment_id,
                                "Crypto",
                                telegram_username=telegram_username,
                                server_name=getattr(api_client, 'server_name', None),
                                server_id=getattr(api_client, 'server_id', None),
                            )
                            add_referral_reward(user_id, price, payment_id)
                            user_uri_data = api_client.get_user_uri(username)

                            user_language = get_user_language(user_id)
                            
                            if user_uri_data and 'normal_sub' in user_uri_data:
                                sub_url = user_uri_data['normal_sub']
                                ipv4_url = user_uri_data.get('ipv4', '')
                                ipv4_info = _localized_ipv4_info(user_language, ipv4_url)

                                qr = qrcode.make(ipv4_url or sub_url)
                                bio = io.BytesIO()
                                qr.save(bio, 'PNG')
                                bio.seek(0)
                                success_message = get_message_text(user_language, "payment_completed").format(plan_gb=plan_gb, username=username, sub_url=sub_url, ipv4_info=ipv4_info)
                                try:
                                    bot.send_photo(
                                        user_id,
                                        photo=bio,
                                        caption=success_message,
                                        parse_mode="Markdown"
                                    )
                                    send_download_prompt_safely(bot, user_id, user_language)
                                except Exception as e:
                                    print(f"Failed to send success message to user {user_id}: {e}")
                            else:
                                try:
                                    bot.send_message(
                                        user_id,
                                        get_message_text(user_language, "payment_completed_no_url"),
                                        parse_mode="Markdown"
                                    )
                                except Exception as e:
                                    print(f"Failed to send success message to user {user_id}: {e}")
                        else:
                            _record_crypto_sale_creation_failure(payment_id, username=username, api_client=api_client)
                except Exception as e:
                    print(f"Error checking pending payment {payment_id}: {e}")

        # Also run reseller debt reminders/escalations on the same monitoring cycle.
        debt_events = evaluate_reseller_debt_policies()
        for event in debt_events:
            try:
                reseller_id = int(event['user_id'])
            except (TypeError, ValueError):
                continue

            debt = float(event.get('debt', 0.0))
            debt_state = str(event.get('debt_state', 'active'))
            debt_age_days = int(event.get('debt_age_days', 0))
            debt_age_hours = float(event.get('debt_age_hours', 0.0))
            unlock_amount = float(event.get('unlock_amount', 0.0))
            hours_until_ban = float(event.get('hours_until_ban', 0.0))

            if event.get('auto_banned') or event.get('auto_suspended') or event.get('notify_user'):
                try:
                    user_language = get_user_language(reseller_id)
                    if event.get('auto_banned'):
                        user_message = get_message_text(user_language, "reseller_auto_banned").format(
                            debt=debt,
                        )
                    elif event.get('auto_suspended'):
                        user_message = get_message_text(user_language, "reseller_auto_suspended").format(
                            debt=debt,
                            hours_until_ban=hours_until_ban,
                        )
                    elif debt_state == 'suspended':
                        user_message = get_message_text(user_language, "reseller_debt_reminder_suspended").format(
                            debt=debt,
                            debt_age_days=debt_age_days,
                            unlock_amount=unlock_amount
                        )
                    else:
                        user_message = get_message_text(user_language, "reseller_debt_reminder_warning").format(
                            debt=debt,
                            debt_age_days=debt_age_days,
                            suspend_threshold=DEBT_SUSPEND_THRESHOLD
                        )

                    markup = None
                    if debt > 0 and not event.get('auto_banned'):
                        markup = types.InlineKeyboardMarkup()
                        markup.add(
                            types.InlineKeyboardButton(
                                get_button_text(user_language, "settle_debt"),
                                callback_data=f"reseller:settle:{debt:.2f}"
                            )
                        )
                    bot.send_message(reseller_id, user_message, reply_markup=markup, parse_mode="Markdown")
                except Exception:
                    pass

            if event.get('notify_admin'):
                for admin_id in ADMIN_USER_IDS:
                    try:
                        admin_language = get_user_language(admin_id)
                        if event.get('auto_banned'):
                            admin_message = get_message_text(admin_language, "admin_reseller_auto_banned").format(
                                reseller_id=reseller_id,
                                debt=debt,
                                debt_age_hours=debt_age_hours,
                            )
                        elif event.get('auto_suspended'):
                            admin_message = get_message_text(admin_language, "admin_reseller_auto_suspended").format(
                                reseller_id=reseller_id,
                                debt=debt,
                                debt_age_hours=debt_age_hours,
                            )
                        else:
                            state_text = get_message_text(admin_language, _debt_state_label_key(debt_state))
                            admin_message = get_message_text(admin_language, "reseller_debt_threshold_crossed_admin").format(
                                reseller_id=reseller_id,
                                debt_state=state_text,
                                debt=debt,
                                debt_age_days=debt_age_days,
                                warning_threshold=DEBT_WARNING_THRESHOLD,
                                suspend_threshold=DEBT_SUSPEND_THRESHOLD
                            )
                        bot.send_message(admin_id, admin_message, parse_mode="Markdown")
                    except Exception:
                        pass

    except Exception as e:
        print(f"Error in check_pending_payments: {e}")
