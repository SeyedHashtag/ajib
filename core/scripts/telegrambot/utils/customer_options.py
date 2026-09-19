"""Read-only customer choices, calculated by the existing business services."""
import os
from pathlib import Path

from . import account_operations, database
from .web_services import ServiceError


REASONS = {
    'renewal_already_reserved': 'already_reserved',
    'renewal_ineligible_not_expired': 'not_expired',
    'renewal_ineligible_protected_block': 'protected_account',
    'renewal_ineligible_no_record': 'history_unavailable',
    'renewal_ineligible_plan_missing': 'plan_unavailable',
    'renewal_ineligible_plan_mismatch': 'account_changed',
    'renewal_cycle_changed': 'account_changed',
}


def payment_methods(services, scope, language, *, writes=True):
    from .receipt_checker import get_card_number_for_receipt_type
    from .exchange_rate import get_exchange_rate
    common = 'store_unavailable' if scope != 'main' else 'writes_paused' if not writes else None
    card_reason = common or ('language_unavailable' if language != 'fa' else None)
    if not card_reason:
        try:
            available = bool(get_card_number_for_receipt_type('regular')) and bool(get_exchange_rate())
        except Exception:
            available = False
        if not available:
            card_reason = 'method_unavailable'
    crypto_reason = common
    if not crypto_reason and not (os.getenv('CRYPTO_MERCHANT_ID') and os.getenv('CRYPTO_API_KEY')
                                 and services.bot_token(scope)):
        crypto_reason = 'method_unavailable'
    return [{'id': method, 'available': reason is None, 'reason': reason}
            for method, reason in [('crypto', crypto_reason), ('card', card_reason)]]


def renewal_options(services, user_id, scope, server_id, username, *, writes=True):
    from .catalog_service import load_catalog
    from .renewal import find_customer_renewal_offer, find_customer_reservation
    if not services.owned(user_id, scope, username, server_id):
        raise ServiceError('Account not found', 404)
    result = {'username': username, 'server_id': server_id, 'choices': [],
              'reservation': None, 'reason': None}
    if scope != 'main':
        return {**result, 'reason': 'store_unavailable'}
    records = services.payments(user_id, scope)
    reservation = find_customer_reservation(user_id, username, server_id=server_id, payments=records)
    if reservation:
        result['reservation'] = {'payment_id': reservation['payment_id'],
                                 'status': reservation.get('renewal_status') or 'pending'}
    try:
        from .reseller_blocks import renewal_block_guard
        with renewal_block_guard(username, server_id) as allowed:
            if not allowed:
                return {**result, 'reason': 'protected_account'}
            client, data, lookup = services.resolve_account(user_id, scope, username, server_id)
            plans = load_catalog(Path(database.bot_dir()) / 'plans.json')
            for plan in services.catalog(scope):
                for mode in ('immediate', 'reserved'):
                    offer = find_customer_renewal_offer(int(user_id), username, client, data, plans,
                        payments=records, server_id=server_id, allow_reservation=mode == 'reserved',
                        target_plan_gb=plan['id'], lookup_result=lookup)
                    enabled = bool(offer.get('eligible')) and offer.get('renewal_mode') == mode
                    reason = None if enabled else REASONS.get(offer.get('reason'), 'renewal_unavailable')
                    if not writes:
                        enabled, reason = False, 'writes_paused'
                    result['choices'].append({'plan': plan, 'mode': mode, 'available': enabled,
                                              'reason': reason})
    except account_operations.AccountBusy:
        result['reason'] = 'account_busy'
    except ServiceError as error:
        if error.status == 404:
            raise
        result['reason'] = 'account_unavailable'
    return result
