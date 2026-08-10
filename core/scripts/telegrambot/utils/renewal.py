import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from utils.account_state import (
    inspect_account,
    is_business_expired,
    panel_deadline,
    panel_days_remaining,
    parse_timestamp,
    resolve_service_cycle,
    verified_panel_expired,
)
from utils.atomic_store import locked_json, read_json, write_json


PAYMENTS_FILE = '/etc/ajib/core/scripts/telegrambot/payments.json'
RESELLERS_FILE = '/etc/ajib/core/scripts/telegrambot/resellers.json'
STATE_FILE = '/etc/ajib/core/scripts/telegrambot/expired_user_cleanup.json'

GB_BYTES = 1024 ** 3
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
PAID_STATUSES = {'completed', 'paid', 'succeeded'}
DELETE_RESULTS = {'deleted', 'already_missing'}
RESERVATION_ACTIVE_STATUSES = {'reserved', 'processing', 'attention'}
RESERVATION_RETRY_SECONDS = 3600
RESERVATION_CLAIM_LEASE_SECONDS = 600
CUSTOMER_RENEWAL_DISCOUNT_PERCENT = Decimal('10')
MONEY = Decimal('0.01')


def _load_json_file(path, default):
    try:
        data = read_json(path, default)
        return data if data is not None else default
    except Exception:
        pass
    return default


def _save_json_file(path, data):
    write_json(path, data)


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y'}
    return bool(value)


def _safe_bytes(value):
    return max(0, _safe_int(value, 0) or 0)


def _gb_from_bytes(byte_count):
    if byte_count is None:
        return None
    return round(float(byte_count) / GB_BYTES, 3)


def _parse_account_creation_time(value):
    return parse_timestamp(value)


def _expiration_deadline(user_data):
    return panel_deadline(user_data)


def _days_remaining(user_data, now=None):
    return panel_days_remaining(user_data, now=now)


def _now_str():
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def _state_key(server_id, username):
    return f"{server_id or 'primary'}:{username}"


def _token(*parts):
    raw = ':'.join(str(part or '') for part in parts)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _record_renewal_completed(plan_record, result, source):
    """Best-effort idempotent renewal measurement after a successful reset."""
    try:
        from utils.growth_events import (
            EVENT_RENEWAL_COMPLETED,
            SURFACE_HOSTED,
            SURFACE_MAIN,
            record_growth_event,
        )

        record = plan_record if isinstance(plan_record, dict) else {}
        after_state = (result or {}).get('after_state') or {}
        identity = next((
            str(value)
            for value in (
                record.get('payment_id'),
                record.get('reservation_id'),
                record.get('retail_order_id'),
                record.get('created_at'),
                record.get('renewal_reserved_at'),
                record.get('timestamp'),
                after_state.get('captured_at'),
            )
            if value
        ), _token(source, (result or {}).get('username'), _now_str()))
        hosted = str(source or '').lower() == 'hosted_customer'
        tenant_id = (
            record.get('hosted_tenant_id')
            or record.get('hosted_bot_id')
            or record.get('bot_id')
        ) if hosted else None
        user_id = (
            record.get('buyer_user_id')
            or record.get('customer_user_id')
            or record.get('user_id')
            or record.get('reseller_id')
        )
        plan_id = (
            record.get('plan_gb')
            or record.get('gb')
            or (record.get('renewal_plan_snapshot') or {}).get('plan_gb')
        )
        record_growth_event(
            EVENT_RENEWAL_COMPLETED,
            user_id=user_id,
            surface=SURFACE_HOSTED if hosted else SURFACE_MAIN,
            hosted_tenant_id=tenant_id,
            plan_id=plan_id,
            payment_method=record.get('payment_method'),
            deduplication_key=f"renewal-completed:{source}:{identity}",
            metadata={
                'source': source,
                'username': str((result or {}).get('username') or _record_username(record)),
                'server_id': (result or {}).get('server_id') or _record_server_id(record),
                'renewal_mode': record.get('renewal_mode', 'immediate'),
            },
        )
    except Exception:
        return


def _escape_markdown(value):
    text = str(value if value is not None else '—')
    for char in ('\\', '`', '*', '_', '[', ']'):
        text = text.replace(char, f"\\{char}")
    return text


def capture_user_state(user_data, now=None, cycle=None):
    user_data = user_data or {}
    upload_bytes = _safe_bytes(user_data.get('upload_bytes'))
    download_bytes = _safe_bytes(user_data.get('download_bytes'))
    max_download_bytes = _safe_bytes(user_data.get('max_download_bytes'))
    used_bytes = upload_bytes + download_bytes
    remaining_bytes = None
    if max_download_bytes > 0:
        remaining_bytes = max(0, max_download_bytes - used_bytes)

    shared_state = inspect_account(user_data, cycle=cycle, now=now)
    return {
        'captured_at': _now_str(),
        'account_creation_date': user_data.get('account_creation_date'),
        'expiration_days': _safe_int(user_data.get('expiration_days')),
        'expiration_deadline': (
            _expiration_deadline(user_data).isoformat()
            if _expiration_deadline(user_data) is not None
            else None
        ),
        'days_remaining': _days_remaining(user_data, now=now),
        'configured_days': shared_state.configured_days,
        'panel_state': shared_state.panel_state.value,
        'entitlement_state': shared_state.entitlement_state.value,
        'normalized_state': shared_state.state,
        'entitlement_issued_at': (
            shared_state.entitlement_issued_at.isoformat()
            if shared_state.entitlement_issued_at else None
        ),
        'entitlement_deadline': (
            shared_state.entitlement_deadline.isoformat()
            if shared_state.entitlement_deadline else None
        ),
        'entitlement_days_remaining': shared_state.entitlement_days_remaining,
        'cycle_fingerprint': shared_state.cycle_fingerprint,
        'gb_remaining': _gb_from_bytes(remaining_bytes),
        'gb_limit': _gb_from_bytes(max_download_bytes) if max_download_bytes > 0 else None,
        'gb_used': _gb_from_bytes(used_bytes),
        'blocked': bool(user_data.get('blocked', False)),
        'status': user_data.get('status'),
        'upload_bytes': upload_bytes,
        'download_bytes': download_bytes,
        'max_download_bytes': max_download_bytes,
    }


def expected_after_state(plan_gb, days):
    return {
        'days_remaining': _safe_int(days, 0),
        'gb_remaining': round(_safe_float(plan_gb), 3),
        'gb_limit': round(_safe_float(plan_gb), 3),
        'gb_used': 0.0,
        'blocked': False,
        'status': 'active',
        'upload_bytes': 0,
        'download_bytes': 0,
        'max_download_bytes': int(round(_safe_float(plan_gb) * GB_BYTES)),
    }


def is_user_expired(user_data, now=None):
    return verified_panel_expired(user_data, now=now)


