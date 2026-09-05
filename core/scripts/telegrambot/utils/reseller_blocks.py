"""Durable, account-scoped temporary blocks shared by both bot runtimes.

Intent is committed before a panel mutation. The second transaction serializes
the mutation with admin/debt changes; an interrupted mutation remains retryable.
"""

import logging
import uuid
from copy import deepcopy
from datetime import timedelta
from contextlib import contextmanager

from utils import reseller as store
from utils.time_utils import utc_now, parse_utc_timestamp, format_utc_timestamp

LOG = logging.getLogger('ajib.reseller_blocks')
ACTIVE_STATES = {'pending', 'blocked', 'releasing'}


def _now(value=None):
    return parse_utc_timestamp(value) if value is not None else utc_now()


def _find(records, reseller_id, token, *, authorize=True):
    owner = records.get(str(reseller_id)) or {}
    if authorize and owner.get('status') not in {'approved', 'suspended'}:
        raise ValueError('denied')
    for config in owner.get('configs', []):
        if (isinstance(config, dict) and config.get('block_control_id') == token
                and not config.get('removed_from_vpn')):
            return owner, config
    raise ValueError('denied')


def customer_block_token(reseller_id, config_index):
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        owner = records.get(str(reseller_id)) or {}
        if owner.get('status') not in {'approved', 'suspended'}:
            raise ValueError('denied')
        configs = owner.get('configs', [])
        if not isinstance(config_index, int) or not 0 <= config_index < len(configs):
            raise ValueError('denied')
        config = configs[config_index]
        if not isinstance(config, dict) or config.get('removed_from_vpn') or not config.get('server_id'):
            raise ValueError('denied')
        if not config.get('block_control_id'):
            config['block_control_id'] = uuid.uuid4().hex
            store._write_resellers_file(records)
        return config['block_control_id']


def block_view(reseller_id, token):
    with store.reseller_lock, store._resellers_file_lock():
        _, config = _find(store._read_resellers_file(), reseller_id, token)
        return deepcopy(config)


def _temporary_active(config, now):
    block = config.get('reseller_block') or {}
    deadline = parse_utc_timestamp(block.get('until'))
    return block.get('state') in {'pending', 'blocked'} and deadline is not None and deadline > now


def _seed_reasons(config, live):
    if ('external_blocked' not in config and config.get('debt_policy_blocked')
            and not config.get('debt_policy_changed_blocked')
            and (config.get('debt_policy_hold_snapshot') or {}).get('blocked_before_policy')):
        config['external_blocked'] = True
        return
    owned = (config.get('block_reconcile_pending') or config.get('debt_policy_blocked')
             or config.get('admin_blocked') or (config.get('reseller_block') or {}).get('state') in ACTIVE_STATES)
    if not owned:
        config['external_blocked'] = bool(live.get('blocked'))
    config.setdefault('external_blocked', False if owned else bool(live.get('blocked')))


def _effective_block(owner, config, now):
    return bool(config.get('external_blocked') or config.get('admin_blocked')
                or config.get('debt_policy_blocked')
                or _temporary_active(config, now))


def _resolve(config, multi_api):
    username, server_id = config.get('username'), config.get('block_server_id') or config.get('server_id')
    if not username or not server_id:
        raise ValueError('denied')
    client, live, result = multi_api.resolve_unique_user(
        username, preferred_server_id=server_id, force_refresh=True,
        allow_exact_on_partial=False,
    )
    if (not client or not live or result.get('status') != 'found'
            or not result.get('uniqueness_verified')
            or str(client.server_id) != str(server_id)):
        raise RuntimeError('identity_unverified')
    return client, live


def request_block(reseller_id, token, hours, multi_api, *, now=None, request_id=None):
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
        raise ValueError('invalid')
    current = _now(now)
    config = block_view(reseller_id, token)
    client, live = _resolve(config, multi_api)
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        _, fresh = _find(records, reseller_id, token)
        if (fresh.get('username'), fresh.get('server_id')) != (config.get('username'), config.get('server_id')):
            raise ValueError('denied')
        existing = fresh.get('reseller_block') or {}
        if request_id and request_id in fresh.get('processed_block_requests', []):
            return deepcopy(existing)
        # Duplicate Telegram callbacks do not extend an existing block.
        if existing.get('state') in ACTIVE_STATES:
            return deepcopy(existing)
        _seed_reasons(fresh, live)
        if request_id:
            fresh.setdefault('processed_block_requests', []).append(request_id)
        fresh['reseller_block'] = {
            'id': uuid.uuid4().hex[:12], 'reseller_id': str(reseller_id),
            'username': fresh['username'], 'server_id': client.server_id,
            'requested_at': format_utc_timestamp(current),
            'until': format_utc_timestamp(current + timedelta(hours=hours)),
            'state': 'pending', 'attempts': 0,
        }
        fresh['block_reconcile_pending'] = True
        store._write_resellers_file(records)
    reconcile_block(reseller_id, token, multi_api, now=current)
    return block_view(reseller_id, token)['reseller_block']


def release_block(reseller_id, token, multi_api, *, now=None, expected_block_id=None):
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        _, config = _find(records, reseller_id, token)
        block = config.get('reseller_block') or {}
        if expected_block_id is not None and block.get('id') != expected_block_id:
            raise ValueError('denied')
        if block.get('state') not in ACTIVE_STATES:
            return deepcopy(block)
        block['state'] = 'releasing'
        config['block_reconcile_pending'] = True
        store._write_resellers_file(records)
    reconcile_block(reseller_id, token, multi_api, now=now)
    return block_view(reseller_id, token)['reseller_block']


