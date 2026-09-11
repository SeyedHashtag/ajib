"""Durable main-store trials using the bot's shared eligibility and history."""
import time
import secrets
from . import database, web_store, test_config_store
from .atomic_store import locked_json, read_json
from .trial_state import _has_used_test_config_from, _mark_test_config_used_in_memory
from .time_utils import format_utc_timestamp
from .web_services import ServiceError

CONFIGS = '/etc/ajib/core/scripts/telegrambot/test_configs.json'
SETTINGS = '/etc/ajib/core/scripts/telegrambot/test_settings.json'
WAITING = '/etc/ajib/core/scripts/telegrambot/waiting_test_users.json'


def state(user_id, scope):
    if scope != 'main':
        return {'available': False, 'eligible': False, 'status': 'bot_only', 'account': None}
    configs = test_config_store.load_test_configs(CONFIGS)
    entry = configs.get(str(user_id)) or {}
    row = database.get_connection().execute('SELECT status FROM web_trials WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (str(user_id),)).fetchone()
    queued = str(user_id) in read_json(WAITING, {})
    disabled = bool(read_json(SETTINGS, {}).get('creation_disabled'))
    return {'available': True, 'eligible': not _has_used_test_config_from(configs, user_id),
            'creation_disabled': disabled, 'queued': queued,
            'status': row[0] if row else ('completed' if entry.get('username') else 'available'),
            'account': {k: entry[k] for k in ('username', 'server_id', 'used_at', 'connected_at') if k in entry} or None}


def request(user_id, scope, key, language):
    if scope != 'main':
        raise ServiceError('Request a trial through this storefront’s bot', 409)
    user_id = str(user_id)
    with database.transaction(operation='web_trial_request') as connection:
        row = connection.execute('SELECT id,status FROM web_trials WHERE user_id=? AND key=?', (user_id, key)).fetchone()
        if row:
            return dict(row)
        row = connection.execute("SELECT id,status FROM web_trials WHERE user_id=? AND status IN ('queued','processing','uncertain')", (user_id,)).fetchone()
        if row:
            return dict(row)
        if _has_used_test_config_from(test_config_store.load_test_configs(CONFIGS), user_id):
            raise ServiceError('A trial has already been issued or is being created', 409)
        trial_id = secrets.token_hex(16)
        now = int(time.time())
        connection.execute('INSERT INTO web_trials VALUES (?,?,?,?,?,?,?,?,?,?)',
            (trial_id, user_id, key, 'queued', language, None, None, now, now, None))
        with locked_json(WAITING, {}) as waiting:
            waiting.setdefault(user_id, {'telegram_id': int(user_id), 'language': language,
                                         'added_at': format_utc_timestamp()})
        web_store.audit(connection, user_id, 'main', 'trial.request', trial_id)
        return {'id': trial_id, 'status': 'queued'}


def connected(user_id, scope):
    if scope != 'main':
        raise ServiceError('Use this storefront’s bot', 409)
    with database.transaction(operation='web_trial_connected') as connection:
        with locked_json(CONFIGS, {}) as configs:
            entry = configs.get(str(user_id)) or {}
            if not entry.get('username') or not entry.get('used_at'):
                raise ServiceError('A trial has not been issued', 409)
            entry.setdefault('connected_at', format_utc_timestamp())
        web_store.audit(connection, user_id, scope, 'trial.connected', entry['username'])
    return {'connected': True}


def process_one(services):
    now = int(time.time())
    with database.transaction(operation='web_trial_claim') as connection:
        connection.execute("UPDATE web_trials SET status='uncertain',reason='worker_interrupted',updated_at=? WHERE status='processing' AND updated_at<?", (now, now-600))
        if read_json(SETTINGS, {}).get('creation_disabled'):
            return False
        row = connection.execute("SELECT * FROM web_trials WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if not row:
            return False
        with locked_json(CONFIGS, {}) as configs:
            entry = dict(configs.get(row['user_id']) or {})
            if _has_used_test_config_from(configs, row['user_id']):
                # A bot may have fulfilled the shared waitlist before this worker.
                if entry.get('username') and entry.get('used_at') and not entry.get('creation_pending_at'):
                    connection.execute("UPDATE web_trials SET status='completed',username=?,server_id=?,updated_at=? WHERE id=?", (entry['username'], entry.get('server_id'), now, row['id']))
                return False
            entry.update(creation_pending_at=format_utc_timestamp(), web_creation_pending=row['id'])
            configs[row['user_id']] = entry
        connection.execute("UPDATE web_trials SET status='processing',updated_at=? WHERE id=?", (now, row['id']))
    try:
        if entry.get('replacement_eligible_at'):
            _verify_replacement(services, entry)
        panel = services.panels.select_server_for_new_user()
        if not panel:
            raise RuntimeError('placement_unavailable')
        username = 't' + row['user_id'] + ''.join(chr(97+int(c,16)) for c in row['id'])
        with database.transaction(operation='web_trial_intent') as connection:
            connection.execute('UPDATE web_trials SET username=?,server_id=?,updated_at=? WHERE id=?', (username, panel.server_id, int(time.time()), row['id']))
        # Never repeat an external mutation after a timeout or unknown response.
        from .username_utils import build_user_note
        note = build_user_note(username=username, traffic_limit=1, expiration_days=30, unlimited=True, note_text='test_config')
        result = panel.add_user(username, 1, 30, unlimited=True, note=note)
        if not result:
            raise RuntimeError('panel_outcome_unknown')
        with database.transaction(operation='web_trial_complete') as connection:
            with locked_json(CONFIGS, {}) as configs:
                archived = _mark_test_config_used_in_memory(configs, int(row['user_id']), username=username,
                    language=row['language'], server_id=panel.server_id)
            with locked_json(WAITING, {}) as waiting:
                waiting.pop(row['user_id'], None)
            connection.execute("UPDATE web_trials SET status='completed',updated_at=? WHERE id=?", (int(time.time()), row['id']))
            web_store.audit(connection, row['user_id'], 'main', 'trial.completed', row['id'])
            web_store.enqueue(connection, 'trial:'+row['id'], 'main', row['user_id'], 'Your trial is ready. Open My connections on the website or in Telegram.')
        if archived:
            from .expired_cleanup import queue_superseded_test_cleanup
            queue_superseded_test_cleanup(telegram_user_id=int(row['user_id']), username=archived['username'],
                server_id=archived['server_id'], history_index=archived['history_index'], language=row['language'])
    except Exception as error:
        with database.transaction(operation='web_trial_attention') as connection:
            connection.execute("UPDATE web_trials SET status='uncertain',reason=?,updated_at=? WHERE id=? AND status='processing'", (type(error).__name__, int(time.time()), row['id']))
            web_store.audit(connection, row['user_id'], 'main', 'trial.attention', row['id'], {'error': type(error).__name__})
    return True


def _verify_replacement(services, entry):
    username, server_id = entry.get('replacement_from_username'), entry.get('replacement_from_server_id')
    if (username, server_id, entry.get('replacement_from_used_at')) != (entry.get('username'), entry.get('server_id'), entry.get('used_at')):
        raise RuntimeError('replacement_identity_changed')
    _, user, lookup = services.panels.resolve_unique_user(username, preferred_server_id=server_id)
    from .account_state import inspect_account, PanelState
    if not user or lookup.get('status') != 'found' or not lookup.get('uniqueness_verified'):
        raise RuntimeError('replacement_unverified')
    if inspect_account(user).panel_state != PanelState.HOLD or any(
        isinstance(user.get(field), bool) or str(user.get(field)) != str(expected)
        for field, expected in (('expiration_days',30), ('max_download_bytes',1024**3), ('upload_bytes',0), ('download_bytes',0))
    ):
        raise RuntimeError('replacement_no_longer_unused')