def _is_deleted_record(record):
    if not isinstance(record, dict):
        return True
    return (
        record.get('cleanup_status') in DELETE_RESULTS
        or record.get('cleanup_delete_result') in DELETE_RESULTS
        or bool(record.get('cleanup_deleted_at'))
        or bool(record.get('removed_from_vpn'))
    )


def _is_paid_customer_record(record):
    if not isinstance(record, dict):
        return False
    if record.get('type') == 'settlement' or record.get('plan_gb') == 'Settlement':
        return False
    return str(record.get('status', '')).lower() in PAID_STATUSES


def _record_username(record):
    return str(record.get('renewal_username') or record.get('username') or '').strip()


def _record_server_id(record):
    return record.get('renewal_server_id') or record.get('server_id')


def lookup_renewal_user(multi_api, username, server_id=None):
    """Resolve a renewal target without falling through to another server."""
    if server_id:
        strict_lookup = getattr(multi_api, 'find_user_on_server', None)
        if callable(strict_lookup):
            strict_result = strict_lookup(username, server_id)
            if isinstance(strict_result, tuple) and len(strict_result) == 3:
                api_client, user_data, result = strict_result
                result = result if isinstance(result, dict) else {}
                return api_client, user_data, {
                    'status': result.get('status') or ('found' if user_data is not None else 'unavailable'),
                    'http_status': result.get('http_status'),
                    'error': result.get('error'),
                }

        # Compatibility for injected clients used by older integrations. A
        # result from a different server is never accepted.
        api_client, user_data = multi_api.find_user(username, preferred_server_id=server_id)
        returned_server_id = getattr(api_client, 'server_id', None) if api_client else None
        if api_client and str(returned_server_id) == str(server_id) and user_data is not None:
            return api_client, user_data, {'status': 'found', 'http_status': None, 'error': None}
        return api_client, None, {
            'status': 'unavailable' if api_client is None else 'missing',
            'http_status': None,
            'error': 'strict_lookup_unavailable' if api_client is None else 'not_found',
        }

    api_client, user_data = multi_api.find_user(username)
    return api_client, user_data, {
        'status': 'found' if user_data is not None else 'missing',
        'http_status': None,
        'error': None if user_data is not None else 'not_found',
    }


def _lookup_failure_reason(lookup_result):
    if isinstance(lookup_result, dict) and lookup_result.get('status') == 'unavailable':
        return 'server_unavailable'
    return 'renewal_ineligible_missing'


def _lookup_failure_fields(lookup_result):
    if not isinstance(lookup_result, dict):
        return {}
    return {
        'renewal_api_error': lookup_result.get('error'),
        'renewal_api_http_status': lookup_result.get('http_status'),
    }


def customer_renewal_token(user_id, record_id, username, server_id):
    return _token('customer', user_id, record_id, server_id or 'primary', username)


def reseller_renewal_token(reseller_id, config_index, username, server_id):
    return _token('reseller', reseller_id, config_index, server_id or 'primary', username)


def _plan_for_record(record, plans, source):
    if not isinstance(record, dict) or not isinstance(plans, dict):
        return None, 'renewal_ineligible_plan_missing'

    plan_gb = str(record.get('plan_gb') if record.get('plan_gb') is not None else record.get('gb', '')).strip()
    if plan_gb not in plans:
        return None, 'renewal_ineligible_plan_missing'

    plan = plans.get(plan_gb) or {}
    target = plan.get('target', 'both')
    if source == 'customer' and target == 'reseller':
        return None, 'renewal_ineligible_plan_mismatch'
    if source == 'reseller_customer' and target == 'customer':
        return None, 'renewal_ineligible_plan_mismatch'

    if _safe_int(record.get('days')) != _safe_int(plan.get('days')):
        return None, 'renewal_ineligible_plan_mismatch'

    if (
        record.get('unlimited') is not None
        and _safe_bool(record.get('unlimited')) != _safe_bool(plan.get('unlimited', False))
    ):
        return None, 'renewal_ineligible_plan_mismatch'

    return plan, None


def _live_quota_matches_plan(user_data, plan_gb):
    max_download_bytes = _safe_bytes((user_data or {}).get('max_download_bytes'))
    if max_download_bytes <= 0:
        return False
    live_gb = max_download_bytes / GB_BYTES
    return abs(live_gb - _safe_float(plan_gb)) <= 0.01


def _build_offer(
    record,
    source,
    username,
    server_id,
    api_client,
    user_data,
    plans,
    extra=None,
    reseller_data=None,
    allow_reservation=False,
    lookup_result=None,
    cycle_records=None,
):
    if not api_client or not user_data:
        return {
            'eligible': False,
            'reason': _lookup_failure_reason(lookup_result),
            'source': source,
            'username': username,
            'server_id': server_id,
        }

    cycle = resolve_service_cycle(
        cycle_records if cycle_records is not None else record,
        username=username,
        server_id=server_id or getattr(api_client, 'server_id', None),
        source=source,
    )
    business_expired = is_business_expired(cycle)
    expired = is_user_expired(user_data) or business_expired
    if not expired and (not allow_reservation or bool(user_data.get('blocked', False))):
        return {
            'eligible': False,
            'reason': 'renewal_ineligible_not_expired',
            'source': source,
            'username': username,
            'server_id': server_id,
            'before_state': capture_user_state(user_data, cycle=cycle),
        }

    plan, reason = _plan_for_record(record, plans, source)
    if reason:
        return {
            'eligible': False,
            'reason': reason,
            'source': source,
            'username': username,
            'server_id': server_id,
            'before_state': capture_user_state(user_data, cycle=cycle),
        }

    plan_gb = str(record.get('plan_gb') if record.get('plan_gb') is not None else record.get('gb'))
    if not _live_quota_matches_plan(user_data, plan_gb):
        return {
            'eligible': False,
            'reason': 'renewal_ineligible_plan_mismatch',
            'source': source,
            'username': username,
            'server_id': server_id,
            'before_state': capture_user_state(user_data, cycle=cycle),
        }

    full_price = _safe_float(plan.get('price'))
    price = full_price
    reseller_level = None
    discount_percent = None
    renewal_discount_percent = None
    renewal_discount_amount = 0.0
    if source == 'customer':
        full_price_decimal = Decimal(str(full_price)).quantize(MONEY, rounding=ROUND_HALF_UP)
        renewal_discount_percent = float(CUSTOMER_RENEWAL_DISCOUNT_PERCENT)
        renewal_discount_decimal = (
            full_price_decimal * CUSTOMER_RENEWAL_DISCOUNT_PERCENT / Decimal('100')
        ).quantize(MONEY, rounding=ROUND_HALF_UP)
        renewal_discount_amount = float(renewal_discount_decimal)
        price = float(full_price_decimal - renewal_discount_decimal)
    elif source == 'reseller_customer':
        from utils.reseller import (
            calculate_reseller_wholesale_price,
            get_reseller_level_summary,
        )

        level_summary = get_reseller_level_summary(reseller_data or {})
        reseller_level = level_summary['level']
        discount_percent = level_summary['discount_percent']
        price = calculate_reseller_wholesale_price(full_price, reseller_data or {})

    offer = {
        'eligible': True,
        'source': source,
        'username': username,
        'server_id': server_id or getattr(api_client, 'server_id', None) or 'primary',
        'api_client': api_client,
        'plan_gb': plan_gb,
        'days': _safe_int(plan.get('days'), 0),
        'unlimited': _safe_bool(plan.get('unlimited', False)),
        'price': price,
        'full_price': full_price,
        'reseller_level': reseller_level,
        'discount_percent': discount_percent,
        'renewal_discount_percent': renewal_discount_percent,
        'renewal_discount_amount': renewal_discount_amount,
        'plan': plan,
        'before_state': capture_user_state(user_data, cycle=cycle),
        'expected_after_state': expected_after_state(plan_gb, plan.get('days')),
        'renewal_mode': 'immediate' if expired else 'reserved',
        'business_expired': business_expired,
        'cycle_fingerprint': cycle.fingerprint if cycle else None,
        'entitlement_deadline': cycle.deadline.isoformat() if cycle else None,
    }
    if extra:
        offer.update(extra)
    return offer


