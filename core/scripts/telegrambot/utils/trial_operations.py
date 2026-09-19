"""Durable trial allocation and shared, atomic trial completion."""
import json
import secrets

from . import account_operations as operations, database
from .atomic_store import locked_json, read_json
from .time_utils import format_utc_timestamp
from .trial_state import _has_used_test_config_from, _mark_test_config_used_in_memory

CONFIGS = '/etc/ajib/core/scripts/telegrambot/test_configs.json'
WAITING = '/etc/ajib/core/scripts/telegrambot/waiting_test_users.json'


def _resources(user_id, entry):
    resources = {('trial', str(user_id))}
    for name, server in (('username', 'server_id'), ('replacement_from_username', 'replacement_from_server_id')):
        if entry.get(name):
            resources.add((str(entry.get(server) or 'primary'), str(entry[name]).casefold()))
    return [list(item) for item in sorted(resources)]


def verify_replacement(panels, entry):
    """Refresh replacement eligibility while the original account is claimed."""
    if not entry.get('replacement_eligible_at'):
        return
    username, server = entry.get('replacement_from_username'), entry.get('replacement_from_server_id')
    if (username, server, entry.get('replacement_from_used_at')) != (
            entry.get('username'), entry.get('server_id'), entry.get('used_at')):
        raise operations.AccountBusy('Replacement identity changed')
    _, user, lookup = panels.resolve_unique_user(username, preferred_server_id=server)
    from .account_state import inspect_account, PanelState
    if not user or lookup.get('status') != 'found' or not lookup.get('uniqueness_verified'):
        raise operations.AccountBusy('Replacement identity requires investigation')
    if inspect_account(user).panel_state != PanelState.HOLD or any(
        isinstance(user.get(field), bool) or str(user.get(field)) != str(expected)
        for field, expected in (('expiration_days', 30), ('max_download_bytes', 1024**3),
                                ('upload_bytes', 0), ('download_bytes', 0))
    ):
        raise operations.AccountBusy('The original trial is no longer unused')


def claim(user_id, *, scope='main', owner='bot', language=None, web_id=None):
    resources = _resources(user_id, read_json(CONFIGS, {}).get(str(user_id)) or {})
    with operations.serialize_many(resources), database.transaction(operation='trial_operation_claim') as db:
        if web_id:
            changed = db.execute("UPDATE web_trials SET status='processing',updated_at=strftime('%s','now') WHERE id=? AND user_id=? AND status='queued'",
                                 (web_id, str(user_id))).rowcount
            if changed != 1:
                raise operations.AccountBusy('The web trial is no longer queued for this owner')
        with locked_json(CONFIGS, {}) as configs:
            key = str(user_id)
            if _resources(user_id, configs.get(key) or {}) != resources:
                raise operations.AccountBusy('Trial identity changed before its claim')
            for server, name in resources:
                operations.assert_available(server, name)
            if (configs.get(key) or {}).get('creation_pending_at'):
                raise operations.AccountBusy('A prior trial creation requires investigation')
            if _has_used_test_config_from(configs, key):
                raise operations.AccountBusy('A trial exists or its creation requires investigation')
            entry = dict(configs.get(key) or {})
            ident = 'trial:' + (web_id or secrets.token_hex(16))
            entry.update(telegram_id=int(user_id), creation_pending_at=format_utc_timestamp(),
                         account_operation_id=ident, trial_scope=scope, trial_owner=owner,
                         trial_resources=resources)
            if language:
                entry['language'] = language
            if web_id:
                entry['web_creation_pending'] = web_id
            if scope.startswith('hosted:'):
                entry['reseller_id'] = scope.removeprefix('hosted:')
            configs[key] = entry
            return ident


