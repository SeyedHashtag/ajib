"""Amount-based credit rehabilitation, independent of payment and bot runtimes."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from utils.time_utils import parse_utc_timestamp

CYCLE_CENTS = 500
RECOVERY_CENTS = 1000


def cents(amount):
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0:
            raise ValueError('Invalid wholesale amount')
        return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError) as error:
        raise ValueError('Invalid wholesale amount') from error


def recovery_state(record):
    saved = record.get('credit_recovery')
    if (isinstance(saved, dict) and saved.get('version') == 1
            and saved.get('penalty') in (0, 1, 2)
            and isinstance(saved.get('spent_cents'), int)
            and 0 <= saved['spent_cents'] <= RECOVERY_CENTS):
        return dict(saved)
    history = record.get('credit_outcomes')
    outcomes = [x for x in history if isinstance(x, dict)][-3:] if isinstance(history, list) else []
    penalty = min(2, sum({'late': 1, 'default': 2}.get(x.get('outcome'), 0) for x in outcomes))
    state = {'version': 1, 'penalty': penalty, 'spent_cents': 0, 'reason': None}
    adverse = [x for x in outcomes if x.get('outcome') in {'late', 'default'}]
    if not penalty or not adverse:
        return state
    last = adverse[-1]
    state['reason'] = last.get('outcome')
    started = parse_utc_timestamp(last.get('recorded_at'))
    if started is None:
        return state
    # Only dated, fulfilled wholesale records prove spending. Top-ups and
    # settlements are intentionally absent; an outcome count proves no amount.
    seen = set()
    configs = record.get('configs')
    for config in configs if isinstance(configs, list) else []:
        if not isinstance(config, dict):
            continue
        renewals = config.get('renewals')
        renewals = renewals if isinstance(renewals, list) else []
        for item in [config] + [r for r in renewals if isinstance(r, dict)]:
            paid_at = parse_utc_timestamp(item.get('timestamp'))
            reference = item.get('retail_order_id') or item.get('reservation_id')
            reference = reference or (config.get('username'), item.get('timestamp'))
            if reference in seen or paid_at is None or paid_at <= started:
                continue
            seen.add(reference)
            funding = item.get('funding') or {}
            amount = funding.get('prepaid_cents', 0) + funding.get('external_cents', 0)
            if not funding and item.get('funded_at_checkout'):
                amount = cents(item.get('price', 0))
            state['spent_cents'] = min(RECOVERY_CENTS, state['spent_cents'] + amount)
    if state['spent_cents'] >= RECOVERY_CENTS:
        state['penalty'] = 0
        state['reason'] = 'recovered'
    return state


def record_spending(record, amount, reference, now):
    state = recovery_state(record)
    references = record.setdefault('credit_spending_references', [])
    if reference in references or cents(amount) <= 0:
        record['credit_recovery'] = state
        return False
    references.append(reference)
    if state['penalty']:
        state['spent_cents'] = min(RECOVERY_CENTS, state['spent_cents'] + cents(amount))
        if state['spent_cents'] == RECOVERY_CENTS:
            state.update(penalty=0, reason='recovered')
            record.setdefault('credit_policy_history', []).append({
                'kind': 'recovered', 'reference_id': reference, 'recorded_at': now,
            })
    record['credit_recovery'] = state
    return True


def record_penalty(record, outcome, reference, now):
    state = recovery_state(record)
    state.update(penalty=min(2, state['penalty'] + {'late': 1, 'default': 2}[outcome]),
                 spent_cents=0, reason=outcome)
    record['credit_recovery'] = state
    record.setdefault('credit_policy_history', []).append({
        'kind': outcome, 'reference_id': reference, 'recorded_at': now,
    })