def _matching_customer_records(user_id, username=None, server_id=None, payments=None):
    payments = payments if payments is not None else _load_json_file(PAYMENTS_FILE, {})
    records = []
    for record_id, record in (payments or {}).items():
        if not _is_paid_customer_record(record) or _is_deleted_record(record):
            continue
        if str(record.get('user_id')) != str(user_id):
            continue
        record_username = _record_username(record)
        if not record_username:
            continue
        if username and record_username.lower() != str(username).strip().lower():
            continue
        record_server_id = _record_server_id(record)
        if server_id and record_server_id and str(record_server_id) != str(server_id):
            continue
        records.append((str(record_id), record))

    records.sort(key=lambda item: str(item[1].get('updated_at') or item[1].get('created_at') or ''), reverse=True)
    return records


def _reservation_matches(record, username, server_id=None):
    if not isinstance(record, dict) or record.get('renewal_mode') != 'reserved':
        return False
    target_username = _record_username(record).lower()
    if not target_username or target_username != str(username or '').strip().lower():
        return False
    target_server = _record_server_id(record)
    if server_id and target_server and str(target_server) != str(server_id):
        return False
    payment_status = str(record.get('status') or '').lower()
    fulfillment_status = str(record.get('renewal_status') or '').lower()
    if fulfillment_status in RESERVATION_ACTIVE_STATUSES:
        return True
    return payment_status in {'creating', 'waiting_receipt', 'pending_approval', 'pending', 'processing'}


def find_customer_reservation(user_id, username, server_id=None, payments=None):
    payments = payments if payments is not None else _load_json_file(PAYMENTS_FILE, {})
    for payment_id, record in (payments or {}).items():
        if str((record or {}).get('user_id')) != str(user_id):
            continue
        if _reservation_matches(record, username, server_id=server_id):
            return {'payment_id': str(payment_id), **dict(record)}
    return None


def find_reseller_reservation(config):
    if not isinstance(config, dict):
        return None
    for renewal in config.get('renewals', []):
        if not isinstance(renewal, dict):
            continue
        if renewal.get('renewal_mode') == 'reserved' and renewal.get('renewal_status') in RESERVATION_ACTIVE_STATUSES:
            return renewal
    return None


def find_customer_renewal_offer(
    user_id,
    username,
    api_client,
    user_data,
    plans,
    payments=None,
    server_id=None,
    allow_reservation=False,
):
    existing_reservation = find_customer_reservation(
        user_id,
        username,
        server_id=server_id or getattr(api_client, 'server_id', None),
        payments=payments,
    )
    if existing_reservation:
        return {
            'eligible': False,
            'reason': 'renewal_already_reserved',
            'source': 'customer',
            'username': username,
            'server_id': server_id or getattr(api_client, 'server_id', None),
            'reservation': existing_reservation,
        }
    matching_records = _matching_customer_records(
        user_id,
        username=username,
        server_id=server_id,
        payments=payments,
    )
    cycle_records = {record_id: record for record_id, record in matching_records}
    current_cycle = resolve_service_cycle(
        cycle_records,
        username=username,
        server_id=server_id or getattr(api_client, 'server_id', None),
        source='customer',
    )
    if current_cycle is not None:
        matching_records = [
            (record_id, record)
            for record_id, record in matching_records
            if record_id == current_cycle.record_id
        ]

    first_ineligible_offer = None
    for record_id, record in matching_records:
        record_username = _record_username(record)
        record_server_id = _record_server_id(record) or server_id or getattr(api_client, 'server_id', None)
        token = customer_renewal_token(user_id, record_id, record_username, record_server_id)
        offer = _build_offer(
            record,
            'customer',
            record_username,
            record_server_id,
            api_client,
            user_data,
            plans,
            extra={
                'token': token,
                'base_record_id': record_id,
                'base_record': record,
            },
            allow_reservation=allow_reservation,
            cycle_records=cycle_records,
        )
        if offer.get('eligible'):
            return offer
        if first_ineligible_offer is None:
            first_ineligible_offer = offer
    if first_ineligible_offer:
        return first_ineligible_offer
    return {
        'eligible': False,
        'reason': 'renewal_ineligible_no_record',
        'source': 'customer',
        'username': username,
        'server_id': server_id or getattr(api_client, 'server_id', None),
        'before_state': capture_user_state(user_data),
    }


def resolve_customer_renewal_token(
    user_id,
    token,
    plans,
    multi_api=None,
    payments=None,
    allow_reservation=True,
):
    from utils.api_client import MultiServerAPI

    multi_api = multi_api or MultiServerAPI()
    payments = payments if payments is not None else _load_json_file(PAYMENTS_FILE, {})
    matching_records = _matching_customer_records(user_id, payments=payments)
    for record_id, record in matching_records:
        username = _record_username(record)
        server_id = _record_server_id(record)
        if customer_renewal_token(user_id, record_id, username, server_id) != token:
            continue
        exact_records = {
            candidate_id: candidate
            for candidate_id, candidate in matching_records
            if _record_username(candidate).lower() == username.lower()
            and str(_record_server_id(candidate) or 'primary').lower()
            == str(server_id or 'primary').lower()
        }
        current_cycle = resolve_service_cycle(
            exact_records,
            username=username,
            server_id=server_id,
            source='customer',
        )
        if current_cycle is not None and current_cycle.record_id != record_id:
            return {
                'eligible': False,
                'reason': 'renewal_ineligible_cycle_changed',
                'source': 'customer',
                'username': username,
                'server_id': server_id,
            }
        api_client, user_data, lookup_result = lookup_renewal_user(
            multi_api,
            username,
            server_id=server_id,
        )
        return _build_offer(
            record,
            'customer',
            username,
            server_id,
            api_client,
            user_data,
            plans,
            extra={
                'token': token,
                'base_record_id': record_id,
                'base_record': record,
            },
            allow_reservation=allow_reservation,
            lookup_result=lookup_result,
            cycle_records=exact_records,
        )
    return {'eligible': False, 'reason': 'renewal_ineligible_missing', 'source': 'customer'}