def create(ident, user_id, panels, allocator, plan):
    from .account_mutations import create as create_account
    entry = read_json(CONFIGS, {}).get(str(user_id)) or {}
    if entry.get('account_operation_id') != ident:
        raise operations.AccountBusy('Trial claim identity changed')
    resources = _resources(user_id, entry)
    if entry.get('trial_resources') != resources:
        raise operations.AccountBusy('Trial resource provenance requires investigation')
    origin = {'type': 'trial', 'scope': entry['trial_scope'], 'owner': entry['trial_owner'],
              'id': ident, 'user_id': str(user_id), 'web_id': entry.get('web_creation_pending'),
              'replacement': {key: entry.get(key) for key in ('replacement_eligible_at',
                  'replacement_from_username', 'replacement_from_server_id', 'replacement_from_used_at')}}

    def allocated(username, client):
        with locked_json(CONFIGS, {}) as configs:
            current = configs.get(str(user_id)) or {}
            if (current.get('account_operation_id') != ident or _resources(user_id, current) != resources
                    or current.get('trial_resources') != resources):
                raise operations.AccountBusy('Trial claim identity changed')
            current.update(pending_username=username, pending_server_id=client.server_id)
        verify_replacement(panels, current)
    return create_account(ident, panels, allocator, plan, origin=origin, note_text='test_config',
                          on_allocated=allocated, resources=resources)


def complete(ident, *, notify=False):
    """No panel requests; claim, history, waitlist and notification commit together."""
    with database.transaction(operation='trial_operation_complete') as db:
        row, metadata = operations.existing(ident), operations.details(ident)
        if not row or not metadata or row['status'] != 'succeeded':
            raise operations.AccountBusy('Trial panel outcome has not been verified')
        if metadata['phase'] == 'completed':
            return
        origin = json.loads(metadata['origin_json'])
        if origin.get('type') != 'trial':
            raise operations.AccountBusy('Operation is not a trial')
        key = origin['user_id']
        with locked_json(CONFIGS, {}) as configs:
            entry = configs.get(key) or {}
            if entry.get('account_operation_id') != ident or entry.get('trial_scope') != origin['scope']:
                raise operations.AccountBusy('Trial ownership changed')
            resources = _resources(key, entry)
            claimed = json.loads(metadata['resources_json'])
            if entry.get('trial_resources') != resources or any(item not in claimed for item in resources):
                raise operations.AccountBusy('Trial resource provenance requires investigation')
            language = entry.get('language', 'en')
            archived = _mark_test_config_used_in_memory(configs, int(key), username=row['username'],
                                                       server_id=row['server_id'], language=language)
            configs[key].pop('pending_username', None)
            configs[key].pop('pending_server_id', None)
            configs[key].pop('trial_resources', None)
        with locked_json(WAITING, {}) as waiting:
            waiting.pop(key, None)
        if origin.get('web_id'):
            db.execute("UPDATE web_trials SET status='completed',username=?,server_id=?,reason=NULL,updated_at=strftime('%s','now') WHERE id=? AND user_id=?",
                       (row['username'], row['server_id'], origin['web_id'], key))
        if archived:
            # The cleanup scheduler discovers this committed historical record.
            # It sends the notice and starts the grace period outside this transaction.
            operations.event(db, ident, 'superseded_trial_cleanup_pending')
        if notify:
            from . import web_store
            messages = {'en': 'Your trial is ready. Open My connections.',
                        'fa': 'سرویس آزمایشی شما آماده است. اتصال‌های من را باز کنید.',
                        'ru': 'Пробный сервис готов. Откройте «Мои подключения».',
                        'tk': 'Synag hyzmatyňyz taýýar. Birikmeleriňizi açyň.'}
            web_store.enqueue(db, ident, origin['scope'], key, messages.get(language, messages['en']))
        operations.complete(ident)


def release_unallocated(user_id):
    """Only a claim with no prepared panel operation can be abandoned."""
    with database.transaction(operation='trial_unallocated_release'):
        with locked_json(CONFIGS, {}) as configs:
            entry = configs.get(str(user_id)) or {}
            ident = entry.get('account_operation_id')
            if ident and operations.existing(ident):
                return False
            for field in ('account_operation_id', 'creation_pending_at', 'web_creation_pending', 'trial_resources'):
                entry.pop(field, None)
            return True