def reconcile_block(reseller_id, token, multi_api, *, now=None):
    current = _now(now)
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        owner, config = _find(records, reseller_id, token, authorize=False)
        block = config.get('reseller_block') or {}
        if block.get('state') in ACTIVE_STATES:
            if (config.get('username'), config.get('block_server_id') or config.get('server_id')) != (block.get('username'), block.get('server_id')):
                block['last_error'] = 'identity_changed'
                store._write_resellers_file(records)
                return False
        try:
            client, live = _resolve(config, multi_api)
            desired = _effective_block(owner, config, current)
            if bool(live.get('blocked')) != desired:
                if client.update_user(config['username'], {'blocked': desired}) is None:
                    raise RuntimeError('panel_update_failed')
                confirmed = client.get_user(config['username'])
                if not confirmed or confirmed.get('blocked') is not desired:
                    raise RuntimeError('panel_confirmation_pending')
            if block.get('state') in ACTIVE_STATES:
                block['state'] = 'blocked' if _temporary_active(config, current) else 'complete'
                block['other_block'] = desired if block['state'] == 'complete' else False
                block.pop('last_error', None)
            config['block_reconcile_pending'] = False
            config['block_last_error'] = None
            config['block_updated_at'] = format_utc_timestamp(current)
            store._write_resellers_file(records)
            return True
        except Exception as error:
            if block:
                block['attempts'] = int(block.get('attempts', 0)) + 1
                block['last_error'] = type(error).__name__
            config['block_reconcile_pending'] = True
            config['block_last_error'] = type(error).__name__
            store._write_resellers_file(records)
            LOG.warning('block_retry reseller=%s token=%s reason=%s', reseller_id, token, type(error).__name__)
            return False


def process_due_blocks(multi_api=None, *, now=None, owner_id=None):
    if multi_api is None:
        from utils.api_client import MultiServerAPI
        multi_api = MultiServerAPI()
    current = _now(now)
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        due = []
        for reseller_id, owner in records.items():
            if owner_id is not None and str(reseller_id) != str(owner_id):
                continue
            for config in owner.get('configs', []):
                if not isinstance(config, dict) or config.get('removed_from_vpn'):
                    continue
                block = config.get('reseller_block') or {}
                expired = block.get('state') in ACTIVE_STATES and not _temporary_active(config, current)
                if config.get('block_control_id') and (config.get('block_reconcile_pending') or expired):
                    due.append((reseller_id, config['block_control_id']))
    results = []
    for reseller_id, token in due:
        try:
            results.append(reconcile_block(reseller_id, token, multi_api, now=current))
        except ValueError:
            continue  # Account removed after the scan.
    return results


def set_owned_block_reason(reseller_id, config_index, client, live, *, reason, blocked):
    """Commit admin/debt intent and compose it with temporary block ownership."""
    if reason not in {'admin_blocked', 'debt_policy_blocked'}:
        raise ValueError('Unknown block reason')
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        owner = records.get(str(reseller_id)) or {}
        configs = owner.get('configs', [])
        if not 0 <= config_index < len(configs):
            return None
        config = configs[config_index]
        if config.get('username') != live.get('username', config.get('username')):
            return None
        if reason == 'admin_blocked' and str(config.get('server_id')) != str(client.server_id):
            return None
        config['block_server_id'] = client.server_id
        _seed_reasons(config, live)
        if reason == 'admin_blocked':
            config['external_blocked'] = False
        config[reason] = bool(blocked)
        token = config.setdefault('block_control_id', uuid.uuid4().hex)
        config['block_reconcile_pending'] = True
        store._write_resellers_file(records)
    # Debt/admin callers already performed an exact account lookup. Keep that
    # identity while serializing the panel write with timer reconciliation.
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        owner, config = _find(records, reseller_id, token, authorize=False)
        desired = _effective_block(owner, config, utc_now())
        result = (client.update_user(config['username'], {'blocked': desired})
                  if bool(live.get('blocked')) != desired else {'unchanged': True})
        if result is not None:
            config['block_reconcile_pending'] = False
            store._write_resellers_file(records)
        return result


def set_admin_block(client, username, blocked, live):
    with store.reseller_lock, store._resellers_file_lock():
        records = store._read_resellers_file()
        matches = [(owner_id, index) for owner_id, owner in records.items()
                   for index, config in enumerate(owner.get('configs', []))
                   if isinstance(config, dict) and not config.get('removed_from_vpn')
                   and config.get('username') == username and str(config.get('server_id')) == str(client.server_id)]
    if len(matches) == 1:
        return set_owned_block_reason(*matches[0], client, live, reason='admin_blocked', blocked=blocked)
    return client.update_user(username, {'blocked': blocked})


def _has_protected_block(records, username, server_id):
    for owner in records.values():
        for config in owner.get('configs', []):
            if (isinstance(config, dict) and not config.get('removed_from_vpn')
                    and config.get('username') == username
                    and str(server_id) in {str(config.get('block_server_id')), str(config.get('server_id'))}):
                if (config.get('admin_blocked') or config.get('debt_policy_blocked')
                        or config.get('external_blocked')
                        or (config.get('reseller_block') or {}).get('state') in ACTIVE_STATES):
                    return True
    return False


@contextmanager
def renewal_block_guard(username, server_id):
    """Serialize reset with block intent so a concurrent renewal cannot unblock."""
    if not store._resellers_store_exists():
        yield True
        return
    with store.reseller_lock, store._resellers_file_lock():
        yield not _has_protected_block(store._read_resellers_file(), username, server_id)