def _iter_reseller_configs(reseller_id, reseller_data=None):
    if reseller_data is None:
        resellers = _load_json_file(RESELLERS_FILE, {})
        reseller_data = resellers.get(str(reseller_id), {})
    configs = reseller_data.get('configs', []) if isinstance(reseller_data, dict) else []
    if not isinstance(configs, list):
        return []
    return [(index, config) for index, config in enumerate(configs) if isinstance(config, dict)]


def find_reseller_renewal_offer(
    reseller_id,
    config_index,
    api_client,
    user_data,
    plans,
    reseller_data=None,
    allow_reservation=False,
    lookup_result=None,
):
    configs = dict(_iter_reseller_configs(reseller_id, reseller_data=reseller_data))
    config = configs.get(config_index)
    if not config or _is_deleted_record(config):
        return {'eligible': False, 'reason': 'renewal_ineligible_missing', 'source': 'reseller_customer'}

    existing_reservation = find_reseller_reservation(config)
    if existing_reservation:
        return {
            'eligible': False,
            'reason': 'renewal_already_reserved',
            'source': 'reseller_customer',
            'username': config.get('username'),
            'server_id': config.get('server_id'),
            'reservation': dict(existing_reservation),
        }

    username = str(config.get('username') or '').strip()
    server_id = config.get('server_id') or getattr(api_client, 'server_id', None)
    token = reseller_renewal_token(reseller_id, config_index, username, server_id)
    return _build_offer(
        config,
        'reseller_customer',
        username,
        server_id,
        api_client,
        user_data,
        plans,
        extra={
            'token': token,
            'reseller_id': str(reseller_id),
            'config_index': config_index,
            'config': config,
        },
        reseller_data=reseller_data,
        allow_reservation=allow_reservation,
        lookup_result=lookup_result,
    )


def resolve_reseller_renewal_token(
    reseller_id,
    token,
    plans,
    multi_api=None,
    reseller_data=None,
    allow_reservation=True,
):
    from utils.api_client import MultiServerAPI

    multi_api = multi_api or MultiServerAPI()
    if reseller_data is None:
        resellers = _load_json_file(RESELLERS_FILE, {})
        reseller_data = resellers.get(str(reseller_id), {})

    for config_index, config in _iter_reseller_configs(reseller_id, reseller_data=reseller_data):
        if _is_deleted_record(config):
            continue
        username = str(config.get('username') or '').strip()
        server_id = config.get('server_id')
        if reseller_renewal_token(reseller_id, config_index, username, server_id) != token:
            continue
        api_client, user_data, lookup_result = lookup_renewal_user(
            multi_api,
            username,
            server_id=server_id,
        )
        return find_reseller_renewal_offer(
            reseller_id,
            config_index,
            api_client,
            user_data,
            plans,
            reseller_data=reseller_data,
            allow_reservation=allow_reservation,
            lookup_result=lookup_result,
        )

    return {'eligible': False, 'reason': 'renewal_ineligible_missing', 'source': 'reseller_customer'}


def customer_payment_metadata(offer):
    return {
        'type': 'renewal',
        'renewal_source': 'customer',
        'renewal_username': offer.get('username'),
        'renewal_server_id': offer.get('server_id'),
        'renewal_base_record_id': offer.get('base_record_id'),
        'renewal_before_state': offer.get('before_state'),
        'renewal_mode': offer.get('renewal_mode', 'immediate'),
        'renewal_business_expired': bool(offer.get('business_expired')),
        'renewal_cycle_fingerprint': offer.get('cycle_fingerprint'),
        'renewal_entitlement_deadline': offer.get('entitlement_deadline'),
        'renewal_discount_percent': offer.get('renewal_discount_percent'),
        'renewal_discount_amount': offer.get('renewal_discount_amount'),
        'renewal_plan_snapshot': {
            'plan_gb': offer.get('plan_gb'),
            'days': offer.get('days'),
            'unlimited': offer.get('unlimited', False),
            'price': offer.get('price'),
            'full_price': offer.get('full_price'),
            'reseller_level': offer.get('reseller_level'),
            'discount_percent': offer.get('discount_percent'),
            'renewal_discount_percent': offer.get('renewal_discount_percent'),
            'renewal_discount_amount': offer.get('renewal_discount_amount'),
        },
        'renewal_baseline': offer.get('before_state'),
    }


def reseller_renewal_record(offer, before_state, after_state):
    return {
        'timestamp': _now_str(),
        'price': offer.get('price'),
        'list_price': offer.get('full_price'),
        'reseller_level': offer.get('reseller_level'),
        'discount_percent': offer.get('discount_percent'),
        'gb': offer.get('plan_gb'),
        'days': offer.get('days'),
        'unlimited': offer.get('unlimited', False),
        'before_state': before_state,
        'after_state': after_state,
        'renewal_mode': offer.get('renewal_mode', 'immediate'),
        'renewal_business_expired': bool(offer.get('business_expired')),
        'renewal_cycle_fingerprint': offer.get('cycle_fingerprint'),
        'renewal_entitlement_deadline': offer.get('entitlement_deadline'),
    }


def reserved_renewal_record(offer, reservation_id=None, funded=False):
    """Build the durable fulfillment record shared by debt and funded reservations."""
    return {
        **reseller_renewal_record(offer, offer.get('before_state'), None),
        'reservation_id': str(reservation_id or uuid.uuid4().hex),
        'renewal_mode': 'reserved',
        'renewal_status': 'reserved',
        'renewal_reserved_at': _now_str(),
        'renewal_baseline': dict(offer.get('before_state') or {}),
        'renewal_plan_snapshot': {
            'plan_gb': offer.get('plan_gb'),
            'days': offer.get('days'),
            'unlimited': offer.get('unlimited', False),
            'price': offer.get('price'),
            'full_price': offer.get('full_price'),
            'reseller_level': offer.get('reseller_level'),
            'discount_percent': offer.get('discount_percent'),
        },
        'funded_at_checkout': bool(funded),
        'renewal_attempts': 0,
    }


