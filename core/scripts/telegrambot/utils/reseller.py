import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    from . import database, state_store
except ImportError:  # Direct module loading in compatibility tests/tools.
    database = None
    state_store = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

RESELLERS_FILE = '/etc/ajib/core/scripts/telegrambot/resellers.json'
reseller_lock = threading.RLock()


def _sqlite_managed():
    return state_store is not None and state_store.is_managed_path(RESELLERS_FILE)


def _safe_float_env(key, default):
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return float(default)


DEBT_WARNING_THRESHOLD = _safe_float_env('RESELLER_DEBT_WARNING_THRESHOLD', 20.0)
DEBT_SUSPEND_THRESHOLD = _safe_float_env('RESELLER_DEBT_SUSPEND_THRESHOLD', 50.0)
DEBT_SETTLEMENT_THRESHOLD = _safe_float_env('RESELLER_SETTLEMENT_THRESHOLD', 1.0)
DEBT_REMINDER_INTERVAL_HOURS = max(1.0, _safe_float_env('RESELLER_DEBT_REMINDER_INTERVAL_HOURS', 24.0))
DEBT_SUSPEND_DEADLINE_HOURS = max(1.0, _safe_float_env('RESELLER_DEBT_SUSPEND_DEADLINE_HOURS', 48.0))
DEBT_HOLD_DEADLINE_HOURS = max(1.0, _safe_float_env('RESELLER_DEBT_HOLD_DEADLINE_HOURS', 72.0))
DEBT_REMOVAL_DEADLINE_HOURS = max(
    DEBT_HOLD_DEADLINE_HOURS,
    _safe_float_env('RESELLER_DEBT_REMOVAL_DEADLINE_HOURS', 168.0),
)
DEBT_FINAL_WARNING_HOURS = max(
    DEBT_HOLD_DEADLINE_HOURS,
    _safe_float_env('RESELLER_DEBT_FINAL_WARNING_HOURS', 144.0),
)
# Kept as a read-only compatibility alias. Debt policy no longer bans resellers.
DEBT_BAN_DEADLINE_HOURS = DEBT_REMOVAL_DEADLINE_HOURS
UNBAN_GRACE_BAN_DEADLINE_HOURS = DEBT_HOLD_DEADLINE_HOURS
SUSPENDED_REASON_DEBT = 'debt'
SUSPENDED_REASON_UNBAN_GRACE = 'unban_grace'
REMOVAL_REASON_BANNED_RESELLER_CLEANUP = 'banned_reseller_cleanup'
REMOVAL_REASON_RESELLER_DEBT_DEFAULT = 'reseller_debt_default'
REMOVAL_NOTE_BANNED_RESELLER_CLEANUP = 'Removed during banned reseller unpaid user cleanup'
REMOVAL_STATUS_DELETED_FROM_VPN = 'deleted_from_vpn'
REMOVAL_STATUS_ALREADY_MISSING = 'already_missing'
RESELLER_TRUST_START_LIMIT = 5.0
RESELLER_TRUST_LIMIT_STEP = 5.0
RESELLER_TRUST_PAID_STEP = 10.0
RESELLER_TRUST_MAX_LIMIT = 30.0
RESELLER_LEVEL_COUNT = 6
RESELLER_BASE_DISCOUNT_PERCENT = 20
RESELLER_DISCOUNT_STEP_PERCENT = 1
RESELLER_LEVEL_ICONS = ('🌱', '🥉', '🥈', '🥇', '💎', '👑')
RESELLER_LEVEL_PRESENTATION_LEASE_SECONDS = max(
    30.0,
    _safe_float_env('RESELLER_LEVEL_PRESENTATION_LEASE_SECONDS', 300.0),
)
MONEY_QUANTUM = Decimal('0.01')
DEBT_CHARGE_EPSILON = 0.005
CREDIT_OUTCOME_LIMIT = 3
CREDIT_OUTCOME_WEIGHTS = {'good': 0, 'late': 1, 'default': 2}


def _update_recruitment_milestone(reseller_id, reseller_data):
    """Best-effort growth accounting; reseller fulfillment never depends on it."""
    try:
        from utils.recruitment import evaluate_and_notify_recruitment_milestone

        evaluate_and_notify_recruitment_milestone(reseller_id, reseller_data)
    except Exception:
        pass


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_nonnegative_float(value):
    amount = _safe_float(value, 0.0)
    return amount if math.isfinite(amount) and amount > 0 else 0.0