def mark_payment_renewal_reserved(payment_id, payments_file=None, fields=None, now=None):
    path = payments_file or PAYMENTS_FILE
    current = now or datetime.now()
    timestamp = current.strftime(TIMESTAMP_FORMAT)
    with locked_json(path, {}) as payments:
        record = payments.get(str(payment_id)) if isinstance(payments, dict) else None
        if not isinstance(record, dict):
            return False
        if record.get('renewal_status') == 'applied':
            return True
        if (
            record.get('status') == 'completed'
            and record.get('renewal_mode') == 'reserved'
            and record.get('renewal_status') in RESERVATION_ACTIVE_STATUSES
        ):
            record.update(dict(fields or {}))
            record['updated_at'] = timestamp
            return True
        username = _record_username(record)
        server_id = _record_server_id(record)
        user_id = record.get('user_id')
        for other_id, other in payments.items():
            if str(other_id) == str(payment_id) or not isinstance(other, dict):
                continue
            if str(other.get('user_id')) != str(user_id):
                continue
            if _reservation_matches(other, username, server_id=server_id):
                return False
        previous_status = record.get('status', 'unknown')
        record.update(dict(fields or {}))
        record['status'] = 'completed'
        record.setdefault('completed_at', timestamp)
        record['renewal_mode'] = 'reserved'
        record['renewal_status'] = 'reserved'
        record.setdefault('renewal_reserved_at', timestamp)
        record.setdefault('renewal_baseline', record.get('renewal_before_state') or {})
        record.setdefault('renewal_attempts', 0)
        record['updated_at'] = timestamp
        record.pop('renewal_claim_id', None)
        record.pop('renewal_claimed_at', None)
        record.pop('renewal_next_attempt_at', None)
        updates = record.setdefault('updates', [])
        if not isinstance(updates, list):
            updates = []
            record['updates'] = updates
        updates.append({
            'status': 'completed',
            'timestamp': timestamp,
            'previous_status': previous_status,
            'renewal_status': 'reserved',
        })
        return True


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith('Z'):
        raw = f"{raw[:-1]}+00:00"
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _claim_is_live(record, current, lease_seconds):
    claimed_at = _parse_time(record.get('renewal_claimed_at'))
    if claimed_at is None:
        return False
    age = (current - claimed_at).total_seconds()
    return 0 <= age < lease_seconds


def _retry_is_due(record, current):
    next_attempt = _parse_time(record.get('renewal_next_attempt_at'))
    return next_attempt is None or next_attempt <= current


def claim_payment_renewal(
    payment_id,
    payments_file=None,
    now=None,
    force=False,
    lease_seconds=RESERVATION_CLAIM_LEASE_SECONDS,
):
    path = payments_file or PAYMENTS_FILE
    current = now or datetime.now()
    timestamp = current.strftime(TIMESTAMP_FORMAT)
    with locked_json(path, {}) as payments:
        record = payments.get(str(payment_id)) if isinstance(payments, dict) else None
        if not isinstance(record, dict) or record.get('renewal_mode') != 'reserved':
            return None
        status = str(record.get('renewal_status') or '')
        if status == 'processing' and _claim_is_live(record, current, lease_seconds):
            return None
        if status == 'attention':
            reason = str(record.get('renewal_attention_reason') or '')
            if not force and (reason == 'external_renewal' or not _retry_is_due(record, current)):
                return None
        elif status not in {'reserved', 'processing'}:
            return None
        claim_id = uuid.uuid4().hex
        record['renewal_status'] = 'processing'
        record['renewal_claim_id'] = claim_id
        record['renewal_claimed_at'] = timestamp
        record['renewal_processing_from'] = status
        record['updated_at'] = timestamp
        return {'claim_id': claim_id, 'payment_id': str(payment_id), 'record': dict(record)}


def finish_payment_renewal(
    payment_id,
    claim_id,
    status,
    payments_file=None,
    fields=None,
    now=None,
    retry=False,
):
    if status not in {'reserved', 'attention', 'applied'}:
        return False
    path = payments_file or PAYMENTS_FILE
    current = now or datetime.now()
    timestamp = current.strftime(TIMESTAMP_FORMAT)
    with locked_json(path, {}) as payments:
        record = payments.get(str(payment_id)) if isinstance(payments, dict) else None
        if not isinstance(record, dict) or record.get('renewal_claim_id') != str(claim_id):
            return False
        record.update(dict(fields or {}))
        record['renewal_status'] = status
        record['updated_at'] = timestamp
        record.pop('renewal_claim_id', None)
        record.pop('renewal_claimed_at', None)
        record.pop('renewal_processing_from', None)
        if status == 'applied':
            record['renewal_applied_at'] = timestamp
            record.pop('renewal_next_attempt_at', None)
            record.pop('renewal_attention_reason', None)
            record.pop('renewal_last_error', None)
            record.pop('renewal_api_error', None)
            record.pop('renewal_api_http_status', None)
        elif status == 'reserved':
            record.pop('renewal_next_attempt_at', None)
            record.pop('renewal_attention_reason', None)
            record.pop('renewal_last_error', None)
            record.pop('renewal_api_error', None)
            record.pop('renewal_api_http_status', None)
        elif retry:
            attempts = max(0, _safe_int(record.get('renewal_attempts'), 0) or 0) + 1
            record['renewal_attempts'] = attempts
            record['renewal_next_attempt_at'] = (
                current + timedelta(seconds=RESERVATION_RETRY_SECONDS)
            ).strftime(TIMESTAMP_FORMAT)
        return True


def list_payment_renewal_ids(payments_file=None):
    payments = _load_json_file(payments_file or PAYMENTS_FILE, {})
    return [
        str(payment_id)
        for payment_id, record in (payments or {}).items()
        if isinstance(record, dict)
        and record.get('status') == 'completed'
        and record.get('renewal_mode') == 'reserved'
        and record.get('renewal_status') in RESERVATION_ACTIVE_STATUSES
    ]


def reservation_generation_changed(record, user_data):
    baseline = record.get('renewal_baseline') or record.get('renewal_before_state') or {}
    if not isinstance(baseline, dict) or not isinstance(user_data, dict):
        return False
    baseline_creation = _parse_account_creation_time(baseline.get('account_creation_date'))
    live_creation = _parse_account_creation_time(user_data.get('account_creation_date'))
    if baseline_creation is not None and live_creation is not None and live_creation != baseline_creation:
        return True
    baseline_deadline = _parse_time(baseline.get('expiration_deadline'))
    live_deadline = _expiration_deadline(user_data)
    if baseline_deadline is not None and live_deadline is not None and live_deadline > baseline_deadline + timedelta(seconds=1):
        return True
    baseline_days = _safe_int(baseline.get('expiration_days'))
    live_days = _safe_int(user_data.get('expiration_days'))
    if baseline_days is not None and live_days is not None and live_days > baseline_days:
        return True
    baseline_limit = _safe_bytes(baseline.get('max_download_bytes'))
    live_limit = _safe_bytes(user_data.get('max_download_bytes'))
    if (
        baseline.get('max_download_bytes') is not None
        and user_data.get('max_download_bytes') is not None
        and baseline_limit != live_limit
    ):
        return True
    baseline_used = _safe_bytes(baseline.get('upload_bytes')) + _safe_bytes(baseline.get('download_bytes'))
    live_used = _safe_bytes(user_data.get('upload_bytes')) + _safe_bytes(user_data.get('download_bytes'))
    return live_used < baseline_used


def inspect_reserved_renewal(record, user_data, force_apply=False, lookup_result=None):
    if not isinstance(user_data, dict):
        return {
            'action': 'attention',
            'reason': _lookup_failure_reason(lookup_result),
            'retry': True,
        }
    if force_apply:
        return {'action': 'apply'}
    if reservation_generation_changed(record, user_data):
        return {'action': 'attention', 'reason': 'external_renewal', 'retry': False}
    if is_user_expired(user_data):
        return {'action': 'apply'}
    return {'action': 'wait'}


def reservation_expected_time_expired(record, now=None):
    baseline = record.get('renewal_baseline') or record.get('renewal_before_state') or {}
    if not isinstance(baseline, dict):
        return False
    current = now or datetime.now()
    deadline = _parse_time(baseline.get('expiration_deadline'))
    if deadline is not None:
        return current >= deadline
    captured_at = _parse_time(baseline.get('captured_at'))
    days_remaining = _safe_int(baseline.get('days_remaining'))
    if captured_at is None or days_remaining is None or days_remaining < 0:
        return False
    return current >= captured_at + timedelta(days=days_remaining)


def reservation_alert_due(record, now=None, reminder_seconds=86400, audience=None):
    current = now or datetime.now()
    field = f'renewal_last_{audience}_alert_at' if audience in {'operator', 'buyer'} else 'renewal_last_alert_at'
    last_value = record.get(field)
    if last_value is None and audience in {'operator', 'buyer'}:
        last_value = record.get('renewal_last_alert_at')
    last_alert = _parse_time(last_value)
    return last_alert is None or (current - last_alert).total_seconds() >= reminder_seconds


def reservation_alert_flags(record, reason, now=None):
    operator_due = reservation_alert_due(record, now=now, audience='operator')
    buyer_allowed = reason != 'server_unavailable' or reservation_expected_time_expired(record, now=now)
    buyer_due = buyer_allowed and reservation_alert_due(record, now=now, audience='buyer')
    return {
        'operator_alert_due': operator_due,
        'buyer_alert_due': buyer_due,
        'alert_due': bool(operator_due or buyer_due),
    }


def refresh_payment_renewal_baseline(payment_id, user_data, payments_file=None):
    path = payments_file or PAYMENTS_FILE
    with locked_json(path, {}) as payments:
        record = payments.get(str(payment_id)) if isinstance(payments, dict) else None
        if not isinstance(record, dict) or record.get('renewal_status') != 'attention':
            return False
        record['renewal_baseline'] = capture_user_state(user_data)
        record['renewal_status'] = 'reserved'
        record['renewal_reviewed_at'] = _now_str()
        record.pop('renewal_attention_reason', None)
        record.pop('renewal_last_error', None)
        record.pop('renewal_next_attempt_at', None)
        record['updated_at'] = _now_str()
        return True


def mark_payment_renewal_alerted(payment_id, payments_file=None, now=None, audience=None):
    path = payments_file or PAYMENTS_FILE
    timestamp = (now or datetime.now()).strftime(TIMESTAMP_FORMAT)
    with locked_json(path, {}) as payments:
        record = payments.get(str(payment_id)) if isinstance(payments, dict) else None
        if not isinstance(record, dict):
            return False
        if audience in {'operator', 'buyer'}:
            record[f'renewal_last_{audience}_alert_at'] = timestamp
        else:
            record['renewal_last_alert_at'] = timestamp
            record['renewal_last_operator_alert_at'] = timestamp
            record['renewal_last_buyer_alert_at'] = timestamp
        record['updated_at'] = timestamp
        return True


def process_payment_renewal_reservation(
    payment_id,
    payments_file=None,
    multi_api=None,
    now=None,
    force=False,
    force_apply=False,
):
    from utils.api_client import MultiServerAPI

    path = payments_file or PAYMENTS_FILE
    current = now or datetime.now()
    claim = claim_payment_renewal(
        payment_id,
        payments_file=path,
        now=current,
        force=force or force_apply,
    )
    if not claim:
        return None
    record = claim['record']
    username = _record_username(record)
    server_id = _record_server_id(record)
    multi_api = multi_api or MultiServerAPI()
    api_client, user_data, lookup_result = lookup_renewal_user(
        multi_api,
        username,
        server_id=server_id,
    )
    inspection = inspect_reserved_renewal(
        record,
        user_data,
        force_apply=force_apply,
        lookup_result=lookup_result,
    )
    if inspection['action'] == 'wait':
        finish_payment_renewal(
            payment_id,
            claim['claim_id'],
            'reserved',
            payments_file=path,
            now=current,
        )
        return {'payment_id': str(payment_id), 'status': 'waiting', 'record': record}
    if inspection['action'] == 'attention':
        reason = inspection.get('reason')
        alert_flags = reservation_alert_flags(record, reason, now=current)
        finish_payment_renewal(
            payment_id,
            claim['claim_id'],
            'attention',
            payments_file=path,
            now=current,
            retry=bool(inspection.get('retry')),
            fields={
                'renewal_attention_reason': reason,
                'renewal_last_error': reason,
                'renewal_live_state': capture_user_state(user_data) if user_data else None,
                **_lookup_failure_fields(lookup_result),
            },
        )
        return {
            'payment_id': str(payment_id),
            'status': 'attention',
            'reason': reason,
            'retry': bool(inspection.get('retry')),
            **alert_flags,
            'record': record,
            'user_data': user_data,
            'lookup_result': lookup_result,
        }

    result = execute_reserved_renewal(record, multi_api=multi_api, force=force_apply)
    if not result.get('success'):
        reason = result.get('reason') or 'renewal_reset_failed'
        alert_flags = reservation_alert_flags(record, reason, now=current)
        finish_payment_renewal(
            payment_id,
            claim['claim_id'],
            'attention',
            payments_file=path,
            now=current,
            retry=True,
            fields={
                'renewal_attention_reason': reason,
                'renewal_last_error': reason,
                'renewal_before_state': result.get('before_state', record.get('renewal_before_state')),
                **_lookup_failure_fields(result.get('lookup_result')),
            },
        )
        return {
            'payment_id': str(payment_id),
            'status': 'attention',
            'reason': reason,
            'retry': True,
            **alert_flags,
            'record': record,
            'result': result,
        }

    persisted = finish_payment_renewal(
        payment_id,
        claim['claim_id'],
        'applied',
        payments_file=path,
        now=current,
        fields={
            'renewal_before_state': result.get('before_state'),
            'renewal_after_state': result.get('after_state'),
            'username': result.get('username'),
            'server_id': result.get('server_id'),
        },
    )
    if persisted:
        mark_cleanup_state_renewed(result.get('username'), result.get('server_id'))
    return {
        'payment_id': str(payment_id),
        'status': 'applied',
        'record': record,
        'result': result,
        'api_client': result.get('api_client') or api_client,
    }