def _money_value(value):
    """Return a finite, cent-rounded, non-negative amount."""
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if not amount.is_finite() or amount <= 0:
        return 0.0
    return float(amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def _is_debt_fully_settled(debt):
    """Return whether debt is below the automated collection threshold.

    The balance remains in the auditable debt ledger so later charges can push
    it back over the threshold, but sub-threshold debt does not keep a debt
    cycle open or trigger collection actions.  A zero threshold retains exact
    zero-only settlement semantics.
    """
    amount = _money_value(debt)
    threshold = _money_value(DEBT_SETTLEMENT_THRESHOLD)
    if threshold == 0.0:
        return amount == 0.0
    return amount < threshold


def _charge_outstanding(charge):
    if not isinstance(charge, dict):
        return 0.0
    return _money_value(charge.get('outstanding_amount', charge.get('amount', 0.0)))


def _debt_charge_total(record):
    charges = (record or {}).get('debt_charges', [])
    if not isinstance(charges, list):
        return 0.0
    return round(sum(_charge_outstanding(charge) for charge in charges), 2)


def _next_debt_charge_sequence(record):
    try:
        current = int((record or {}).get('debt_charge_sequence', 0) or 0)
    except (TypeError, ValueError):
        current = 0
    for charge in (record or {}).get('debt_charges', []):
        if not isinstance(charge, dict):
            continue
        try:
            current = max(current, int(charge.get('sequence', 0) or 0))
        except (TypeError, ValueError):
            continue
    current += 1
    record['debt_charge_sequence'] = current
    return current


def _new_debt_charge(record, amount, kind, reference_id=None, metadata=None, charged_at=None):
    amount_value = _money_value(amount)
    if amount_value <= 0:
        return None
    reference = str(reference_id or '').strip()
    charge_id = reference or uuid.uuid4().hex
    existing = next(
        (
            charge
            for charge in record.get('debt_charges', [])
            if isinstance(charge, dict) and str(charge.get('id') or '') == charge_id
        ),
        None,
    )
    if existing is not None:
        return existing
    charge = {
        'id': charge_id,
        'sequence': _next_debt_charge_sequence(record),
        'kind': str(kind or 'debt'),
        'reference_id': reference or None,
        'original_amount': amount_value,
        'outstanding_amount': amount_value,
        'charged_at': charged_at or _now_str(),
        'metadata': dict(metadata or {}),
    }
    record.setdefault('debt_charges', []).append(charge)
    return charge


def _record_debt_allocation(record, amount, allocations, kind, reference_id=None):
    amount_value = _money_value(amount)
    if amount_value <= 0:
        return
    record.setdefault('debt_allocations', []).append({
        'id': str(reference_id or uuid.uuid4().hex),
        'kind': str(kind or 'settlement'),
        'amount': amount_value,
        'allocations': allocations,
        'created_at': _now_str(),
    })


def _allocate_debt_fifo(record, amount, kind='settlement', reference_id=None):
    remaining = Decimal(str(_money_value(amount)))
    if remaining <= 0:
        return 0.0
    allocations = []
    charges = [charge for charge in record.get('debt_charges', []) if isinstance(charge, dict)]
    def charge_order(charge):
        try:
            sequence = int(charge.get('sequence', 0) or 0)
        except (TypeError, ValueError):
            sequence = 0
        charged_at = _parse_time(charge.get('charged_at'))
        return (
            0 if charged_at is not None else 1,
            charged_at or datetime.min,
            sequence,
            str(charge.get('id') or ''),
        )

    charges.sort(key=charge_order)
    for charge in charges:
        outstanding = Decimal(str(_charge_outstanding(charge)))
        if outstanding <= 0:
            continue
        applied = min(outstanding, remaining)
        if applied <= 0:
            break
        charge['outstanding_amount'] = float(
            (outstanding - applied).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        )
        if charge['outstanding_amount'] <= DEBT_CHARGE_EPSILON:
            charge['outstanding_amount'] = 0.0
            charge['paid_at'] = _now_str()
        allocations.append({'charge_id': charge.get('id'), 'amount': float(applied)})
        remaining -= applied
        if remaining <= 0:
            break
    applied_total = _money_value(Decimal(str(_money_value(amount))) - remaining)
    _record_debt_allocation(record, applied_total, allocations, kind, reference_id=reference_id)
    return applied_total


def _ensure_debt_charge_ledger(record):
    """Lazily reconcile legacy aggregate debt into the itemized FIFO ledger."""
    charges = record.get('debt_charges')
    if not isinstance(charges, list):
        charges = []
    record['debt_charges'] = [charge for charge in charges if isinstance(charge, dict)]
    debt = _money_value(record.get('debt', 0.0))
    tracked = _debt_charge_total(record)
    if debt > tracked + DEBT_CHARGE_EPSILON:
        difference = round(debt - tracked, 2)
        _new_debt_charge(
            record,
            difference,
            'legacy_balance' if not record['debt_charges'] else 'balance_adjustment',
            reference_id=f"legacy-{uuid.uuid4().hex}",
            metadata={'synthetic': True},
            charged_at=record.get('debt_since') or record.get('created_at') or _now_str(),
        )
    elif tracked > debt + DEBT_CHARGE_EPSILON:
        _allocate_debt_fifo(record, tracked - debt, kind='balance_reconciliation')
    return record['debt_charges']


def _add_debt_charge(record, amount, kind, reference_id=None, metadata=None):
    amount_value = _money_value(amount)
    if amount_value <= 0:
        return None
    _ensure_debt_charge_ledger(record)
    reference = str(reference_id or '').strip()
    if reference:
        existing = get_reseller_debt_charge(record, reference)
        if existing is not None:
            return existing
    charge = _new_debt_charge(
        record,
        amount_value,
        kind,
        reference_id=reference,
        metadata=metadata,
    )
    if charge is None:
        return None
    record['debt'] = round(_safe_float(record.get('debt', 0.0)) + amount_value, 2)
    return charge


def get_reseller_debt_charge(record, charge_id):
    target = str(charge_id or '')
    if not target:
        return None
    for charge in (record or {}).get('debt_charges', []):
        if isinstance(charge, dict) and str(charge.get('id') or '') == target:
            return dict(charge)
    return None


def is_reseller_debt_charge_paid(record, charge_id):
    charge = get_reseller_debt_charge(record, charge_id)
    return bool(charge) and _charge_outstanding(charge) <= DEBT_CHARGE_EPSILON


def get_reseller_config_value(config):
    if not isinstance(config, dict) or _is_removed_config(config):
        return 0.0
    renewals = config.get('renewals', [])
    renewal_total = 0.0
    if isinstance(renewals, list):
        renewal_total = sum(
            _safe_float(renewal.get('price', 0.0))
            for renewal in renewals
            if isinstance(renewal, dict)
        )
    return _safe_float(config.get('price', 0.0)) + renewal_total


def get_reseller_config_total_value(configs):
    if not isinstance(configs, list):
        return 0.0
    return sum(get_reseller_config_value(config) for config in configs)


def _reseller_config_total(record):
    return get_reseller_config_total_value((record or {}).get('configs', []))


def get_reseller_total_paid(record):
    data = record or {}
    if 'total_paid' in data:
        return _safe_nonnegative_float(data.get('total_paid', 0.0))
    debt = _safe_float(data.get('debt', 0.0))
    return _safe_nonnegative_float(_reseller_config_total(data) - debt)


def get_reseller_level(total_paid):
    paid_amount = _safe_nonnegative_float(total_paid)
    paid_steps = int(paid_amount // RESELLER_TRUST_PAID_STEP)
    return min(RESELLER_LEVEL_COUNT, paid_steps + 1)


def get_reseller_discount_percent(total_paid):
    level = get_reseller_level(total_paid)
    return RESELLER_BASE_DISCOUNT_PERCENT + ((level - 1) * RESELLER_DISCOUNT_STEP_PERCENT)


def get_reseller_trust_limit(total_paid):
    level = get_reseller_level(total_paid)
    limit = RESELLER_TRUST_START_LIMIT + ((level - 1) * RESELLER_TRUST_LIMIT_STEP)
    return min(RESELLER_TRUST_MAX_LIMIT, limit)


def get_reseller_level_summary(record):
    total_paid = get_reseller_total_paid(record)
    level = get_reseller_level(total_paid)
    discount_percent = get_reseller_discount_percent(total_paid)
    trust_limit = get_reseller_trust_limit(total_paid)
    current_threshold = (level - 1) * RESELLER_TRUST_PAID_STEP
    next_threshold = (
        level * RESELLER_TRUST_PAID_STEP
        if level < RESELLER_LEVEL_COUNT
        else None
    )
    if next_threshold is None:
        progress_amount = RESELLER_TRUST_PAID_STEP
        amount_to_next = 0.0
        progress_fraction = 1.0
    else:
        progress_amount = min(
            RESELLER_TRUST_PAID_STEP,
            max(0.0, total_paid - current_threshold),
        )
        amount_to_next = max(0.0, next_threshold - total_paid)
        progress_fraction = progress_amount / RESELLER_TRUST_PAID_STEP
    progress_segments = min(10, max(0, int(progress_fraction * 10)))
    return {
        'level': level,
        'level_count': RESELLER_LEVEL_COUNT,
        'icon': RESELLER_LEVEL_ICONS[level - 1],
        'discount_percent': discount_percent,
        'trust_limit': trust_limit,
        'total_paid': total_paid,
        'current_threshold': current_threshold,
        'next_level': level + 1 if next_threshold is not None else None,
        'next_threshold': next_threshold,
        'amount_to_next': round(amount_to_next, 2),
        'progress_amount': round(progress_amount, 2),
        'progress_fraction': progress_fraction,
        'progress_segments': progress_segments,
        'is_max_level': level == RESELLER_LEVEL_COUNT,
    }


def calculate_reseller_wholesale_price(list_price, record):
    try:
        amount = Decimal(str(list_price))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid reseller list price')
    if not amount.is_finite() or amount < 0:
        raise ValueError('Invalid reseller list price')
    discount = Decimal(str(get_reseller_level_summary(record)['discount_percent']))
    wholesale = amount * (Decimal('1') - (discount / Decimal('100')))
    return float(wholesale.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def get_reseller_available_credit(record):
    data = record or {}
    debt = _safe_float(data.get('debt', 0.0))
    trust_limit = get_reseller_credit_policy(data)['effective_limit']
    return max(0.0, trust_limit - debt)


def get_reseller_credit_policy(record):
    data = record or {}
    outcomes = data.get('credit_outcomes', [])
    if not isinstance(outcomes, list):
        outcomes = []
    outcomes = [item for item in outcomes[-CREDIT_OUTCOME_LIMIT:] if isinstance(item, dict)]
    adverse_weight = sum(
        CREDIT_OUTCOME_WEIGHTS.get(str(item.get('outcome') or ''), 0)
        for item in outcomes
    )
    base_limit = get_reseller_trust_limit(get_reseller_total_paid(data))
    if adverse_weight >= 2:
        mode = 'prepaid_only'
        effective_limit = 0.0
    elif adverse_weight == 1:
        mode = 'half_credit'
        effective_limit = round(base_limit / 2.0, 2)
    else:
        mode = 'credit'
        effective_limit = base_limit
    return {
        'base_limit': base_limit,
        'effective_limit': effective_limit,
        'mode': mode,
        'adverse_weight': adverse_weight,
        'outcomes': [dict(item) for item in outcomes],
    }


def _record_credit_outcome(record, outcome, source, reference_id=None):
    normalized = str(outcome or '')
    if normalized not in CREDIT_OUTCOME_WEIGHTS:
        return False
    history = record.get('credit_outcomes', [])
    if not isinstance(history, list):
        history = []
    reference = str(reference_id or uuid.uuid4().hex)
    references = record.get('credit_outcome_references', [])
    if not isinstance(references, list):
        references = []
    if reference in {str(item) for item in references}:
        return False
    history.append({
        'outcome': normalized,
        'source': str(source or 'unknown'),
        'reference_id': reference,
        'recorded_at': _now_str(),
    })
    record['credit_outcomes'] = history[-CREDIT_OUTCOME_LIMIT:]
    record['credit_outcome_references'] = (references + [reference])[-500:]
    return True


def record_reseller_credit_outcome(user_id, outcome, source, reference_id=None):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                changed = _record_credit_outcome(
                    current,
                    outcome,
                    source,
                    reference_id=reference_id,
                )
                if changed:
                    current = _ensure_reseller_defaults(current)
                    resellers[user_id] = current
                    _write_resellers_file(resellers)
                return changed
        except Exception:
            return False


def can_reseller_add_debt(record, amount):
    data = record or {}
    debt = _safe_float(data.get('debt', 0.0))
    amount_value = _safe_float(amount, 0.0)
    trust_limit = get_reseller_credit_policy(data)['effective_limit']
    return debt + amount_value <= trust_limit, trust_limit, max(0.0, trust_limit - debt)


def _now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def _resellers_lock_path():
    return f"{RESELLERS_FILE}.lock"


@contextmanager
def _resellers_file_lock():
    if _sqlite_managed():
        with database.transaction():
            yield
        return
    os.makedirs(os.path.dirname(RESELLERS_FILE), exist_ok=True)
    lock_file = open(_resellers_lock_path(), 'a')
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _read_resellers_file():
    if _sqlite_managed():
        data = state_store.read_state(RESELLERS_FILE, {})
        return data if isinstance(data, dict) else {}
    if not os.path.exists(RESELLERS_FILE):
        return {}
    with open(RESELLERS_FILE, 'r') as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write_resellers_file(resellers):
    if _sqlite_managed():
        state_store.write_state(
            RESELLERS_FILE,
            resellers if isinstance(resellers, dict) else {},
        )
        return
    os.makedirs(os.path.dirname(RESELLERS_FILE), exist_ok=True)
    tmp_path = f"{RESELLERS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, 'w') as f:
            json.dump(resellers if isinstance(resellers, dict) else {}, f, indent=4)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, RESELLERS_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _resellers_store_exists():
    return state_store.state_exists(RESELLERS_FILE) if _sqlite_managed() else os.path.exists(RESELLERS_FILE)


def _is_removed_config(config):
    return isinstance(config, dict) and bool(config.get('removed_from_vpn'))


def _mark_config_removed(config, cleanup_status):
    tagged = dict(config or {})
    tagged['removed_from_vpn'] = True
    tagged['removal_reason'] = REMOVAL_REASON_BANNED_RESELLER_CLEANUP
    tagged['removal_note'] = REMOVAL_NOTE_BANNED_RESELLER_CLEANUP
    tagged['removed_at'] = _now_str()
    tagged['removed_cleanup_status'] = cleanup_status
    return tagged


def _compute_debt_state(debt, debt_since=None, now=None):
    debt_amount = _safe_float(debt, 0.0)
    if _is_debt_fully_settled(debt_amount):
        return 'active'
    started_at = _parse_time(debt_since)
    age_hours = (
        max(0.0, ((now or datetime.now()) - started_at).total_seconds() / 3600)
        if started_at else 0.0
    )
    if age_hours >= DEBT_SUSPEND_DEADLINE_HOURS:
        return 'suspended'
    if age_hours >= 24.0:
        return 'warning'
    return 'active'


def get_reseller_unlock_amount(debt):
    return max(0.0, _safe_float(debt, 0.0))


def validate_reseller_manual_payment_amount(amount, current_debt):
    try:
        amount_value = round(float(amount), 2)
    except (TypeError, ValueError):
        return False, 0.0, 'invalid'

    debt_value = round(max(0.0, _safe_float(current_debt, 0.0)), 2)
    if amount_value <= 0:
        return False, amount_value, 'invalid'
    if amount_value > debt_value:
        return False, amount_value, 'over_debt'
    return True, amount_value, None


def _ensure_reseller_defaults(record):
    data = dict(record or {})
    data['status'] = data.get('status', 'pending')
    data.setdefault('telegram_username', None)
    data.setdefault('suspended_reason', None)
    data.setdefault('suspended_at', None)
    debt = _safe_float(data.get('debt', 0.0))
    data['debt'] = debt
    data.setdefault('configs', [])
    if not isinstance(data.get('debt_charges'), list):
        data['debt_charges'] = []
    if not isinstance(data.get('debt_allocations'), list):
        data['debt_allocations'] = []
    total_paid = get_reseller_total_paid(data)
    data['total_paid'] = total_paid
    data['trust_limit'] = get_reseller_trust_limit(total_paid)
    data.setdefault('created_at', _now_str())
    data.setdefault('last_payment_at', None)
    data.setdefault('debt_since', None)
    data.setdefault('debt_last_reminded_at', None)
    data.setdefault('debt_last_admin_alert_level', 'none')
    data.setdefault('debt_last_admin_alert_at', None)
    if not isinstance(data.get('credit_outcomes'), list):
        data['credit_outcomes'] = []
    data['credit_outcomes'] = [
        item for item in data['credit_outcomes'][-CREDIT_OUTCOME_LIMIT:]
        if isinstance(item, dict)
    ]
    if not isinstance(data.get('credit_outcome_references'), list):
        data['credit_outcome_references'] = []
    known_outcome_references = [
        str(item.get('reference_id'))
        for item in data['credit_outcomes']
        if item.get('reference_id')
    ]
    data['credit_outcome_references'] = list(dict.fromkeys(
        [str(item) for item in data['credit_outcome_references'][-500:]]
        + known_outcome_references
    ))[-500:]
    if not isinstance(data.get('debt_notification_state'), dict):
        data['debt_notification_state'] = {}
    if not isinstance(data.get('debt_service_action_claims'), dict):
        data['debt_service_action_claims'] = {}
    if not isinstance(data.get('processed_debt_payments'), list):
        data['processed_debt_payments'] = []
    data['processed_debt_payments'] = [
        item for item in data['processed_debt_payments'][-200:]
        if isinstance(item, dict)
    ]
    if not isinstance(data.get('pending_wholesale_credits'), list):
        data['pending_wholesale_credits'] = []
    data['pending_wholesale_credits'] = [
        item for item in data['pending_wholesale_credits']
        if isinstance(item, dict)
    ]
    data.setdefault('debt_cycle_id', None)
    data.setdefault('debt_cycle_late_recorded', False)
    data.setdefault('debt_cycle_default_recorded', False)
    data.setdefault('debt_services_held_at', None)
    data.setdefault('debt_services_removed_at', None)
    data.setdefault('debt_service_hold_due', False)
    data.setdefault('debt_service_remove_due', False)
    data.setdefault('debt_recovery_pending', False)
    try:
        presented_level = int(data.get('last_presented_reseller_level', 0) or 0)
    except (TypeError, ValueError):
        presented_level = 0
    data['last_presented_reseller_level'] = min(
        RESELLER_LEVEL_COUNT,
        max(0, presented_level),
    )
    if not isinstance(data.get('reseller_level_presentation_claim'), dict):
        data['reseller_level_presentation_claim'] = None

    debt_fully_settled = _is_debt_fully_settled(debt)
    if not debt_fully_settled and not data.get('debt_since'):
        data['debt_since'] = _now_str()
    if not debt_fully_settled and not data.get('debt_cycle_id'):
        data['debt_cycle_id'] = uuid.uuid4().hex
        data['debt_cycle_late_recorded'] = False
        data['debt_cycle_default_recorded'] = False
        data['debt_services_held_at'] = None
        data['debt_services_removed_at'] = None
        data['debt_service_hold_due'] = False
        data['debt_service_remove_due'] = False
        data['debt_service_action_claims'] = {}
        data['debt_notification_state'] = {}
    if debt_fully_settled:
        data['debt_since'] = None
        data['debt_last_reminded_at'] = None
        data['debt_last_admin_alert_level'] = 'none'

    data['debt_state'] = _compute_debt_state(debt, data.get('debt_since'))
    return data


def _restore_suspended_if_debt_fully_settled(data):
    if (
        _is_debt_fully_settled(data.get('debt', 0.0))
        and data.get('status') == 'suspended'
        and data.get('suspended_reason') in {
            SUSPENDED_REASON_DEBT,
            SUSPENDED_REASON_UNBAN_GRACE,
        }
    ):
        data['status'] = 'approved'
        data['suspended_reason'] = None
        data['suspended_at'] = None
        data['debt_recovery_pending'] = True
    return data


def _mark_policy_restore_due_if_needed(data):
    if not _is_debt_fully_settled(data.get('debt', 0.0)):
        return data
    if any(
        isinstance(config, dict)
        and config.get('debt_policy_blocked')
        and not _is_removed_config(config)
        for config in data.get('configs', [])
    ):
        data['debt_restore_services_due'] = True
        data['debt_recovery_pending'] = True
    return data


def _finish_debt_cycle(data):
    data['debt_cycle_id'] = None
    data['debt_cycle_late_recorded'] = False
    data['debt_cycle_default_recorded'] = False
    data['debt_service_hold_due'] = False
    data['debt_service_remove_due'] = False
    data['debt_notification_state'] = {}
    return data


def load_resellers():
    with reseller_lock:
        try:
            with _resellers_file_lock():
                return _read_resellers_file()
        except Exception:
            pass
        return {}


def save_resellers(resellers):
    with reseller_lock:
        with _resellers_file_lock():
            _write_resellers_file(resellers)


def get_reseller_data(user_id):
    resellers = load_resellers()
    data = resellers.get(str(user_id))
    if not data:
        return None
    return _ensure_reseller_defaults(data)


def get_all_resellers():
    resellers = load_resellers()
    normalized = {}
    for rid, data in resellers.items():
        normalized[str(rid)] = _ensure_reseller_defaults(data)
    return normalized


def claim_reseller_level_presentation(user_id, lease_seconds=None):
    user_id = str(user_id)
    lease = max(
        1.0,
        _safe_float(
            lease_seconds,
            RESELLER_LEVEL_PRESENTATION_LEASE_SECONDS,
        ),
    )
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return None
                current = _ensure_reseller_defaults(resellers[user_id])
                summary = get_reseller_level_summary(current)
                presented_level = current.get('last_presented_reseller_level', 0)
                if presented_level >= summary['level']:
                    return None

                existing_claim = current.get('reseller_level_presentation_claim')
                if isinstance(existing_claim, dict):
                    claimed_at = _parse_time(existing_claim.get('claimed_at'))
                    claim_age = lease
                    if claimed_at is not None:
                        elapsed = (datetime.now() - claimed_at).total_seconds()
                        claim_age = elapsed if elapsed >= 0 else lease
                    try:
                        existing_level = int(existing_claim.get('level', 0) or 0)
                    except (TypeError, ValueError):
                        existing_level = 0
                    if claim_age < lease and existing_level >= summary['level']:
                        return None

                claim = {
                    'id': uuid.uuid4().hex,
                    'level': summary['level'],
                    'from_level': presented_level,
                    'kind': 'introduction' if presented_level == 0 else 'level_up',
                    'claimed_at': _now_str(),
                }
                current['reseller_level_presentation_claim'] = claim
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return {**claim, 'summary': summary}
        except Exception:
            return None


def complete_reseller_level_presentation(user_id, claim_id):
    return _finish_reseller_level_presentation(user_id, claim_id, completed=True)


def release_reseller_level_presentation(user_id, claim_id):
    return _finish_reseller_level_presentation(user_id, claim_id, completed=False)


def _finish_reseller_level_presentation(user_id, claim_id, completed):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                claim = current.get('reseller_level_presentation_claim')
                if not isinstance(claim, dict) or claim.get('id') != str(claim_id):
                    return False
                if completed:
                    current['last_presented_reseller_level'] = max(
                        current.get('last_presented_reseller_level', 0),
                        min(
                            RESELLER_LEVEL_COUNT,
                            int(claim.get('level', 0) or 0),
                        ),
                    )
                current['reseller_level_presentation_claim'] = None
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def update_reseller_status(user_id, status, telegram_username=None, suspended_reason=None):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()

                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                previous_status = current.get('status')
                previous_reason = current.get('suspended_reason')
                current['status'] = status
                if status == 'suspended':
                    current['suspended_reason'] = suspended_reason
                    if previous_status != 'suspended' or previous_reason != suspended_reason or not current.get('suspended_at'):
                        current['suspended_at'] = _now_str()
                else:
                    current['suspended_reason'] = None
                    current['suspended_at'] = None
                current = _restore_suspended_if_debt_fully_settled(current)
                if telegram_username is not None:
                    username_clean = str(telegram_username).strip().lstrip('@')
                    current['telegram_username'] = username_clean or None
                resellers[user_id] = _ensure_reseller_defaults(current)

                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def add_reseller_debt(user_id, amount, config_data):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False
                resellers = _read_resellers_file()

                if user_id in resellers:
                    current = _ensure_reseller_defaults(resellers[user_id])
                    order_id = str((config_data or {}).get('retail_order_id') or '')
                    if order_id and any(
                        isinstance(item, dict) and str(item.get('retail_order_id') or '') == order_id
                        for item in current.get('configs', [])
                    ):
                        return True
                    before = _safe_float(current.get('debt', 0.0))
                    amount_value = _money_value(amount)
                    if amount_value <= 0:
                        return False

                    if 'configs' not in current:
                        current['configs'] = []

                    config_data = dict(config_data or {})
                    charge_id = order_id or f"config-{uuid.uuid4().hex}"
                    config_data['debt_charge_id'] = charge_id
                    config_data['timestamp'] = _now_str()
                    charge = _add_debt_charge(
                        current,
                        amount_value,
                        'config',
                        reference_id=charge_id,
                        metadata={
                            'username': config_data.get('username'),
                            'server_id': config_data.get('server_id'),
                            'retail_order_id': order_id or None,
                        },
                    )
                    if charge is None:
                        return False
                    current['configs'].append(config_data)
                    if _is_debt_fully_settled(before) and not _is_debt_fully_settled(current['debt']):
                        current['debt_since'] = _now_str()

                    current = _ensure_reseller_defaults(current)
                    resellers[user_id] = current

                    _write_resellers_file(resellers)
                    _update_recruitment_milestone(user_id, current)
                    return True
                return False
        except Exception:
            return False


def record_funded_reseller_config(user_id, wholesale_amount, config_data):
    """Record a reseller config whose wholesale cost was paid at checkout."""
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                record = dict(config_data or {})
                order_id = str(record.get('retail_order_id') or '')
                if order_id and any(
                    isinstance(item, dict) and str(item.get('retail_order_id') or '') == order_id
                    for item in current.get('configs', [])
                ):
                    return True
                record.setdefault('price', _safe_float(wholesale_amount, 0.0))
                record.setdefault('timestamp', _now_str())
                record['funded_at_checkout'] = True
                current.setdefault('configs', []).append(record)
                current['total_paid'] = get_reseller_total_paid(current) + _safe_float(wholesale_amount, 0.0)
                current['last_payment_at'] = _now_str()
                current = _ensure_reseller_defaults(current)
                resellers[user_id] = current
                _write_resellers_file(resellers)
                _update_recruitment_milestone(user_id, current)
                return True
        except Exception:
            return False


def record_funded_reseller_renewal(user_id, username, wholesale_amount, renewal_data, server_id=None):
    """Record a renewal whose wholesale cost was paid at crypto checkout."""
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                configs = current.get('configs', [])
                target = next((item for item in configs if isinstance(item, dict)
                               and str(item.get('username', '')).lower() == str(username).lower()
                               and (not server_id or not item.get('server_id') or str(item.get('server_id')) == str(server_id))), None)
                if target is None:
                    return False
                record = dict(renewal_data or {})
                order_id = str(record.get('retail_order_id') or '')
                if order_id and any(
                    isinstance(item, dict) and str(item.get('retail_order_id') or '') == order_id
                    for item in target.get('renewals', [])
                ):
                    return True
                record.setdefault('price', _safe_float(wholesale_amount, 0.0))
                record.setdefault('timestamp', _now_str())
                record['funded_at_checkout'] = True
                target.setdefault('renewals', []).append(record)
                target['cleanup_status'] = 'renewed'
                current['total_paid'] = get_reseller_total_paid(current) + _safe_float(wholesale_amount, 0.0)
                current['last_payment_at'] = _now_str()
                current = _ensure_reseller_defaults(current)
                resellers[user_id] = current
                _write_resellers_file(resellers)
                _update_recruitment_milestone(user_id, current)
                return True
        except Exception:
            return False


def reseller_config_is_recorded(user_id, username, server_id=None):
    user_id = str(user_id)
    target_username = str(username or '').strip().lower()
    target_server_id = str(server_id or '').strip()
    if not target_username:
        return False

    with reseller_lock:
        try:
            with _resellers_file_lock():
                reseller_data = _read_resellers_file().get(user_id)
        except Exception:
            return False

    configs = reseller_data.get('configs', []) if isinstance(reseller_data, dict) else []
    if not isinstance(configs, list):
        return False
    for config in configs:
        if not isinstance(config, dict):
            continue
        username_value = str(config.get('username') or '').strip().lower()
        server_id_value = str(config.get('server_id') or '').strip()
        if username_value == target_username and (not target_server_id or server_id_value == target_server_id):
            return True
    return False


def add_reseller_renewal_debt(user_id, username, amount, renewal_data, server_id=None):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False
                resellers = _read_resellers_file()

                if user_id not in resellers:
                    return False

                current = _ensure_reseller_defaults(resellers[user_id])
                configs = current.get('configs', [])
                if not isinstance(configs, list):
                    return False

                target_index = None
                target_username = str(username or '').strip().lower()
                for index, config in enumerate(configs):
                    if not isinstance(config, dict) or _is_removed_config(config):
                        continue
                    if str(config.get('username') or '').strip().lower() != target_username:
                        continue
                    if server_id and config.get('server_id') and str(config.get('server_id')) != str(server_id):
                        continue
                    target_index = index
                    break

                if target_index is None:
                    return False

                order_id = str((renewal_data or {}).get('retail_order_id') or '')
                if order_id and any(
                    isinstance(item, dict) and str(item.get('retail_order_id') or '') == order_id
                    for item in configs[target_index].get('renewals', [])
                ):
                    return True

                before = _safe_float(current.get('debt', 0.0))
                amount_value = _money_value(amount)
                if amount_value <= 0:
                    return False
                renewal_record = dict(renewal_data or {})
                renewal_record.setdefault('timestamp', _now_str())
                renewal_record.setdefault('price', amount_value)
                charge_id = order_id or str(renewal_record.get('reservation_id') or f"renewal-{uuid.uuid4().hex}")
                renewal_record['debt_charge_id'] = charge_id
                charge = _add_debt_charge(
                    current,
                    amount_value,
                    'renewal',
                    reference_id=charge_id,
                    metadata={
                        'username': username,
                        'server_id': server_id,
                        'retail_order_id': order_id or None,
                        'reservation_id': renewal_record.get('reservation_id'),
                    },
                )
                if charge is None:
                    return False
                configs[target_index].setdefault('renewals', [])
                if not isinstance(configs[target_index]['renewals'], list):
                    configs[target_index]['renewals'] = []
                configs[target_index]['renewals'].append(renewal_record)
                configs[target_index]['cleanup_status'] = 'renewed'
                configs[target_index]['cleanup_error'] = None
                if renewal_record.get('after_state') is not None:
                    configs[target_index]['cleanup_last_state'] = renewal_record.get('after_state')

                if _is_debt_fully_settled(before) and not _is_debt_fully_settled(current['debt']):
                    current['debt_since'] = _now_str()

                current = _ensure_reseller_defaults(current)
                resellers[user_id] = current

                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def reserve_reseller_renewal(
    user_id,
    username,
    amount,
    renewal_data,
    server_id=None,
    funded=False,
    enforce_credit=True,
):
    """Atomically record one future renewal and its debt or funded charge."""
    user_id = str(user_id)
    target_username = str(username or '').strip().lower()
    amount_value = _money_value(amount)
    if not target_username or amount_value <= 0:
        return False, {'reason': 'renewal_ineligible_missing'}
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False, {'reason': 'reseller_missing'}
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False, {'reason': 'reseller_missing'}
                current = _ensure_reseller_defaults(resellers[user_id])
                configs = current.get('configs', [])
                target_index = None
                for index, config in enumerate(configs if isinstance(configs, list) else []):
                    if not isinstance(config, dict) or _is_removed_config(config):
                        continue
                    if str(config.get('username') or '').strip().lower() != target_username:
                        continue
                    if server_id and config.get('server_id') and str(config.get('server_id')) != str(server_id):
                        continue
                    target_index = index
                    break
                if target_index is None:
                    return False, {'reason': 'renewal_ineligible_missing'}
                renewals = configs[target_index].setdefault('renewals', [])
                if not isinstance(renewals, list):
                    renewals = []
                    configs[target_index]['renewals'] = renewals
                active = next(
                    (
                        item
                        for item in renewals
                        if isinstance(item, dict)
                        and item.get('renewal_mode') == 'reserved'
                        and item.get('renewal_status') in {'reserved', 'processing', 'attention'}
                    ),
                    None,
                )
                if active is not None:
                    requested_id = str((renewal_data or {}).get('reservation_id') or (renewal_data or {}).get('retail_order_id') or '')
                    if requested_id and str(active.get('reservation_id') or '') == requested_id:
                        return True, dict(active)
                    return False, {'reason': 'renewal_already_reserved', 'reservation': dict(active)}

                record = dict(renewal_data or {})
                reservation_id = str(record.get('reservation_id') or record.get('retail_order_id') or uuid.uuid4().hex)
                record['reservation_id'] = reservation_id
                record.setdefault('retail_order_id', reservation_id if record.get('origin_bot_id') else None)
                record.setdefault('timestamp', _now_str())
                record.setdefault('renewal_reserved_at', record['timestamp'])
                record.setdefault('price', amount_value)
                record['renewal_mode'] = 'reserved'
                record['renewal_status'] = 'reserved'
                record.setdefault('renewal_attempts', 0)

                if funded:
                    record['funded_at_checkout'] = True
                    current['total_paid'] = get_reseller_total_paid(current) + amount_value
                    current['last_payment_at'] = _now_str()
                else:
                    if enforce_credit:
                        can_add, trust_limit, available = can_reseller_add_debt(current, amount_value)
                        if not can_add:
                            return False, {
                                'reason': 'credit_unavailable',
                                'trust_limit': trust_limit,
                                'available_credit': available,
                            }
                    before = _safe_float(current.get('debt', 0.0))
                    charge_id = str(record.get('debt_charge_id') or f"renewal-{reservation_id}")
                    record['debt_charge_id'] = charge_id
                    charge = _add_debt_charge(
                        current,
                        amount_value,
                        'reserved_renewal',
                        reference_id=charge_id,
                        metadata={
                            'reservation_id': reservation_id,
                            'username': username,
                            'server_id': server_id,
                            'retail_order_id': record.get('retail_order_id'),
                        },
                    )
                    if charge is None:
                        return False, {'reason': 'renewal_accounting_failed'}
                    if _is_debt_fully_settled(before) and not _is_debt_fully_settled(current['debt']):
                        current['debt_since'] = _now_str()

                renewals.append(record)
                current = _ensure_reseller_defaults(current)
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return True, dict(record)
        except Exception:
            return False, {'reason': 'renewal_accounting_failed'}


def get_reseller_renewal_reservation(user_id, reservation_id):
    reseller_data = get_reseller_data(user_id) or {}
    for config_index, config in enumerate(reseller_data.get('configs', [])):
        if not isinstance(config, dict):
            continue
        for renewal_index, renewal in enumerate(config.get('renewals', [])):
            if isinstance(renewal, dict) and str(renewal.get('reservation_id') or '') == str(reservation_id):
                return {
                    'reseller_id': str(user_id),
                    'config_index': config_index,
                    'renewal_index': renewal_index,
                    'config': dict(config),
                    'reservation': dict(renewal),
                    'reseller': reseller_data,
                }
    return None


def list_reseller_renewal_reservations():
    result = []
    for reseller_id, reseller_data in get_all_resellers().items():
        for config_index, config in enumerate(reseller_data.get('configs', [])):
            if not isinstance(config, dict) or _is_removed_config(config):
                continue
            for renewal_index, renewal in enumerate(config.get('renewals', [])):
                if not isinstance(renewal, dict):
                    continue
                if renewal.get('renewal_mode') != 'reserved':
                    continue
                if renewal.get('renewal_status') not in {'reserved', 'processing', 'attention'}:
                    continue
                result.append({
                    'reseller_id': str(reseller_id),
                    'config_index': config_index,
                    'renewal_index': renewal_index,
                    'config': dict(config),
                    'reservation': dict(renewal),
                    'reseller': reseller_data,
                })
    return result


def claim_reseller_renewal_reservation(
    user_id,
    reservation_id,
    now=None,
    force=False,
    lease_seconds=600,
):
    user_id = str(user_id)
    current_time = now or datetime.now()
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                for config in current.get('configs', []):
                    if not isinstance(config, dict):
                        continue
                    for reservation in config.get('renewals', []):
                        if not isinstance(reservation, dict) or str(reservation.get('reservation_id') or '') != str(reservation_id):
                            continue
                        status = reservation.get('renewal_status')
                        if status == 'processing':
                            claimed_at = _parse_time(reservation.get('renewal_claimed_at'))
                            if claimed_at is not None and 0 <= (current_time - claimed_at).total_seconds() < lease_seconds:
                                return None
                        elif status == 'attention':
                            next_attempt = _parse_time(reservation.get('renewal_next_attempt_at'))
                            if not force and (
                                reservation.get('renewal_attention_reason') == 'external_renewal'
                                or (next_attempt is not None and next_attempt > current_time)
                            ):
                                return None
                        elif status != 'reserved':
                            return None
                        claim_id = uuid.uuid4().hex
                        reservation['renewal_processing_from'] = status
                        reservation['renewal_status'] = 'processing'
                        reservation['renewal_claim_id'] = claim_id
                        reservation['renewal_claimed_at'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                        resellers[user_id] = _ensure_reseller_defaults(current)
                        _write_resellers_file(resellers)
                        return {'claim_id': claim_id, 'reservation': dict(reservation), 'config': dict(config), 'reseller': current}
                return None
        except Exception:
            return None


def finish_reseller_renewal_reservation(
    user_id,
    reservation_id,
    claim_id,
    status,
    fields=None,
    now=None,
    retry=False,
):
    if status not in {'reserved', 'attention', 'applied'}:
        return False
    user_id = str(user_id)
    current_time = now or datetime.now()
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                for config in current.get('configs', []):
                    if not isinstance(config, dict):
                        continue
                    for reservation in config.get('renewals', []):
                        if not isinstance(reservation, dict) or str(reservation.get('reservation_id') or '') != str(reservation_id):
                            continue
                        if reservation.get('renewal_claim_id') != str(claim_id):
                            return False
                        reservation.update(dict(fields or {}))
                        reservation['renewal_status'] = status
                        reservation.pop('renewal_claim_id', None)
                        reservation.pop('renewal_claimed_at', None)
                        reservation.pop('renewal_processing_from', None)
                        if status == 'applied':
                            reservation['renewal_applied_at'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                            reservation.pop('renewal_attention_reason', None)
                            reservation.pop('renewal_last_error', None)
                            reservation.pop('renewal_next_attempt_at', None)
                            reservation.pop('renewal_api_error', None)
                            reservation.pop('renewal_api_http_status', None)
                            config['cleanup_status'] = 'renewed'
                            config['cleanup_error'] = None
                            if reservation.get('after_state') is not None:
                                config['cleanup_last_state'] = reservation.get('after_state')
                        elif status == 'reserved':
                            reservation.pop('renewal_attention_reason', None)
                            reservation.pop('renewal_last_error', None)
                            reservation.pop('renewal_next_attempt_at', None)
                            reservation.pop('renewal_api_error', None)
                            reservation.pop('renewal_api_http_status', None)
                        elif retry:
                            reservation['renewal_attempts'] = int(reservation.get('renewal_attempts', 0) or 0) + 1
                            reservation['renewal_next_attempt_at'] = (
                                current_time + timedelta(hours=1)
                            ).strftime('%Y-%m-%d %H:%M:%S')
                        resellers[user_id] = _ensure_reseller_defaults(current)
                        _write_resellers_file(resellers)
                        return True
                return False
        except Exception:
            return False


def refresh_reseller_renewal_baseline(user_id, reservation_id, user_data):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                for config in current.get('configs', []):
                    for reservation in config.get('renewals', []) if isinstance(config, dict) else []:
                        if isinstance(reservation, dict) and str(reservation.get('reservation_id') or '') == str(reservation_id):
                            reservation['renewal_baseline'] = dict(user_data or {})
                            reservation['renewal_status'] = 'reserved'
                            reservation['renewal_reviewed_at'] = _now_str()
                            reservation.pop('renewal_attention_reason', None)
                            reservation.pop('renewal_last_error', None)
                            reservation.pop('renewal_next_attempt_at', None)
                            resellers[user_id] = _ensure_reseller_defaults(current)
                            _write_resellers_file(resellers)
                            return True
                return False
        except Exception:
            return False


def mark_reseller_renewal_alerted(user_id, reservation_id, now=None, audience=None):
    user_id = str(user_id)
    timestamp = (now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                for config in current.get('configs', []):
                    for reservation in config.get('renewals', []) if isinstance(config, dict) else []:
                        if isinstance(reservation, dict) and str(reservation.get('reservation_id') or '') == str(reservation_id):
                            if audience in {'operator', 'buyer'}:
                                reservation[f'renewal_last_{audience}_alert_at'] = timestamp
                            else:
                                reservation['renewal_last_alert_at'] = timestamp
                                reservation['renewal_last_operator_alert_at'] = timestamp
                                reservation['renewal_last_buyer_alert_at'] = timestamp
                            resellers[user_id] = _ensure_reseller_defaults(current)
                            _write_resellers_file(resellers)
                            return True
                return False
        except Exception:
            return False


def sync_reseller_renewal_reservation(user_id, reservation_id, status, fields=None):
    """Idempotently mirror hosted payment fulfillment into reseller history."""
    if status not in {'reserved', 'attention', 'applied'}:
        return False
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                for config in current.get('configs', []):
                    for reservation in config.get('renewals', []) if isinstance(config, dict) else []:
                        if not isinstance(reservation, dict) or str(reservation.get('reservation_id') or '') != str(reservation_id):
                            continue
                        reservation.update(dict(fields or {}))
                        reservation['renewal_status'] = status
                        if status == 'applied':
                            reservation.setdefault('renewal_applied_at', _now_str())
                            reservation.pop('renewal_attention_reason', None)
                            reservation.pop('renewal_last_error', None)
                            reservation.pop('renewal_next_attempt_at', None)
                            reservation.pop('renewal_api_error', None)
                            reservation.pop('renewal_api_http_status', None)
                            config['cleanup_status'] = 'renewed'
                            config['cleanup_error'] = None
                            if reservation.get('after_state') is not None:
                                config['cleanup_last_state'] = reservation.get('after_state')
                        elif status == 'reserved':
                            reservation.pop('renewal_attention_reason', None)
                            reservation.pop('renewal_last_error', None)
                            reservation.pop('renewal_next_attempt_at', None)
                            reservation.pop('renewal_api_error', None)
                            reservation.pop('renewal_api_http_status', None)
                        resellers[user_id] = _ensure_reseller_defaults(current)
                        _write_resellers_file(resellers)
                        return True
                return False
        except Exception:
            return False


def clear_reseller_debt(user_id):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False
                resellers = _read_resellers_file()

                if user_id in resellers:
                    current = _ensure_reseller_defaults(resellers[user_id])
                    _ensure_debt_charge_ledger(current)
                    _allocate_debt_fifo(
                        current,
                        current.get('debt', 0.0),
                        kind='admin_clear',
                    )
                    current['debt'] = 0.0
                    current = _restore_suspended_if_debt_fully_settled(current)
                    current = _mark_policy_restore_due_if_needed(current)
                    current = _finish_debt_cycle(current)
                    current = _ensure_reseller_defaults(current)
                    resellers[user_id] = current
                    _write_resellers_file(resellers)
                    return True
                return False
        except Exception:
            return False


def set_reseller_debt(user_id, amount):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False
                resellers = _read_resellers_file()

                if user_id in resellers:
                    current = _ensure_reseller_defaults(resellers[user_id])
                    previous_debt = _safe_float(current.get('debt', 0.0))
                    new_debt = _money_value(amount)
                    _ensure_debt_charge_ledger(current)
                    if new_debt > previous_debt:
                        _add_debt_charge(
                            current,
                            new_debt - previous_debt,
                            'admin_adjustment',
                            reference_id=f"admin-adjustment-{uuid.uuid4().hex}",
                        )
                    elif new_debt < previous_debt:
                        _allocate_debt_fifo(
                            current,
                            previous_debt - new_debt,
                            kind='admin_adjustment_credit',
                        )
                    current['debt'] = new_debt

                    if _is_debt_fully_settled(previous_debt) and not _is_debt_fully_settled(current['debt']):
                        current['debt_since'] = _now_str()
                    if _is_debt_fully_settled(current['debt']):
                        current['debt_since'] = None
                    current = _restore_suspended_if_debt_fully_settled(current)
                    if _is_debt_fully_settled(current['debt']):
                        current = _mark_policy_restore_due_if_needed(current)
                        current = _finish_debt_cycle(current)

                    current = _ensure_reseller_defaults(current)
                    resellers[user_id] = current
                    _write_resellers_file(resellers)
                    return True
                return False
        except Exception:
            return False


def delete_reseller(user_id):
    """Delete a reseller record from the system."""
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False
                resellers = _read_resellers_file()

                if user_id not in resellers:
                    return False

                del resellers[user_id]
                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def get_banned_reseller_cleanup_candidates(reseller_data):
    """Return reseller-created customer configs eligible for banned cleanup."""
    data = _ensure_reseller_defaults(reseller_data or {})
    configs = data.get('configs', [])
    if not isinstance(configs, list):
        configs = []

    last_payment_at = data.get('last_payment_at')
    last_payment_dt = _parse_time(last_payment_at)
    candidates = []

    for index, config in enumerate(configs):
        if not isinstance(config, dict):
            continue
        if _is_removed_config(config):
            continue
        username = str(config.get('username') or '').strip()
        if not username:
            continue
        timestamp = config.get('timestamp')
        timestamp_dt = _parse_time(timestamp)
        if last_payment_dt and (not timestamp_dt or timestamp_dt <= last_payment_dt):
            continue
        candidates.append({
            'config_index': index,
            'username': username,
            'customer_name': str(config.get('customer_name') or '').strip(),
            'timestamp': timestamp or 'N/A',
            'price': _safe_float(config.get('price', 0.0)),
            'server_id': config.get('server_id'),
        })

    return candidates


def cleanup_banned_reseller_users(user_id, multi_api):
    """Delete unpaid customer configs for a banned reseller and tag local history."""
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False, {'reason': 'Reseller not found'}
                resellers = _read_resellers_file()
        except Exception:
            return False, {'reason': 'Unable to load resellers'}

        if user_id not in resellers:
            return False, {'reason': 'Reseller not found'}

        stored_reseller = resellers[user_id]
        had_explicit_total_paid = isinstance(stored_reseller, dict) and 'total_paid' in stored_reseller
        current = _ensure_reseller_defaults(stored_reseller)
        if current.get('status') != 'banned':
            return False, {'reason': 'Cleanup is only available for banned resellers'}

        candidates = get_banned_reseller_cleanup_candidates(current)
        if not candidates:
            return True, {
                'deleted': [],
                'already_missing': [],
                'failed': [],
                'removed_count': 0,
                'tagged_count': 0,
                'removed_value': 0.0,
                'remaining_debt': _safe_float(current.get('debt', 0.0)),
                'remaining_configs': len(current.get('configs', []) or []),
                'last_payment_at': current.get('last_payment_at'),
            }

        deleted = []
        already_missing = []
        failed = []
        tagged_status_by_index = {}

        for candidate in candidates:
            username = candidate['username']
            api_client, live_user = multi_api.find_user(username, preferred_server_id=candidate.get('server_id'))
            if api_client is None or live_user is None:
                already_missing.append(candidate)
                tagged_status_by_index[candidate['config_index']] = REMOVAL_STATUS_ALREADY_MISSING
                continue

            result = api_client.delete_user(username)
            if result is None:
                failed.append(candidate)
                continue

            deleted.append(candidate)
            tagged_status_by_index[candidate['config_index']] = REMOVAL_STATUS_DELETED_FROM_VPN

        removed_value = sum(
            _safe_float(candidate.get('price', 0.0))
            for candidate in candidates
            if candidate.get('config_index') in tagged_status_by_index
        )
        failed_value = sum(_safe_float(candidate.get('price', 0.0)) for candidate in failed)

        configs = current.get('configs', [])
        if not isinstance(configs, list):
            configs = []
        current['configs'] = []
        for index, config in enumerate(configs):
            if index in tagged_status_by_index and isinstance(config, dict):
                current['configs'].append(_mark_config_removed(config, tagged_status_by_index[index]))
            else:
                current['configs'].append(config)
        previous_debt = _safe_float(current.get('debt', 0.0))
        target_debt = max(failed_value, previous_debt - removed_value)
        _ensure_debt_charge_ledger(current)
        _allocate_debt_fifo(
            current,
            max(0.0, previous_debt - target_debt),
            kind='banned_cleanup_writeoff',
        )
        current['debt'] = target_debt
        if not had_explicit_total_paid:
            current.pop('total_paid', None)
            current.pop('trust_limit', None)
        current = _ensure_reseller_defaults(current)
        try:
            with _resellers_file_lock():
                latest_resellers = _read_resellers_file()
                latest_resellers[user_id] = current
                _write_resellers_file(latest_resellers)
        except Exception:
            return False, {'reason': 'Unable to save cleanup result'}

        return True, {
            'deleted': deleted,
            'already_missing': already_missing,
            'failed': failed,
            'removed_count': len(tagged_status_by_index),
            'tagged_count': len(tagged_status_by_index),
            'removed_value': removed_value,
            'remaining_debt': _safe_float(current.get('debt', 0.0)),
            'remaining_configs': len(current.get('configs', []) or []),
            'last_payment_at': current.get('last_payment_at'),
        }


def _debt_service_charge_sources(config):
    sources = {}
    if not isinstance(config, dict):
        return sources
    charge_id = str(config.get('debt_charge_id') or '')
    if charge_id:
        sources[charge_id] = {
            'days': config.get('days'),
            'gb': config.get('gb'),
            'unlimited': bool(config.get('unlimited', False)),
            'started_at': config.get('timestamp'),
            'kind': 'config',
        }
    for renewal in config.get('renewals', []) if isinstance(config.get('renewals'), list) else []:
        if not isinstance(renewal, dict):
            continue
        renewal_charge_id = str(renewal.get('debt_charge_id') or '')
        if not renewal_charge_id:
            continue
        sources[renewal_charge_id] = {
            'days': renewal.get('days'),
            'gb': renewal.get('gb') or renewal.get('plan_gb'),
            'unlimited': bool(renewal.get('unlimited', False)),
            'started_at': renewal.get('renewal_applied_at') or renewal.get('timestamp'),
            'kind': 'renewal',
            'activated': renewal.get('renewal_status') == 'applied' or not renewal.get('renewal_mode'),
        }
    return sources


def get_reseller_debt_service_candidates(reseller_data):
    data = _ensure_reseller_defaults(reseller_data or {})
    _ensure_debt_charge_ledger(data)
    charges = {
        str(charge.get('id') or ''): charge
        for charge in data.get('debt_charges', [])
        if isinstance(charge, dict) and _charge_outstanding(charge) > DEBT_CHARGE_EPSILON
    }
    candidates = []
    linked_ids = set()
    for index, config in enumerate(data.get('configs', [])):
        if not isinstance(config, dict) or _is_removed_config(config):
            continue
        sources = _debt_service_charge_sources(config)
        outstanding_ids = [charge_id for charge_id in sources if charge_id in charges]
        if not outstanding_ids:
            continue
        linked_ids.update(outstanding_ids)
        candidates.append({
            'config_index': index,
            'username': str(config.get('username') or '').strip(),
            'server_id': config.get('server_id'),
            'charge_ids': outstanding_ids,
            'charge_sources': sources,
        })
    manual_review = [
        charge_id for charge_id in charges
        if charge_id not in linked_ids
    ]
    return candidates, manual_review


def _live_usage_snapshot(live, held_at):
    live = live if isinstance(live, dict) else {}
    used_bytes = max(
        0,
        int(_safe_float(live.get('upload_bytes'), 0))
        + int(_safe_float(live.get('download_bytes'), 0)),
    )
    quota_bytes = max(0, int(_safe_float(live.get('max_download_bytes'), 0)))
    return {
        'held_at': held_at,
        'blocked_before_policy': bool(live.get('blocked', False)),
        'status_before_policy': live.get('status'),
        'account_creation_date': live.get('account_creation_date'),
        'expiration_days': live.get('expiration_days'),
        'used_bytes': used_bytes,
        'quota_bytes': quota_bytes,
    }


def _exact_debt_service_lookup(multi_api, username, server_id):
    if not username or not server_id:
        return None, None, {'status': 'unavailable', 'error': 'mapping_incomplete'}
    finder = getattr(multi_api, 'find_user_on_server', None)
    if callable(finder):
        return finder(username, str(server_id))
    client, live = multi_api.find_user(username, preferred_server_id=server_id)
    if client is None or live is None:
        return None, None, {'status': 'missing', 'error': None}
    if str(getattr(client, 'server_id', server_id)) != str(server_id):
        return None, None, {'status': 'unavailable', 'error': 'server_mismatch'}
    return client, live, {'status': 'found', 'error': None}


def _prorated_collectible(charge, source, snapshot):
    if not isinstance(charge, dict) or not isinstance(source, dict) or not isinstance(snapshot, dict):
        return None
    if source.get('kind') == 'renewal' and not source.get('activated', True):
        used_fraction = 0.0
        time_fraction = 0.0
        traffic_fraction = 0.0
    else:
        duration_days = _safe_float(source.get('days'), 0.0)
        started_at = _parse_time(source.get('started_at'))
        held_at = _parse_time(snapshot.get('held_at'))
        if duration_days <= 0 or started_at is None or held_at is None:
            return None
        active_hours = max(0.0, (held_at - started_at).total_seconds() / 3600)
        time_fraction = min(1.0, active_hours / (duration_days * 24.0))
        traffic_fraction = 0.0
        if not source.get('unlimited'):
            quota_bytes = _safe_float(snapshot.get('quota_bytes'), 0.0)
            if quota_bytes <= 0:
                try:
                    quota_bytes = float(source.get('gb')) * 1024 ** 3
                except (TypeError, ValueError):
                    quota_bytes = 0.0
            if quota_bytes > 0:
                traffic_fraction = min(
                    1.0,
                    max(0.0, _safe_float(snapshot.get('used_bytes'), 0.0)) / quota_bytes,
                )
        used_fraction = min(1.0, max(time_fraction, traffic_fraction))
    original = _money_value(charge.get('original_amount', charge.get('amount', 0.0)))
    outstanding = _charge_outstanding(charge)
    already_paid = max(0.0, original - outstanding)
    used_total = round(original * used_fraction, 2)
    collectible = round(max(0.0, min(outstanding, used_total - already_paid)), 2)
    return {
        'time_fraction': time_fraction,
        'traffic_fraction': traffic_fraction,
        'used_fraction': used_fraction,
        'used_total': used_total,
        'already_paid': already_paid,
        'collectible': collectible,
        'writeoff': round(max(0.0, outstanding - collectible), 2),
    }


def process_reseller_debt_service_action(user_id, multi_api, action):
    """Serialize external debt actions against settlement and policy updates."""
    with reseller_lock:
        return _process_reseller_debt_service_action(user_id, multi_api, action)


def _process_reseller_debt_service_action(user_id, multi_api, action):
    """Apply a retry-safe hold, removal, or restoration for debt-linked users."""
    user_id = str(user_id)
    if action not in {'hold', 'remove', 'restore'}:
        return False, {'reason': 'invalid_action'}
    initial = get_reseller_data(user_id)
    if not initial:
        return False, {'reason': 'reseller_missing'}
    if action != 'restore' and _is_debt_fully_settled(initial.get('debt', 0.0)):
        return False, {'reason': 'debt_settled'}
    candidates, manual_review = get_reseller_debt_service_candidates(initial)
    results = {'changed': [], 'already_missing': [], 'failed': [], 'manual_review': manual_review}

    if action == 'restore':
        candidates = []
        for index, config in enumerate(initial.get('configs', [])):
            if isinstance(config, dict) and config.get('debt_policy_blocked') and not _is_removed_config(config):
                candidates.append({
                    'config_index': index,
                    'username': str(config.get('username') or '').strip(),
                    'server_id': config.get('server_id'),
                })

    initial_charges = {
        str(charge.get('id') or ''): charge
        for charge in initial.get('debt_charges', [])
        if isinstance(charge, dict)
    }

    external = []
    for candidate in candidates:
        precomputed_calculations = []
        if action == 'remove':
            index = candidate.get('config_index')
            configs = initial.get('configs', [])
            config = configs[index] if isinstance(index, int) and 0 <= index < len(configs) else None
            snapshot = config.get('debt_policy_hold_snapshot') if isinstance(config, dict) else None
            held_sources = config.get('debt_policy_hold_sources') if isinstance(config, dict) else None
            sources = held_sources if isinstance(held_sources, dict) else _debt_service_charge_sources(config)
            unsafe_charge_ids = []
            for charge_id in candidate.get('charge_ids', []):
                calculation = _prorated_collectible(
                    initial_charges.get(str(charge_id)),
                    sources.get(str(charge_id)),
                    snapshot,
                )
                if calculation is None:
                    unsafe_charge_ids.append(str(charge_id))
                else:
                    precomputed_calculations.append({'charge_id': str(charge_id), **calculation})
            if unsafe_charge_ids or not precomputed_calculations:
                results['manual_review'].extend(unsafe_charge_ids or candidate.get('charge_ids', []))
                results['failed'].append({**candidate, 'reason': 'proration_unsafe'})
                continue
        username = candidate.get('username')
        server_id = candidate.get('server_id')
        client, live, lookup = _exact_debt_service_lookup(multi_api, username, server_id)
        lookup_status = str((lookup or {}).get('status') or 'unavailable')
        if lookup_status == 'unavailable':
            results['failed'].append({**candidate, 'reason': (lookup or {}).get('error')})
            continue
        if lookup_status == 'missing' or client is None or live is None:
            details = {
                'snapshot': {
                    'held_at': _now_str(),
                    'blocked_before_policy': True,
                    'status_before_policy': 'missing',
                    'account_creation_date': None,
                    'expiration_days': None,
                    'used_bytes': 0,
                    'quota_bytes': 0,
                },
                'changed_by_policy': False,
                'calculations': precomputed_calculations,
                'api_result': {'status': 'already_missing'},
            }
            external.append((candidate, 'already_missing', details))
            results['already_missing'].append(candidate)
            continue
        held_at = _now_str()
        snapshot = _live_usage_snapshot(live, held_at)
        if action == 'hold':
            changed_by_policy = not snapshot['blocked_before_policy']
            api_result = client.update_user(username, {'blocked': True}) if changed_by_policy else {'unchanged': True}
        elif action == 'restore':
            config = initial.get('configs', [])[candidate['config_index']]
            changed_by_policy = bool(config.get('debt_policy_changed_blocked'))
            api_result = client.update_user(username, {'blocked': False}) if changed_by_policy else {'unchanged': True}
        else:
            changed_by_policy = True
            api_result = client.delete_user(username)
        if api_result is None:
            results['failed'].append({**candidate, 'reason': 'api_failed'})
            continue
        external.append((candidate, 'changed', {
            'snapshot': snapshot,
            'changed_by_policy': changed_by_policy,
            'calculations': precomputed_calculations,
            'api_result': api_result,
        }))
        results['changed'].append(candidate)

    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                current = _ensure_reseller_defaults(resellers.get(user_id, {}))
                if action != 'restore' and _is_debt_fully_settled(current.get('debt', 0.0)):
                    return False, {'reason': 'debt_settled_during_action', **results}
                _ensure_debt_charge_ledger(current)
                charges = {
                    str(charge.get('id') or ''): charge
                    for charge in current.get('debt_charges', [])
                    if isinstance(charge, dict)
                }
                total_writeoff = 0.0
                completed = 0
                for candidate, outcome, details in external:
                    index = candidate.get('config_index')
                    configs = current.get('configs', [])
                    if not isinstance(index, int) or index < 0 or index >= len(configs):
                        continue
                    config = configs[index]
                    if not isinstance(config, dict):
                        continue
                    if action == 'hold':
                        config['debt_policy_blocked'] = True
                        config['debt_policy_changed_blocked'] = bool((details or {}).get('changed_by_policy'))
                        config['debt_policy_hold_snapshot'] = (details or {}).get('snapshot')
                        config['debt_policy_hold_sources'] = {
                            str(charge_id): dict(source)
                            for charge_id, source in _debt_service_charge_sources(config).items()
                            if str(charge_id) in {str(item) for item in candidate.get('charge_ids', [])}
                        }
                        config['debt_policy_hold_charge_allocations'] = [
                            {
                                'charge_id': str(charge_id),
                                'original_amount': _money_value(charges.get(str(charge_id), {}).get('original_amount')),
                                'outstanding_at_hold': _charge_outstanding(charges.get(str(charge_id))),
                            }
                            for charge_id in candidate.get('charge_ids', [])
                        ]
                        config['debt_policy_hold_status'] = outcome
                        completed += 1
                    elif action == 'restore':
                        config['debt_policy_blocked'] = False
                        config['debt_policy_changed_blocked'] = False
                        config['debt_policy_restored_at'] = _now_str()
                        completed += 1
                    elif action == 'remove':
                        snapshot = config.get('debt_policy_hold_snapshot')
                        held_sources = config.get('debt_policy_hold_sources')
                        sources = held_sources if isinstance(held_sources, dict) else _debt_service_charge_sources(config)
                        calculations = []
                        safe_to_finalize = bool(candidate.get('charge_ids'))
                        for charge_id in candidate.get('charge_ids', []):
                            charge = charges.get(str(charge_id))
                            calculation = _prorated_collectible(charge, sources.get(str(charge_id)), snapshot)
                            if calculation is None:
                                results['manual_review'].append(str(charge_id))
                                safe_to_finalize = False
                                continue
                            writeoff = calculation['writeoff']
                            if writeoff > 0:
                                before = _charge_outstanding(charge)
                                charge['outstanding_amount'] = round(max(0.0, before - writeoff), 2)
                                charge.setdefault('writeoffs', []).append({
                                    'kind': 'unused_service_proration',
                                    'amount': writeoff,
                                    'created_at': _now_str(),
                                    **calculation,
                                })
                                total_writeoff += writeoff
                            calculations.append({'charge_id': charge_id, **calculation})
                        if safe_to_finalize:
                            config['removed_from_vpn'] = True
                            config['removal_reason'] = REMOVAL_REASON_RESELLER_DEBT_DEFAULT
                            config['removal_note'] = 'Removed after reseller debt default deadline'
                            config['removed_at'] = _now_str()
                            config['removed_cleanup_status'] = (
                                REMOVAL_STATUS_ALREADY_MISSING if outcome == 'already_missing'
                                else REMOVAL_STATUS_DELETED_FROM_VPN
                            )
                            config['debt_proration'] = calculations
                            config['debt_removal_api_result'] = (details or {}).get('api_result')
                            completed += 1
                if total_writeoff > 0:
                    current['debt'] = round(max(0.0, _safe_float(current.get('debt')) - total_writeoff), 2)
                    _record_debt_allocation(
                        current,
                        total_writeoff,
                        [],
                        'unused_service_proration',
                        reference_id=f"proration:{current.get('debt_cycle_id')}",
                    )
                stage_completed = not results['failed'] and completed == len(candidates)
                if action == 'hold' and stage_completed:
                    current['debt_services_held_at'] = _now_str()
                    current['debt_service_hold_due'] = False
                elif action == 'remove' and stage_completed:
                    current['debt_services_removed_at'] = _now_str()
                    current['debt_service_remove_due'] = False
                elif action == 'restore' and stage_completed:
                    current['debt_services_held_at'] = None
                    current['debt_restore_services_due'] = False
                if _is_debt_fully_settled(current.get('debt', 0.0)):
                    current['debt_since'] = None
                    current = _restore_suspended_if_debt_fully_settled(current)
                    current = _mark_policy_restore_due_if_needed(current)
                    current = _finish_debt_cycle(current)
                current.setdefault('debt_service_actions', []).append({
                    'action': action,
                    'timestamp': _now_str(),
                    'changed': completed,
                    'failed': len(results['failed']),
                    'manual_review': sorted(set(results['manual_review'])),
                    'writeoff': round(total_writeoff, 2),
                })
                current['debt_service_actions'] = current['debt_service_actions'][-50:]
                current = _ensure_reseller_defaults(current)
                resellers[user_id] = current
                _write_resellers_file(resellers)
                results.update({
                    'completed': completed,
                    'stage_completed': stage_completed,
                    'writeoff': round(total_writeoff, 2),
                    'remaining_debt': current.get('debt', 0.0),
                })
                return stage_completed, results
        except Exception as error:
            return False, {'reason': str(error), **results}


def apply_reseller_payment(user_id, amount, payment_id=None, allocation_kind='settlement'):
    user_id = str(user_id)
    paid_amount = _money_value(amount)
    if paid_amount <= 0:
        return False, None
    payment_key = str(payment_id or '').strip() or None
    duplicate = False
    saved_current = None
    new_debt = None
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return False, None
                resellers = _read_resellers_file()

                if user_id not in resellers:
                    return False, None

                current = _ensure_reseller_defaults(resellers[user_id])
                if payment_key:
                    previous = next(
                        (
                            item for item in current.get('processed_debt_payments', [])
                            if str(item.get('payment_id') or '') == payment_key
                        ),
                        None,
                    )
                    if previous is not None:
                        if _money_value(previous.get('amount')) != paid_amount:
                            return False, None
                        duplicate = True
                        new_debt = _safe_float(current.get('debt', 0.0))
                        saved_current = current
                if duplicate:
                    pass
                else:
                    current_debt = _safe_float(current.get('debt', 0.0))
                    cycle_id = str(current.get('debt_cycle_id') or '')
                    cycle_started = _parse_time(current.get('debt_since'))
                    cycle_was_late = bool(current.get('debt_cycle_late_recorded'))
                    credited_amount = max(0.0, min(paid_amount, current_debt))
                    excess_amount = round(max(0.0, paid_amount - current_debt), 2)
                    new_debt = round(max(0.0, current_debt - paid_amount), 2)
                    _ensure_debt_charge_ledger(current)
                    _allocate_debt_fifo(
                        current,
                        credited_amount,
                        kind=allocation_kind,
                        reference_id=payment_key,
                    )
                    current['debt'] = new_debt

                    if credited_amount > 0:
                        current['total_paid'] = round(
                            get_reseller_total_paid(current) + credited_amount,
                            2,
                        )
                        current['last_payment_at'] = _now_str()
                    if _is_debt_fully_settled(new_debt):
                        current['debt_since'] = None
                        if (
                            current_debt > 0
                            and not cycle_was_late
                            and cycle_started is not None
                            and (datetime.now() - cycle_started).total_seconds()
                                < DEBT_SUSPEND_DEADLINE_HOURS * 3600
                        ):
                            _record_credit_outcome(
                                current,
                                'good',
                                'on_time_settlement',
                                reference_id=f"good:{cycle_id or payment_key or uuid.uuid4().hex}",
                            )
                    current = _restore_suspended_if_debt_fully_settled(current)
                    if _is_debt_fully_settled(new_debt):
                        current = _mark_policy_restore_due_if_needed(current)
                        current = _finish_debt_cycle(current)
                    if excess_amount > 0:
                        excess_id = f"settlement-excess:{payment_key or uuid.uuid4().hex}"
                        if not any(
                            str(item.get('id') or '') == excess_id
                            for item in current.get('pending_wholesale_credits', [])
                        ):
                            current.setdefault('pending_wholesale_credits', []).append({
                                'id': excess_id,
                                'amount': excess_amount,
                                'source': 'settlement_excess',
                                'created_at': _now_str(),
                            })
                    if payment_key:
                        current.setdefault('processed_debt_payments', []).append({
                            'payment_id': payment_key,
                            'amount': paid_amount,
                            'credited_to_debt': round(credited_amount, 2),
                            'excess_amount': excess_amount,
                            'debt_after': new_debt,
                            'processed_at': _now_str(),
                        })
                    current = _ensure_reseller_defaults(current)
                    resellers[user_id] = current
                    _write_resellers_file(resellers)
                    saved_current = current
        except Exception:
            return False, None
    if not duplicate and saved_current is not None:
        _update_recruitment_milestone(user_id, saved_current)
    flush_reseller_pending_wholesale_credits(user_id)
    return True, new_debt


def flush_reseller_pending_wholesale_credits(user_id=None):
    """Retry durable settlement-excess credits without holding the reseller file lock."""
    target = str(user_id) if user_id is not None else None
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return 0
                resellers = _read_resellers_file()
                pending = [
                    (str(reseller_id), dict(item))
                    for reseller_id, record in resellers.items()
                    if target is None or str(reseller_id) == target
                    for item in _ensure_reseller_defaults(record).get('pending_wholesale_credits', [])
                ]
        except Exception:
            return 0
    completed_ids = set()
    for reseller_id, item in pending:
        credit_id = str(item.get('id') or '')
        amount = _money_value(item.get('amount'))
        if not credit_id or amount <= 0:
            continue
        try:
            from utils.reseller_wholesale_credit import credit_wholesale_balance

            credit_wholesale_balance(
                reseller_id,
                amount,
                credit_id,
                source=str(item.get('source') or 'settlement_excess'),
            )
            completed_ids.add((reseller_id, credit_id))
        except Exception:
            continue
    if not completed_ids:
        return 0
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                for reseller_id, credit_id in completed_ids:
                    if reseller_id not in resellers:
                        continue
                    current = _ensure_reseller_defaults(resellers[reseller_id])
                    current['pending_wholesale_credits'] = [
                        item for item in current.get('pending_wholesale_credits', [])
                        if str(item.get('id') or '') != credit_id
                    ]
                    resellers[reseller_id] = current
                _write_resellers_file(resellers)
        except Exception:
            return 0
    return len(completed_ids)


def _compute_debt_state_with_deadline(debt, debt_since, now):
    """Return the time-based debt state without ever banning a reseller."""
    debt_amount = _safe_float(debt, 0.0)

    if _is_debt_fully_settled(debt_amount):
        return 'active', False, False

    debt_since_dt = _parse_time(debt_since)
    hours_in_debt = 0.0
    if debt_since_dt:
        hours_in_debt = max(0.0, (now - debt_since_dt).total_seconds() / 3600)

    suspend_deadline_passed = hours_in_debt >= DEBT_SUSPEND_DEADLINE_HOURS
    removal_deadline_passed = hours_in_debt >= DEBT_REMOVAL_DEADLINE_HOURS

    if suspend_deadline_passed:
        return 'suspended', True, removal_deadline_passed
    if hours_in_debt >= 24:
        return 'warning', False, removal_deadline_passed
    return 'active', False, removal_deadline_passed


def _notification_claim_due(record, kind, audience, now, lease_minutes=10):
    state = record.setdefault('debt_notification_state', {})
    key = f"{kind}:{audience}"
    item = state.get(key)
    if not isinstance(item, dict):
        item = {}
    if item.get('delivered_at'):
        return False
    claimed_at = _parse_time(item.get('claimed_at'))
    if claimed_at is not None and now - claimed_at < timedelta(minutes=lease_minutes):
        return False
    state[key] = {'claimed_at': now.strftime('%Y-%m-%d %H:%M:%S')}
    return True


def _service_action_claim_due(record, action, now, lease_minutes=10):
    claims = record.setdefault('debt_service_action_claims', {})
    current_cycle = str(record.get('debt_cycle_id') or '')
    claim = claims.get(action) if isinstance(claims.get(action), dict) else {}
    if str(claim.get('cycle_id') or '') != current_cycle:
        claim = {}
    claimed_at = _parse_time(claim.get('claimed_at'))
    if claimed_at is not None and now - claimed_at < timedelta(minutes=lease_minutes):
        return False
    claims[action] = {
        'cycle_id': current_cycle,
        'claimed_at': now.strftime('%Y-%m-%d %H:%M:%S'),
    }
    return True


def complete_reseller_debt_service_action_claim(user_id, action, completed=True):
    """Complete or release a hold/remove/restore action lease."""
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                current.setdefault('debt_service_action_claims', {}).pop(str(action), None)
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def complete_reseller_debt_notification(user_id, kind, audience, delivered=True):
    user_id = str(user_id)
    key = f"{kind}:{audience}"
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                state = current.setdefault('debt_notification_state', {})
                item = state.get(key) if isinstance(state.get(key), dict) else {}
                if delivered:
                    item['delivered_at'] = _now_str()
                    item.pop('claimed_at', None)
                else:
                    item.pop('claimed_at', None)
                state[key] = item
                if kind == 'recovered' and delivered:
                    user_done = bool((state.get('recovered:user') or {}).get('delivered_at'))
                    admin_done = bool((state.get('recovered:admin') or {}).get('delivered_at'))
                    if user_done and admin_done:
                        current['debt_recovery_pending'] = False
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def claim_reseller_debt_notification(user_id, kind, audience):
    user_id = str(user_id)
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                claimed = _notification_claim_due(current, str(kind), str(audience), datetime.now())
                if claimed:
                    resellers[user_id] = current
                    _write_resellers_file(resellers)
                return claimed
        except Exception:
            return False


def mark_reseller_debt_service_action(user_id, action, result=None):
    """Persist a completed external hold/remove/restore action."""
    user_id = str(user_id)
    result = dict(result or {})
    with reseller_lock:
        try:
            with _resellers_file_lock():
                resellers = _read_resellers_file()
                if user_id not in resellers:
                    return False
                current = _ensure_reseller_defaults(resellers[user_id])
                if action == 'hold':
                    current['debt_services_held_at'] = _now_str()
                    current['debt_service_hold_due'] = False
                    skipped = ('opened', 'reminder_24h', 'deadline_final', 'suspended')
                elif action == 'remove':
                    current['debt_services_removed_at'] = _now_str()
                    current['debt_service_remove_due'] = False
                    skipped = ('opened', 'reminder_24h', 'deadline_final', 'suspended', 'hold_due', 'deletion_warning')
                elif action == 'restore':
                    current['debt_services_held_at'] = None
                    current['debt_restore_services_due'] = False
                    skipped = ()
                else:
                    return False
                for kind in skipped:
                    current.setdefault('debt_notification_state', {}).setdefault(
                        f"{kind}:user", {}
                    ).setdefault('delivered_at', _now_str())
                current.setdefault('debt_service_actions', []).append({
                    'action': action,
                    'timestamp': _now_str(),
                    'result': result,
                })
                current['debt_service_actions'] = current['debt_service_actions'][-50:]
                resellers[user_id] = current
                _write_resellers_file(resellers)
                return True
        except Exception:
            return False


def evaluate_reseller_debt_policies():
    with reseller_lock:
        try:
            with _resellers_file_lock():
                if not _resellers_store_exists():
                    return []
                resellers = _read_resellers_file()

                now = datetime.now()
                events = []
                changed = False

                for user_id, record in resellers.items():
                    current = _ensure_reseller_defaults(record)
                    current = _restore_suspended_if_debt_fully_settled(current)
                    debt = _safe_float(current.get('debt', 0.0))
                    debt_fully_settled = _is_debt_fully_settled(debt)
                    original_status = current.get('status', 'pending')

                    # Fraud/abuse bans are explicit admin state. Debt automation
                    # must neither change nor clean up an already-banned account.
                    if original_status == 'banned':
                        if current != record:
                            changed = True
                            resellers[user_id] = current
                        continue

                    if debt_fully_settled:
                        current = _mark_policy_restore_due_if_needed(current)
                        current = _finish_debt_cycle(current)

                    if not debt_fully_settled and not current.get('debt_since'):
                        current['debt_since'] = _now_str()
                    if not debt_fully_settled and not current.get('debt_cycle_id'):
                        current['debt_cycle_id'] = uuid.uuid4().hex

                    debt_since = current.get('debt_since')
                    debt_state, suspend_deadline_passed, removal_deadline_passed = _compute_debt_state_with_deadline(
                        debt, debt_since, now
                    )
                    current['debt_state'] = debt_state
                    auto_suspended = False

                    debt_since_dt = _parse_time(current.get('debt_since'))
                    debt_age_hours = (
                        max(0.0, (now - debt_since_dt).total_seconds() / 3600)
                        if debt_since_dt else 0.0
                    )

                    if (
                        not debt_fully_settled
                        and suspend_deadline_passed
                        and current.get('status') in {'approved', 'suspended'}
                    ):
                        if current.get('status') == 'approved':
                            current['status'] = 'suspended'
                            current['suspended_reason'] = SUSPENDED_REASON_DEBT
                            current['suspended_at'] = _now_str()
                            auto_suspended = True
                        elif current.get('suspended_reason') == SUSPENDED_REASON_UNBAN_GRACE:
                            current['suspended_reason'] = SUSPENDED_REASON_DEBT
                        if not current.get('debt_cycle_late_recorded'):
                            _record_credit_outcome(
                                current,
                                'late',
                                'debt_deadline',
                                reference_id=f"late:{current.get('debt_cycle_id')}",
                            )
                            current['debt_cycle_late_recorded'] = True
                        changed = True

                    if not debt_fully_settled and debt_age_hours >= DEBT_HOLD_DEADLINE_HOURS:
                        current['debt_service_hold_due'] = not bool(current.get('debt_services_held_at'))
                        if not current.get('debt_cycle_default_recorded'):
                            _record_credit_outcome(
                                current,
                                'default',
                                'service_hold_deadline',
                                reference_id=f"default:{current.get('debt_cycle_id')}",
                            )
                            current['debt_cycle_default_recorded'] = True
                        changed = True

                    if not debt_fully_settled and removal_deadline_passed:
                        current['debt_service_remove_due'] = not bool(current.get('debt_services_removed_at'))
                        changed = True

                    if debt_fully_settled and current.get('debt_recovery_pending'):
                        event_kind = 'recovered'
                    elif not debt_fully_settled and current.get('debt_service_remove_due'):
                        event_kind = 'remove_due'
                    elif not debt_fully_settled and debt_age_hours >= DEBT_FINAL_WARNING_HOURS and not current.get('debt_services_removed_at'):
                        event_kind = 'deletion_warning'
                    elif not debt_fully_settled and current.get('debt_service_hold_due'):
                        event_kind = 'hold_due'
                    elif auto_suspended:
                        event_kind = 'suspended'
                    elif not debt_fully_settled and current.get('debt_services_removed_at'):
                        removed_at = _parse_time(current.get('debt_services_removed_at'))
                        weeks_after_removal = (
                            int(max(0.0, (now - removed_at).total_seconds()) // (7 * 24 * 3600))
                            if removed_at else 0
                        )
                        event_kind = (
                            f'post_removal_week_{weeks_after_removal}'
                            if weeks_after_removal >= 1 else None
                        )
                    elif current.get('status') == 'suspended':
                        event_kind = None
                    elif not debt_fully_settled and debt_age_hours >= max(24.0, DEBT_SUSPEND_DEADLINE_HOURS - 6.0):
                        event_kind = 'deadline_final'
                    elif not debt_fully_settled and debt_age_hours >= 24.0:
                        event_kind = 'reminder_24h'
                    elif not debt_fully_settled:
                        event_kind = 'opened'
                    else:
                        event_kind = None

                    notify_user = False
                    notify_admin = False
                    service_action = None
                    if event_kind:
                        requested_action = (
                            'restore' if event_kind == 'recovered' and current.get('debt_restore_services_due')
                            else ('hold' if event_kind == 'hold_due' else ('remove' if event_kind == 'remove_due' else None))
                        )
                        if requested_action and _service_action_claim_due(current, requested_action, now):
                            service_action = requested_action
                        if not requested_action or service_action:
                            notify_user = _notification_claim_due(current, event_kind, 'user', now)
                            if event_kind in {'suspended', 'recovered', 'hold_due', 'remove_due'}:
                                notify_admin = _notification_claim_due(current, event_kind, 'admin', now)

                    if current != record:
                        changed = True
                        resellers[user_id] = current

                    if event_kind and (notify_user or notify_admin or service_action):
                        debt_age_days = max(0, int(debt_age_hours // 24))
                        unlock_amount = get_reseller_unlock_amount(debt) if debt_state == 'suspended' else 0.0
                        events.append({
                            'user_id': str(user_id),
                            'kind': event_kind,
                            'cycle_id': current.get('debt_cycle_id'),
                            'debt': debt,
                            'debt_state': debt_state,
                            'status': current.get('status', 'pending'),
                            'suspended_reason': current.get('suspended_reason'),
                            'debt_age_days': debt_age_days,
                            'debt_age_hours': debt_age_hours,
                            'debt_since': current.get('debt_since'),
                            'last_payment_at': current.get('last_payment_at'),
                            'unlock_amount': unlock_amount,
                            'notify_user': notify_user,
                            'notify_admin': notify_admin,
                            'auto_suspended': auto_suspended,
                            'auto_banned': False,
                            'service_action': service_action,
                            'hours_until_suspend': max(0, DEBT_SUSPEND_DEADLINE_HOURS - debt_age_hours),
                            'hours_until_hold': max(0, DEBT_HOLD_DEADLINE_HOURS - debt_age_hours),
                            'hours_until_removal': max(0, DEBT_REMOVAL_DEADLINE_HOURS - debt_age_hours),
                            'hours_until_ban': 0.0,
                            'suspend_deadline_passed': suspend_deadline_passed,
                            'ban_deadline_passed': False,
                            'credit_policy': get_reseller_credit_policy(current),
                        })

                if changed:
                    _write_resellers_file(resellers)

                return events
        except Exception:
            return []