def process_reseller_renewal_reservation(
    reseller_id,
    reservation_id,
    multi_api=None,
    now=None,
    force=False,
    force_apply=False,
):
    from utils.api_client import MultiServerAPI
    from utils.reseller import (
        claim_reseller_renewal_reservation,
        finish_reseller_renewal_reservation,
        is_reseller_debt_charge_paid,
    )

    current = now or datetime.now()
    claim = claim_reseller_renewal_reservation(
        reseller_id,
        reservation_id,
        now=current,
        force=force or force_apply,
        lease_seconds=RESERVATION_CLAIM_LEASE_SECONDS,
    )
    if not claim:
        return None
    reservation = claim['reservation']
    config = claim['config']
    reseller_data = claim['reseller']
    record = {
        **reservation,
        'renewal_username': config.get('username'),
        'renewal_server_id': config.get('server_id'),
        'renewal_source': reservation.get('renewal_source') or 'reseller_customer',
    }
    multi_api = multi_api or MultiServerAPI()
    api_client, user_data, lookup_result = lookup_renewal_user(
        multi_api,
        record['renewal_username'],
        server_id=record.get('renewal_server_id'),
    )
    inspection = inspect_reserved_renewal(
        record,
        user_data,
        force_apply=force_apply,
        lookup_result=lookup_result,
    )
    if inspection['action'] == 'wait':
        finish_reseller_renewal_reservation(
            reseller_id,
            reservation_id,
            claim['claim_id'],
            'reserved',
            now=current,
        )
        return {'reservation_id': str(reservation_id), 'reseller_id': str(reseller_id), 'status': 'waiting', 'record': record}
    if inspection['action'] == 'attention':
        reason = inspection.get('reason')
        alert_flags = reservation_alert_flags(reservation, reason, now=current)
        finish_reseller_renewal_reservation(
            reseller_id,
            reservation_id,
            claim['claim_id'],
            'attention',
            now=current,
            retry=bool(inspection.get('retry')),
            fields={
                'renewal_attention_reason': reason,
                'renewal_last_error': reason,
                'renewal_live_state': capture_user_state(user_data) if user_data else None,
                **_lookup_failure_fields(lookup_result),
            },
        )
        return {
            'reservation_id': str(reservation_id),
            'reseller_id': str(reseller_id),
            'status': 'attention',
            'reason': reason,
            'retry': bool(inspection.get('retry')),
            **alert_flags,
            'record': record,
            'user_data': user_data,
            'lookup_result': lookup_result,
        }

    restricted = reseller_data.get('status') != 'approved'
    charge_id = reservation.get('debt_charge_id')
    funded = bool(reservation.get('funded_at_checkout')) or not charge_id
    charge_paid = funded or is_reseller_debt_charge_paid(reseller_data, charge_id)
    if restricted and not charge_paid and not force_apply:
        reason = 'reseller_debt_review'
        alert_flags = reservation_alert_flags(reservation, reason, now=current)
        finish_reseller_renewal_reservation(
            reseller_id,
            reservation_id,
            claim['claim_id'],
            'attention',
            now=current,
            retry=True,
            fields={
                'renewal_attention_reason': reason,
                'renewal_last_error': reason,
                'renewal_live_state': capture_user_state(user_data),
            },
        )
        return {
            'reservation_id': str(reservation_id),
            'reseller_id': str(reseller_id),
            'status': 'attention',
            'reason': reason,
            'retry': True,
            **alert_flags,
            'record': record,
        }

    result = execute_reserved_renewal(record, multi_api=multi_api, force=force_apply)
    if not result.get('success'):
        reason = result.get('reason') or 'renewal_reset_failed'
        alert_flags = reservation_alert_flags(reservation, reason, now=current)
        finish_reseller_renewal_reservation(
            reseller_id,
            reservation_id,
            claim['claim_id'],
            'attention',
            now=current,
            retry=True,
            fields={
                'renewal_attention_reason': reason,
                'renewal_last_error': reason,
                'before_state': result.get('before_state', reservation.get('before_state')),
                **_lookup_failure_fields(result.get('lookup_result')),
            },
        )
        return {
            'reservation_id': str(reservation_id),
            'reseller_id': str(reseller_id),
            'status': 'attention',
            'reason': reason,
            'retry': True,
            **alert_flags,
            'record': record,
            'result': result,
        }

    persisted = finish_reseller_renewal_reservation(
        reseller_id,
        reservation_id,
        claim['claim_id'],
        'applied',
        now=current,
        fields={
            'before_state': result.get('before_state'),
            'after_state': result.get('after_state'),
        },
    )
    if persisted:
        mark_cleanup_state_renewed(result.get('username'), result.get('server_id'))
    return {
        'reservation_id': str(reservation_id),
        'reseller_id': str(reseller_id),
        'status': 'applied',
        'record': record,
        'result': result,
        'api_client': result.get('api_client') or api_client,
    }


def mark_cleanup_state_renewed(username, server_id):
    key = _state_key(server_id, username)
    try:
        with locked_json(STATE_FILE, {}) as state:
            if isinstance(state, dict):
                state.pop(key, None)
    except (OSError, TypeError, ValueError):
        pass


def _mark_payment_record_renewed(record_id, after_state):
    if not record_id:
        return
    try:
        with locked_json(PAYMENTS_FILE, {}) as payments:
            record = payments.get(str(record_id)) if isinstance(payments, dict) else None
            if not isinstance(record, dict):
                return
            record['cleanup_status'] = 'renewed'
            record['cleanup_error'] = None
            record['cleanup_last_state'] = after_state
            record['updated_at'] = _now_str()
    except (OSError, TypeError, ValueError):
        return


def _execute_reset(
    username,
    server_id,
    plan_record,
    source,
    multi_api=None,
    require_expired=True,
    validate_plan=True,
    business_expired=False,
    clear_cleanup=True,
):
    from utils.api_client import MultiServerAPI

    multi_api = multi_api or MultiServerAPI()
    api_client, user_data, lookup_result = lookup_renewal_user(
        multi_api,
        username,
        server_id=server_id,
    )
    if not api_client or not user_data:
        return {
            'success': False,
            'reason': _lookup_failure_reason(lookup_result),
            'lookup_result': lookup_result,
        }

    before_state = capture_user_state(user_data)
    if require_expired and not (is_user_expired(user_data) or business_expired):
        return {'success': False, 'reason': 'renewal_ineligible_not_expired', 'before_state': before_state}

    if validate_plan and not _live_quota_matches_plan(
        user_data,
        plan_record.get('plan_gb') or plan_record.get('gb'),
    ):
        return {'success': False, 'reason': 'renewal_ineligible_plan_mismatch', 'before_state': before_state}

    reset_result_method = getattr(api_client, 'reset_user_result', None)
    if callable(reset_result_method):
        reset_outcome = reset_result_method(username)
        result = reset_outcome.get('data') if isinstance(reset_outcome, dict) else None
        reset_status = reset_outcome.get('status') if isinstance(reset_outcome, dict) else None
        if reset_status != 'succeeded':
            reason = 'server_unavailable' if reset_status == 'unavailable' else 'renewal_reset_failed'
            return {
                'success': False,
                'reason': reason,
                'before_state': before_state,
                'lookup_result': {
                    'status': 'unavailable' if reset_status == 'unavailable' else 'failed',
                    'http_status': reset_outcome.get('http_status') if isinstance(reset_outcome, dict) else None,
                    'error': reset_outcome.get('error') if isinstance(reset_outcome, dict) else 'reset_failed',
                },
            }
    else:
        result = api_client.reset_user(username)
        if result is None:
            return {'success': False, 'reason': 'renewal_reset_failed', 'before_state': before_state}

    after_user = api_client.get_user(username) or user_data
    after_state = capture_user_state(after_user)
    if clear_cleanup:
        mark_cleanup_state_renewed(username, server_id or getattr(api_client, 'server_id', None))

    return {
        'success': True,
        'username': username,
        'server_id': server_id or getattr(api_client, 'server_id', None),
        'api_client': api_client,
        'before_state': before_state,
        'after_state': after_state,
        'raw_result': result,
    }


def execute_customer_renewal(payment_record, plans=None, multi_api=None):
    from utils.edit_plans import load_plans

    plans = plans if plans is not None else load_plans()
    username = payment_record.get('renewal_username')
    server_id = payment_record.get('renewal_server_id')
    if not username:
        return {'success': False, 'reason': 'renewal_ineligible_missing'}

    plan, reason = _plan_for_record(payment_record, plans, 'customer')
    if reason:
        return {'success': False, 'reason': reason}

    result = _execute_reset(
        username,
        server_id,
        payment_record,
        'customer',
        multi_api=multi_api,
        business_expired=bool(payment_record.get('renewal_business_expired')),
    )
    if result.get('success'):
        _mark_payment_record_renewed(payment_record.get('renewal_base_record_id'), result.get('after_state'))
        _record_renewal_completed(payment_record, result, 'customer')
    return result


def execute_reseller_renewal(offer, multi_api=None):
    username = offer.get('username')
    server_id = offer.get('server_id')
    if not username:
        return {'success': False, 'reason': 'renewal_ineligible_missing'}
    result = _execute_reset(
        username,
        server_id,
        {'gb': offer.get('plan_gb')},
        'reseller_customer',
        multi_api=multi_api,
        business_expired=bool(offer.get('business_expired')),
        clear_cleanup=False,
    )
    if result.get('success'):
        _record_renewal_completed(offer, result, 'reseller_customer')
    return result


def execute_reserved_renewal(record, multi_api=None, force=False):
    username = _record_username(record)
    server_id = _record_server_id(record)
    if not username:
        return {'success': False, 'reason': 'renewal_ineligible_missing'}
    snapshot = record.get('renewal_plan_snapshot')
    if not isinstance(snapshot, dict):
        snapshot = {
            'plan_gb': record.get('plan_gb') or record.get('gb'),
            'days': record.get('days'),
            'unlimited': record.get('unlimited', False),
        }
    result = _execute_reset(
        username,
        server_id,
        snapshot,
        record.get('renewal_source') or 'reserved',
        multi_api=multi_api,
        require_expired=not force,
        validate_plan=not (force or record.get('renewal_reviewed_at')),
        clear_cleanup=False,
    )
    if result.get('success') and record.get('renewal_base_record_id'):
        _mark_payment_record_renewed(record.get('renewal_base_record_id'), result.get('after_state'))
    if result.get('success'):
        _record_renewal_completed(
            record,
            result,
            record.get('renewal_source') or 'reserved',
        )
    return result


def format_state_summary(state, language='en'):
    from utils.translations import get_message_text

    if not isinstance(state, dict):
        return get_message_text(language, 'renewal_state_summary').format(
            days_remaining=get_message_text(language, 'value_unknown'),
            gb_used=get_message_text(language, 'value_unknown'),
            gb_limit=get_message_text(language, 'value_unknown'),
        )
    gb_limit = state.get('gb_limit')
    gb_limit_text = (
        get_message_text(language, 'value_unlimited')
        if gb_limit is None
        else f"{_safe_float(gb_limit):.2f} GB"
    )
    days_remaining = state.get('days_remaining')
    return get_message_text(language, 'renewal_state_summary').format(
        days_remaining=(
            days_remaining
            if days_remaining is not None
            else get_message_text(language, 'value_unknown')
        ),
        gb_used=f"{_safe_float(state.get('gb_used')):.2f} GB",
        gb_limit=gb_limit_text,
    )


def format_renewal_offer(language, offer, include_payment_prompt=True):
    from utils.currency_format import format_usd_amount
    from utils.translations import get_message_text

    before = format_state_summary(offer.get('before_state'), language)
    after = format_state_summary(offer.get('expected_after_state'), language)
    renewal_discount_details = ''
    renewal_discount_percent = _safe_float(offer.get('renewal_discount_percent'))
    if offer.get('source') == 'customer' and renewal_discount_percent > 0:
        renewal_discount_details = get_message_text(
            language,
            'renewal_discount_offer_line',
        ).format(
            list_price=format_usd_amount(
                offer.get('full_price', offer.get('price', 0))
            ),
            percent=f"{renewal_discount_percent:g}",
            discount_amount=format_usd_amount(
                offer.get('renewal_discount_amount', 0)
            ),
        )
    payment_prompt = f"\n\n{get_message_text(language, 'select_payment_method')}" if include_payment_prompt else ""
    return get_message_text(language, 'renewal_offer_details').format(
        username=_escape_markdown(offer.get('username')),
        plan_gb=offer.get('plan_gb'),
        days=offer.get('days'),
        price=format_usd_amount(offer.get('price', 0)),
        list_price=format_usd_amount(offer.get('full_price', offer.get('price', 0))),
        reseller_level=offer.get('reseller_level') or 1,
        discount_percent=offer.get('discount_percent') or 0,
        renewal_discount_details=renewal_discount_details,
        before=before,
        after=after,
        payment_prompt=payment_prompt,
    )


def format_renewal_unavailable(language, offer):
    from utils.translations import get_message_text

    reason = (offer or {}).get('reason')
    reason_text = get_message_text(language, reason) if reason else ''
    if not reason_text or reason_text == reason:
        reason_text = get_message_text(language, 'renewal_generic_unavailable_reason')
    return get_message_text(language, 'renewal_unavailable').format(reason=reason_text)


def format_renewal_success(language, result, plan_gb, days, sub_url=None, ipv4_url=None):
    from utils.translations import get_message_text

    ipv4_info = (
        get_message_text(language, 'renewal_ipv4_line').format(ipv4_url=ipv4_url)
        if ipv4_url
        else ""
    )
    return get_message_text(language, 'renewal_success').format(
        username=_escape_markdown(result.get('username')),
        plan_gb=plan_gb,
        days=days,
        before=format_state_summary(result.get('before_state'), language),
        after=format_state_summary(result.get('after_state'), language),
        sub_url=sub_url or get_message_text(language, 'value_not_available'),
        ipv4_info=ipv4_info,
    )
