import json
import os
import datetime
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from telebot import types
from utils.command import bot, is_admin
from utils.common import admin_action_text, create_main_markup
try:
    from utils.common import record_main_growth_event
except ImportError:
    def record_main_growth_event(*args, **kwargs):
        return False
from utils.api_client import MultiServerAPI
from utils.account_state import (
    PanelState,
    elapsed_full_days,
    inspect_account,
    parse_timestamp,
    remaining_full_days,
)
from utils.translations import BUTTON_TRANSLATIONS, get_message_text
from utils.language import get_user_language
import qrcode
import io
import logging
from utils.username_utils import (
    allocate_username,
    build_user_note,
    load_recorded_usernames,
    RecordedUsernameLoadError,
)
from utils.telegram_safe import safe_answer_callback_query, safe_edit_message_text, safe_send_message, safe_send_photo
from utils.download_guidance import send_download_prompt_safely
from utils import test_config_store
from utils.time_utils import format_utc_timestamp, parse_utc_timestamp, utc_now

TEST_CONFIGS_FILE = '/etc/ajib/core/scripts/telegrambot/test_configs.json'
TEST_SETTINGS_FILE = '/etc/ajib/core/scripts/telegrambot/test_settings.json'
TEST_WAITING_LIST_FILE = '/etc/ajib/core/scripts/telegrambot/waiting_test_users.json'
TEST_TRAFFIC_GB = 1
TEST_DAYS = 30
TEST_CREATION_CLAIM_TIMEOUT_MINUTES = 15
TEST_REPLACEMENT_ELIGIBILITY_DAYS = 30
TEST_STALE_CLEANUP_DAYS = 60
TEST_CONFIG_JOB_LOCK = threading.Lock()
TEST_CONFIG_INFLIGHT = set()


def _atomic_helpers():
    try:
        from utils.atomic_store import locked_json, read_json
        return locked_json, read_json
    except ImportError:
        return None


def _int_env(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


TEST_CONFIG_EXECUTOR = ThreadPoolExecutor(
    max_workers=_int_env("AJIB_TEST_CONFIG_WORKERS", 2),
    thread_name_prefix="ajib-test-config",
)

def load_test_settings():
    try:
        helpers = _atomic_helpers()
        if helpers:
            data = helpers[1](TEST_SETTINGS_FILE, {})
            return data if isinstance(data, dict) else {}
        if os.path.exists(TEST_SETTINGS_FILE):
            with open(TEST_SETTINGS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"creation_disabled": False}

def save_test_settings(settings):
    try:
        helpers = _atomic_helpers()
        if helpers:
            with helpers[0](TEST_SETTINGS_FILE, {}) as stored:
                if not isinstance(stored, dict):
                    raise ValueError("Test settings must contain a JSON object.")
                stored.clear()
                stored.update(settings if isinstance(settings, dict) else {})
        else:
            os.makedirs(os.path.dirname(TEST_SETTINGS_FILE), exist_ok=True)
            with open(TEST_SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=4)
    except Exception:
        pass

def is_test_creation_disabled():
    settings = load_test_settings()
    return settings.get("creation_disabled", False)

def load_test_configs():
    return test_config_store.load_test_configs(TEST_CONFIGS_FILE)

def save_test_configs(configs):
    test_config_store.save_test_configs(TEST_CONFIGS_FILE, configs)

def load_waiting_users():
    try:
        helpers = _atomic_helpers()
        if helpers:
            data = helpers[1](TEST_WAITING_LIST_FILE, {})
            return data if isinstance(data, dict) else {}
        if os.path.exists(TEST_WAITING_LIST_FILE):
            with open(TEST_WAITING_LIST_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    save_waiting_users({})
    return {}

def save_waiting_users(users):
    helpers = _atomic_helpers()
    if helpers:
        with helpers[0](TEST_WAITING_LIST_FILE, {}) as stored:
            if not isinstance(stored, dict):
                raise ValueError("Test waiting list must contain a JSON object.")
            stored.clear()
            stored.update(users if isinstance(users, dict) else {})
    else:
        os.makedirs(os.path.dirname(TEST_WAITING_LIST_FILE), exist_ok=True)
        with open(TEST_WAITING_LIST_FILE, 'w') as f:
            json.dump(users, f, indent=4)

def _parse_config_time(value):
    return parse_utc_timestamp(value)


def _strict_nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _exact_test_lookup(multi_api, username, server_id):
    """Resolve one recorded test account without cross-server fallback."""
    if not multi_api or not username or not server_id:
        return None, 'unavailable'
    strict_lookup = getattr(multi_api, 'find_user_on_server', None)
    if callable(strict_lookup):
        try:
            result = strict_lookup(username, server_id)
        except Exception:
            return None, 'unavailable'
        if isinstance(result, tuple) and len(result) == 3:
            _client, user_data, outcome = result
            outcome = outcome if isinstance(outcome, dict) else {}
            return user_data, outcome.get('status') or ('found' if user_data else 'unavailable')

    get_client = getattr(multi_api, 'get_client', None)
    client = get_client(server_id) if callable(get_client) else None
    if client is None:
        return None, 'unavailable'
    result_method = getattr(client, 'get_user_result', None)
    if callable(result_method):
        try:
            outcome = result_method(username)
        except Exception:
            return None, 'unavailable'
        outcome = outcome if isinstance(outcome, dict) else {}
        return outcome.get('data'), outcome.get('status') or 'unavailable'
    try:
        user_data = client.get_user(username)
        if user_data is not None:
            return user_data, 'found'
        users = client.get_users()
    except Exception:
        return None, 'unavailable'
    if users is None:
        return None, 'unavailable'
    if isinstance(users, dict):
        user_data = users.get(username)
        return user_data, 'found' if isinstance(user_data, dict) else 'missing'
    return None, 'missing'


def _verified_unused_hold(entry, user_data):
    if not isinstance(entry, dict) or not isinstance(user_data, dict):
        return False
    snapshot = inspect_account(user_data)
    if snapshot.panel_state != PanelState.HOLD:
        return False
    if _strict_nonnegative_int(user_data.get('expiration_days')) != TEST_DAYS:
        return False
    if _strict_nonnegative_int(user_data.get('max_download_bytes')) != TEST_TRAFFIC_GB * (1024 ** 3):
        return False
    return (
        _strict_nonnegative_int(user_data.get('upload_bytes')) == 0
        and _strict_nonnegative_int(user_data.get('download_bytes')) == 0
    )


def _replacement_identity(entry):
    if not isinstance(entry, dict):
        return None
    username = str(entry.get('username') or '').strip()
    server_id = str(entry.get('server_id') or '').strip()
    used_at = str(entry.get('used_at') or '').strip()
    if not username or not server_id or not used_at:
        return None
    return username, server_id, used_at


def _creation_claim_is_active(entry, now=None):
    if not isinstance(entry, dict):
        return False
    claimed_at = _parse_config_time(entry.get('creation_pending_at'))
    if claimed_at is None:
        return False
    current = parse_utc_timestamp(now) if now is not None else utc_now()
    return current - claimed_at < datetime.timedelta(
        minutes=TEST_CREATION_CLAIM_TIMEOUT_MINUTES
    )


def _has_used_test_config_from(configs, user_id, now=None):
    key = str(user_id)
    if key not in configs:
        return False
    entry = configs[key]
    if not isinstance(entry, dict):
        return False
    if _creation_claim_is_active(entry, now=now):
        return True
    if entry.get('creation_pending_at') and not any(
        entry.get(field) for field in ('used_at', 'username', 'historical_configs')
    ):
        return False
    if not any(entry.get(field) for field in ('used_at', 'username', 'historical_configs', 'reset_at')):
        return False
    reset_at_str = entry.get('reset_at')
    if reset_at_str:
        # User was reset — check if they have received a new test config since the reset
        used_at_str = entry.get('used_at')
        if used_at_str:
            used_at = _parse_config_time(used_at_str)
            reset_at = _parse_config_time(reset_at_str)
            if used_at is None or reset_at is None:
                return False
            # If used_at is older than reset_at, the user has not yet collected their new test config
            if used_at <= reset_at:
                return False
    return True

def has_used_test_config(user_id):
    return _has_used_test_config_from(load_test_configs(), user_id)


def get_test_config_journey(user_id, now=None):
    """Return the persisted onboarding state for a customer's free test."""
    entry = load_test_configs().get(str(user_id))
    if not isinstance(entry, dict) or not _has_used_test_config_from({str(user_id): entry}, user_id, now=now):
        return None
    connected_at = parse_timestamp(entry.get("connected_at"))
    current = parse_utc_timestamp(now) if now is not None else utc_now()
    used_at_aware = parse_timestamp(entry.get("used_at"))
    hold_elapsed_days = elapsed_full_days(used_at_aware, now=current)
    remaining_days = (
        remaining_full_days(
            connected_at + datetime.timedelta(days=TEST_DAYS),
            now=current,
        )
        if connected_at is not None
        else None
    )
    return {
        "used_at": entry.get("used_at"),
        "connected_at": entry.get("connected_at"),
        "remaining_days": remaining_days,
        "panel_state": "connected" if connected_at is not None else "hold",
        "hold_elapsed_days": hold_elapsed_days,
        "replacement_eligible": bool(
            connected_at is None
            and hold_elapsed_days is not None
            and TEST_REPLACEMENT_ELIGIBILITY_DAYS <= hold_elapsed_days < TEST_STALE_CLEANUP_DAYS
        ),
        "stale_cleanup_due": bool(
            connected_at is None
            and hold_elapsed_days is not None
            and hold_elapsed_days >= TEST_STALE_CLEANUP_DAYS
        ),
        "traffic_gb": TEST_TRAFFIC_GB,
        "username": entry.get("username"),
        "server_id": entry.get("server_id"),
    }


def mark_test_config_connected(user_id, connected_at=None):
    """Idempotently record that the customer confirmed a successful connection."""
    key = str(user_id)
    timestamp = format_utc_timestamp(connected_at)

    def mutate(configs):
        entry = configs.get(key)
        if not isinstance(entry, dict) or not _has_used_test_config_from(configs, user_id):
            return False
        entry.setdefault("connected_at", timestamp)
        return True

    return bool(test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate))

def add_to_waiting_list(user_id, username=None, language=None):
    if has_used_test_config(user_id):
        return False

    key = str(user_id)
    helpers = _atomic_helpers()
    if helpers:
        with helpers[0](TEST_WAITING_LIST_FILE, {}) as waiting_users:
            if not isinstance(waiting_users, dict):
                raise ValueError("Test waiting list must contain a JSON object.")
            if key in waiting_users:
                return False
            waiting_users[key] = {
                "telegram_id": user_id,
                "telegram_username": username,
                "language": language,
                "added_at": format_utc_timestamp(),
            }
            return True
    waiting_users = load_waiting_users()
    if key in waiting_users:
        return False
    waiting_users[key] = {
        "telegram_id": user_id,
        "telegram_username": username,
        "language": language,
        "added_at": format_utc_timestamp(),
    }
    save_waiting_users(waiting_users)
    return True

def _mark_test_config_used_in_memory(
    configs,
    user_id,
    username=None,
    language=None,
    telegram_username=None,
    server_id=None,
    used_at=None,
):
    key = str(user_id)
    # Preserve existing history fields (reset_at, reset_count, original used_at, etc.)
    existing = configs.get(key, {})
    entry = dict(existing)
    now_value = format_utc_timestamp(used_at)
    archived = None
    replacement_username = str(entry.get('replacement_from_username') or '').strip()
    replacement_server = str(entry.get('replacement_from_server_id') or '').strip()
    replacement_used_at = str(entry.get('replacement_from_used_at') or '').strip()
    if (
        entry.get('replacement_eligible_at')
        and replacement_username
        and replacement_server
        and replacement_used_at
        and username
        and str(username).lower() != replacement_username.lower()
    ):
        history = [item for item in entry.get('historical_configs', []) if isinstance(item, dict)]
        target = (replacement_server.lower(), replacement_username.lower())
        history_index = next((
            index
            for index, item in enumerate(history)
            if (
                str(item.get('server_id') or 'primary').lower(),
                str(item.get('username') or '').lower(),
            ) == target
        ), None)
        archived = {
            'username': replacement_username,
            'server_id': replacement_server,
            'used_at': replacement_used_at,
            'superseded_at': now_value,
            'cleanup_reason': 'superseded_on_hold_test',
        }
        if history_index is None:
            history.append(dict(archived))
            history_index = len(history) - 1
        else:
            history[history_index].update(archived)
            archived = dict(history[history_index])
        archived['history_index'] = history_index
        entry['historical_configs'] = history

    entry['used_at'] = now_value
    entry['telegram_id'] = user_id
    entry.pop('creation_pending_at', None)
    if username:
        entry['username'] = username
    if language:
        entry['language'] = language
    if telegram_username:
        entry['telegram_username'] = telegram_username
    if server_id:
        entry['server_id'] = server_id

    for field in (
        'replacement_eligible_at',
        'replacement_from_username',
        'replacement_from_server_id',
        'replacement_from_used_at',
        'replacement_validation_status',
        'replacement_validation_at',
    ):
        entry.pop(field, None)

    configs[key] = entry
    return archived

def mark_test_config_used(user_id, username=None, language=None, telegram_username=None, server_id=None):
    def mutate(configs):
        return _mark_test_config_used_in_memory(
            configs,
            user_id,
            username=username,
            language=language,
            telegram_username=telegram_username,
            server_id=server_id,
        )

    return test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate)


def _queue_superseded_cleanup(user_id, archived, language=None):
    if not isinstance(archived, dict):
        return
    try:
        from utils.expired_cleanup import queue_superseded_test_cleanup

        queue_superseded_test_cleanup(
            telegram_user_id=user_id,
            username=archived.get('username'),
            server_id=archived.get('server_id'),
            history_index=archived.get('history_index'),
            language=language,
        )
    except Exception:
        logging.getLogger('ajib.expired_cleanup').exception(
            'Failed to queue superseded test cleanup. user_id=%s username=%s',
            user_id,
            archived.get('username'),
        )


def _claim_test_config_creation(user_id, now=None):
    now = parse_utc_timestamp(now) if now is not None else utc_now()
    now_value = format_utc_timestamp(now)
    key = str(user_id)

    def mutate(configs):
        entry = dict(configs.get(key) or {})
        if _creation_claim_is_active(entry, now=now):
            return False
        entry.pop('creation_pending_at', None)
        if _has_used_test_config_from({key: entry}, key, now=now):
            return False
        entry.setdefault('telegram_id', user_id)
        entry['creation_pending_at'] = now_value
        configs[key] = entry
        return True

    return bool(test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate))


def _release_test_config_creation(user_id):
    key = str(user_id)

    def mutate(configs):
        entry = configs.get(key)
        if not isinstance(entry, dict):
            return
        entry.pop('creation_pending_at', None)
        if not any(entry.get(field) for field in ('used_at', 'username', 'historical_configs', 'reset_at')):
            configs.pop(key, None)

    test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate)


def _revoke_replacement_eligibility(user_id, reason):
    key = str(user_id)

    def mutate(configs):
        entry = configs.get(key)
        if not isinstance(entry, dict):
            return
        entry.pop('creation_pending_at', None)
        entry.pop('reset_at', None)
        for field in (
            'replacement_eligible_at',
            'replacement_from_username',
            'replacement_from_server_id',
            'replacement_from_used_at',
        ):
            entry.pop(field, None)
        entry['replacement_validation_status'] = str(reason or 'invalid')
        entry['replacement_validation_at'] = format_utc_timestamp()

    test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate)


def _revalidate_pending_replacement(user_id, multi_api):
    entry = load_test_configs().get(str(user_id))
    if not isinstance(entry, dict) or not entry.get('replacement_eligible_at'):
        return True, False
    username = str(entry.get('replacement_from_username') or '').strip()
    server_id = str(entry.get('replacement_from_server_id') or '').strip()
    used_at = str(entry.get('replacement_from_used_at') or '').strip()
    if (username, server_id, used_at) != _replacement_identity(entry):
        _revoke_replacement_eligibility(user_id, 'identity_changed')
        return False, False
    user_data, lookup_status = _exact_test_lookup(multi_api, username, server_id)
    if lookup_status == 'unavailable':
        return False, True
    if lookup_status != 'found' or not _verified_unused_hold(entry, user_data):
        _revoke_replacement_eligibility(user_id, 'no_longer_unused_hold')
        return False, False
    return True, False


def reset_test_users(mode='expired', now=None, multi_api=None):
    """
    Mark test users as eligible to receive a new test config.

    mode='expired'  — only reset users whose test config has expired (>30 days old)
    mode='all'      — reset every user in the database

    Returns the number of users that were reset.
    """
    now = parse_utc_timestamp(now) if now is not None else utc_now()
    reset_ts = format_utc_timestamp(now)

    try:
        multi_api = multi_api or MultiServerAPI()
    except Exception:
        return 0

    snapshot = load_test_configs()
    verified = {}
    for key, entry in snapshot.items() if isinstance(snapshot, dict) else []:
        if not isinstance(entry, dict) or _creation_claim_is_active(entry, now=now):
            continue
        if not _has_used_test_config_from(snapshot, key, now=now):
            continue
        identity = _replacement_identity(entry)
        if identity is None:
            continue
        username, server_id, used_at_value = identity
        used_at = _parse_config_time(used_at_value)
        if mode == 'expired' and (
            used_at is None
            or now < used_at + datetime.timedelta(days=TEST_REPLACEMENT_ELIGIBILITY_DAYS)
            or now >= used_at + datetime.timedelta(days=TEST_STALE_CLEANUP_DAYS)
        ):
            continue
        user_data, lookup_status = _exact_test_lookup(multi_api, username, server_id)
        if lookup_status != 'found':
            continue
        if mode == 'expired' and not _verified_unused_hold(entry, user_data):
            continue
        verified[str(key)] = identity

    def mutate(configs):
        count = 0
        for key, entry in configs.items():
            if not isinstance(entry, dict) or _creation_claim_is_active(entry, now=now):
                continue
            if not _has_used_test_config_from(configs, key, now=now):
                continue
            identity = verified.get(str(key))
            if identity is None or _replacement_identity(entry) != identity:
                continue
            entry['reset_at'] = reset_ts
            entry['reset_count'] = entry.get('reset_count', 0) + 1
            entry['replacement_eligible_at'] = reset_ts
            entry['replacement_from_username'] = identity[0]
            entry['replacement_from_server_id'] = identity[1]
            entry['replacement_from_used_at'] = identity[2]
            count += 1
        return count

    return test_config_store.update_test_configs(TEST_CONFIGS_FILE, mutate)

@bot.message_handler(func=lambda message: any(
    message.text == translations["test_config"] 
    for translations in BUTTON_TRANSLATIONS.values()
))
def test_config(message):
    user_id = message.from_user.id
    language = get_user_language(user_id)

    # Check if test creation is disabled
    if is_test_creation_disabled():
        if has_used_test_config(user_id):
            bot.reply_to(
                message,
                get_message_text(language, "test_config_used"),
                reply_markup=create_main_markup(is_admin=False, user_id=user_id)
            )
            return

        add_to_waiting_list(user_id, message.from_user.username, language)
        bot.reply_to(
            message,
            get_message_text(language, "test_config_waiting_list"),
            reply_markup=create_main_markup(is_admin=False, user_id=user_id)
        )
        return

    # Check if user has already used a test config
    if has_used_test_config(user_id):
        bot.reply_to(
            message,
            get_message_text(language, "test_config_used"),
            reply_markup=create_main_markup(is_admin=False, user_id=user_id)
        )
        return
    
    # The customer makes one explicit commitment before any account is provisioned.
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            get_message_text(language, "start_free_test_button"),
            callback_data="start_free_test",
        ),
        types.InlineKeyboardButton(
            get_message_text(language, "cancel_test_button"),
            callback_data="cancel_test_config",
        )
    )
    
    bot.reply_to(
        message,
        get_message_text(language, "test_config_offer").format(
            traffic_gb=TEST_TRAFFIC_GB,
            days=TEST_DAYS,
        ),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "cancel_test_config")
def handle_cancel_test_config(call):
    language = get_user_language(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    safe_edit_message_text(
        bot,
        get_message_text(language, "test_config_cancelled"),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )


def _queue_test_config_creation(user_id, chat_id, message_id, language, telegram_username=None):
    with TEST_CONFIG_JOB_LOCK:
        if user_id in TEST_CONFIG_INFLIGHT:
            return False
        TEST_CONFIG_INFLIGHT.add(user_id)

    def run():
        try:
            safe_edit_message_text(
                bot,
                get_message_text(language, "test_config_creating"),
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            pass
        try:
            create_test_config(
                user_id,
                chat_id,
                is_automatic=False,
                language=language,
                telegram_username=telegram_username,
            )
        finally:
            with TEST_CONFIG_JOB_LOCK:
                TEST_CONFIG_INFLIGHT.discard(user_id)

    try:
        TEST_CONFIG_EXECUTOR.submit(run)
    except Exception:
        with TEST_CONFIG_JOB_LOCK:
            TEST_CONFIG_INFLIGHT.discard(user_id)
        raise
    return True


@bot.callback_query_handler(func=lambda call: call.data in {"confirm_test_config", "start_free_test"})
def handle_confirm_test_config(call):
    user_id = call.from_user.id
    language = get_user_language(user_id)
    # Check if test creation is disabled
    if is_test_creation_disabled():
        if has_used_test_config(user_id):
            safe_answer_callback_query(bot, call.id)
            safe_edit_message_text(
                bot,
                get_message_text(language, "test_config_used"),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return

        add_to_waiting_list(user_id, call.from_user.username, language)
        safe_answer_callback_query(bot, call.id)
        safe_edit_message_text(
            bot,
            get_message_text(language, "test_config_waiting_list"),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return

    # Double check if user has already used a test config
    if has_used_test_config(user_id):
        safe_answer_callback_query(bot, call.id)
        safe_edit_message_text(
            bot,
            get_message_text(language, "test_config_used"),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return

    safe_answer_callback_query(bot, call.id)
    queued = _queue_test_config_creation(
        user_id,
        call.message.chat.id,
        call.message.message_id,
        language,
        telegram_username=call.from_user.username,
    )
    if queued:
        record_main_growth_event(
            "trial_started",
            user_id,
            language=language,
            deduplication_key=f"main:trial_started:{user_id}",
        )
    else:
        safe_answer_callback_query(
            bot,
            call.id,
            text=get_message_text(language, "test_config_in_progress"),
        )


def _trial_activation_markup(language):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(
            get_message_text(language, "trial_connected_button"),
            callback_data="trial_connected",
        ),
        types.InlineKeyboardButton(
            get_message_text(language, "trial_need_help_button"),
            callback_data="trial_need_help",
        ),
        types.InlineKeyboardButton(
            get_message_text(language, "trial_see_plans_button"),
            callback_data="trial_see_plans",
        ),
    )
    return markup


def _send_created_test_config(chat_id, username, user_uri_data, is_automatic=False, language=None):
    language = language or get_user_language(chat_id)
    if user_uri_data and 'normal_sub' in user_uri_data:
        sub_url = user_uri_data['normal_sub']
        ipv4_url = user_uri_data.get('ipv4', '')

        # Create QR code for IPv4 URL when available.
        qr = qrcode.make(ipv4_url or sub_url)
        bio = io.BytesIO()
        qr.save(bio, 'PNG')
        bio.seek(0)

        success_message = get_message_text(language, "test_config_created").format(
            traffic_gb=TEST_TRAFFIC_GB,
            days=TEST_DAYS,
            username=username,
            ipv4_line=(
                get_message_text(language, "test_config_ipv4_line").format(ipv4_url=ipv4_url)
                if ipv4_url else ""
            ),
            sub_url=sub_url,
        )
        safe_send_photo(
            bot,
            chat_id,
            photo=bio,
            caption=success_message,
            parse_mode="Markdown"
        )
        safe_send_message(
            bot,
            chat_id,
            get_message_text(language, "trial_activation_steps"),
            reply_markup=_trial_activation_markup(language),
            parse_mode="Markdown",
        )
        send_download_prompt_safely(bot, chat_id, language)
    else:
        safe_send_message(
            bot,
            chat_id,
            get_message_text(language, "test_config_created_no_url"),
            parse_mode="Markdown"
        )


@bot.callback_query_handler(func=lambda call: call.data == "trial_connected")
def handle_trial_connected(call):
    language = get_user_language(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    if not mark_test_config_connected(call.from_user.id):
        safe_edit_message_text(
            bot,
            get_message_text(language, "test_config_used"),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        return
    record_main_growth_event(
        "trial_activated",
        call.from_user.id,
        language=language,
        deduplication_key=f"main:trial_activated:{call.from_user.id}",
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        get_message_text(language, "trial_see_plans_button"),
        callback_data="trial_see_plans",
    ))
    safe_edit_message_text(
        bot,
        get_message_text(language, "trial_connected_confirmation"),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "trial_need_help")
def handle_trial_need_help(call):
    language = get_user_language(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    safe_send_message(
        bot,
        call.message.chat.id,
        get_message_text(language, "trial_help_intro"),
    )
    send_download_prompt_safely(bot, call.message.chat.id, language)


@bot.callback_query_handler(func=lambda call: call.data == "trial_see_plans")
def handle_trial_see_plans(call):
    safe_answer_callback_query(bot, call.id)
    from utils.purchase_plan import show_plans

    show_plans(call.message.chat.id, call.from_user.id, call.message.message_id)

def _create_test_config_with_client(
    user_id,
    chat_id,
    api_client,
    existing_usernames,
    test_configs,
    is_automatic=False,
    language=None,
    telegram_username=None,
):
    if _has_used_test_config_from(test_configs, user_id):
        return False
    if not _claim_test_config_creation(user_id):
        return False

    class _SingleClientLookup:
        @staticmethod
        def get_client(server_id):
            return api_client if str(getattr(api_client, 'server_id', '')) == str(server_id) else None

    replacement_valid, _retryable = _revalidate_pending_replacement(user_id, _SingleClientLookup())
    if not replacement_valid:
        _release_test_config_creation(user_id)
        return False

    try:
        username = allocate_username("t", user_id, existing_usernames)
        note_payload = build_user_note(
            username=username,
            traffic_limit=TEST_TRAFFIC_GB,
            expiration_days=TEST_DAYS,
            unlimited=True,
            note_text="test_config",
        )
        result = api_client.add_user(
            username,
            TEST_TRAFFIC_GB,
            TEST_DAYS,
            unlimited=True,
            note=note_payload,
        )
        if result is None:
            result = api_client.add_user(username, TEST_TRAFFIC_GB, TEST_DAYS, unlimited=True)
            if result is not None:
                logging.getLogger("ajib.usernames").warning(
                    "Created test user without note fallback. user_id=%s username=%s",
                    user_id,
                    username,
                )
    except Exception:
        _release_test_config_creation(user_id)
        raise

    if not result:
        _release_test_config_creation(user_id)
        return False

    archived = mark_test_config_used(
        user_id,
        username=username,
        language=language,
        telegram_username=telegram_username,
        server_id=api_client.server_id,
    )
    _queue_superseded_cleanup(user_id, archived, language=language)
    _mark_test_config_used_in_memory(
        test_configs,
        user_id,
        username=username,
        language=language,
        telegram_username=telegram_username,
        server_id=api_client.server_id,
    )
    existing_usernames.add(username)

    user_uri_data = api_client.get_user_uri(username)
    _send_created_test_config(
        chat_id,
        username,
        user_uri_data,
        is_automatic=is_automatic,
        language=language,
    )
    return True

def create_test_config(user_id, chat_id, is_automatic=False, language=None, telegram_username=None, ignore_creation_disabled=False):
    def notify_creation_failed():
        if is_automatic:
            return
        message_lookup = globals().get("get_message_text")
        language_lookup = globals().get("get_user_language")
        bot_instance = globals().get("bot")
        if not callable(message_lookup) or bot_instance is None:
            return
        resolved_language = language
        if not resolved_language and callable(language_lookup):
            resolved_language = language_lookup(user_id)
        bot_instance.send_message(
            chat_id,
            message_lookup(resolved_language or "en", "test_config_creation_failed"),
            parse_mode="Markdown",
        )

    # Check if test creation is disabled
    if is_test_creation_disabled() and not ignore_creation_disabled:
        return False

    if not _claim_test_config_creation(user_id):
        return False

    try:
        recorded_usernames = load_recorded_usernames()
        multi_api = MultiServerAPI()
    except RecordedUsernameLoadError as exc:
        logging.getLogger("ajib.usernames").error(
            "Test user creation blocked because username history could not be loaded. user_id=%s error=%s",
            user_id,
            exc,
        )
        _release_test_config_creation(user_id)
        notify_creation_failed()
        return False
    except Exception:
        _release_test_config_creation(user_id)
        raise

    replacement_revalidator = globals().get('_revalidate_pending_replacement')
    if callable(replacement_revalidator):
        replacement_valid, _retryable = replacement_revalidator(user_id, multi_api)
        if not replacement_valid:
            _release_test_config_creation(user_id)
            notify_creation_failed()
            return False

    def allocate(existing_usernames):
        return allocate_username(
            "t",
            user_id,
            set(existing_usernames) | recorded_usernames,
        )

    def create(api_client, username):
        note_payload = build_user_note(
            username=username,
            traffic_limit=TEST_TRAFFIC_GB,
            expiration_days=TEST_DAYS,
            unlimited=True,
            note_text="test_config",
        )
        result = api_client.add_user(
            username,
            TEST_TRAFFIC_GB,
            TEST_DAYS,
            unlimited=True,
            note=note_payload,
        )
        if result is None:
            result = api_client.add_user(username, TEST_TRAFFIC_GB, TEST_DAYS, unlimited=True)
            if result is not None:
                logging.getLogger("ajib.usernames").warning(
                    "Created test user without note fallback. user_id=%s username=%s",
                    user_id,
                    username,
                )
        return result

    try:
        username, result, api_client = multi_api.create_user_with_retry(allocate, create)
    except Exception:
        _release_test_config_creation(user_id)
        raise
    if result:
        archived = mark_test_config_used(
            user_id,
            username=username,
            language=language,
            telegram_username=telegram_username,
            server_id=api_client.server_id,
        )
        queue_superseded = globals().get('_queue_superseded_cleanup')
        if callable(queue_superseded):
            queue_superseded(user_id, archived, language=language)
        user_uri_data = api_client.get_user_uri(username)
        _send_created_test_config(
            chat_id,
            username,
            user_uri_data,
            is_automatic=is_automatic,
            language=language,
        )
        return True

    _release_test_config_creation(user_id)

    notify_creation_failed()
    return False

def _safe_server_weight(value):
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 1.0
    return weight if weight > 0 else 1.0

def _build_bulk_test_config_state():
    multi_api = MultiServerAPI()
    existing_usernames = load_recorded_usernames()
    server_states = []

    for index, (server, client) in enumerate(multi_api.iter_clients(include_disabled=True)):
        users = client.get_users()
        if users is None:
            continue

        existing_usernames.update(multi_api.extract_usernames(users))
        if not server.get("enabled", True):
            continue

        weight = _safe_server_weight(server.get("weight", 1))
        server_states.append({
            "index": index,
            "client": client,
            "allocated_count": (
                multi_api.allocated_user_count(users)
                if callable(getattr(multi_api, 'allocated_user_count', None))
                else multi_api.active_user_count(users)
            ),
            "weight": weight,
        })

    return existing_usernames, server_states

def _select_bulk_server_state(server_states):
    if not server_states:
        return None
    return min(
        server_states,
        key=lambda state: (state["allocated_count"] / state["weight"], state["index"])
    )


# ─── Admin: Reset Test Accounts ───────────────────────────────────────────────

def build_test_accounts_menu():
    settings = load_test_settings()
    disabled = settings.get("creation_disabled", False)
    status_text = "🔴 *Disabled*" if disabled else "🟢 *Enabled*"
    toggle_text = "✅ Enable Test Creation" if disabled else "🚫 Disable Test Creation"
    toggle_action = "enable" if disabled else "disable"
    waiting_count = len(load_waiting_users())

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⏰ Reset Expired Only", callback_data="reset_test:expired"),
        types.InlineKeyboardButton("♻️ Reset All", callback_data="reset_test:all"),
    )
    markup.add(types.InlineKeyboardButton(toggle_text, callback_data=f"toggle_test_creation:{toggle_action}"))
    markup.add(types.InlineKeyboardButton("👥 Manage Waiting Users", callback_data="manage_waiting"))
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="reset_test:cancel"))

    text = (
        f"🔄 *Manage Test Accounts*\n\n"
        f"Current test creation status: {status_text}\n"
        f"⏳ Waiting Users: *{waiting_count}*\n\n"
        f"Choose an option:\n"
        f"• *Expired Only* — users whose 30-day test config has already expired\n"
        f"• *Reset All* — every user in the database (including active ones)\n\n"
        f"The `test_configs.json` database is *kept intact* for broadcasting."
    )
    return text, markup

def build_waiting_management_menu():
    waiting_count = len(load_waiting_users())
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🎁 Create & Send Configs", callback_data="waiting_prompt:create"))
    markup.add(types.InlineKeyboardButton("📢 Notify Eligibility", callback_data="waiting_prompt:notify"))
    markup.add(types.InlineKeyboardButton("🗑️ Clear List", callback_data="waiting_action:clear"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="waiting_action:back"))
    text = (
        f"👥 *Manage Waiting Users*\n\n"
        f"⏳ Waiting Users: *{waiting_count}*\n\n"
        "Choose an action:"
    )
    return text, markup

def build_waiting_chunk_menu(action):
    waiting_count = len(load_waiting_users())
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("20 Users", callback_data=f"waiting_chunk:{action}:20"),
        types.InlineKeyboardButton("50 Users", callback_data=f"waiting_chunk:{action}:50"),
    )
    markup.add(
        types.InlineKeyboardButton("100 Users", callback_data=f"waiting_chunk:{action}:100"),
        types.InlineKeyboardButton("All Users", callback_data=f"waiting_chunk:{action}:all"),
    )
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="waiting_chunk:cancel"))
    text = (
        f"There are {waiting_count} users currently in the waiting list. "
        "Select how many users to process in this chunk:"
    )
    return text, markup

@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == admin_action_text("manage_test_accounts"))
def reset_test_accounts_menu(message):
    """Admin command: show reset and settings management."""
    text, markup = build_test_accounts_menu()
    bot.reply_to(
        message,
        text,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_test_creation:"))
def handle_toggle_test_creation(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    action = call.data.split(":", 1)[1]
    settings = load_test_settings()

    if action == "enable":
        settings["creation_disabled"] = False
        msg = "🟢 Test account creation enabled."
    else:
        settings["creation_disabled"] = True
        msg = "🔴 Test account creation disabled."

    save_test_settings(settings)
    bot.answer_callback_query(call.id, msg)

    text, markup = build_test_accounts_menu()

    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "manage_waiting")
def handle_manage_waiting(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    text, markup = build_waiting_management_menu()
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("waiting_action:"))
def handle_waiting_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    action = call.data.split(":", 1)[1]

    if action == "back":
        text, markup = build_test_accounts_menu()
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    if action == "clear":
        waiting_count = len(load_waiting_users())
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Confirm", callback_data="waiting_action:clear_confirm"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="manage_waiting"),
        )
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⚠️ Clear all *{waiting_count}* waiting users?\n\nThis cannot be undone.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    if action == "clear_confirm":
        save_waiting_users({})
        text, markup = build_waiting_management_menu()
        bot.answer_callback_query(call.id, "Waiting list cleared.")
        bot.edit_message_text(
            f"✅ Waiting list cleared.\n\n{text}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("waiting_prompt:"))
def handle_waiting_prompt(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    action = call.data.split(":", 1)[1]
    if action not in ("create", "notify"):
        bot.answer_callback_query(call.id, "Invalid action.")
        return

    text, markup = build_waiting_chunk_menu(action)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("waiting_chunk:"))
def handle_waiting_chunk(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    if call.data == "waiting_chunk:cancel":
        text, markup = build_waiting_management_menu()
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    try:
        _, action, chunk_size = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chunk.")
        return

    if action not in ("create", "notify"):
        bot.answer_callback_query(call.id, "Invalid action.")
        return

    waiting_users = load_waiting_users()
    if not waiting_users:
        text, markup = build_waiting_management_menu()
        bot.answer_callback_query(call.id, "Waiting list is empty.")
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    if chunk_size == "all":
        limit = len(waiting_users)
    else:
        try:
            limit = int(chunk_size)
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid chunk size.")
            return

    selected_users = list(waiting_users.items())[:limit]
    processed_count = 0
    failure_count = 0

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"⏳ Processing {len(selected_users)} waiting users...",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )

    test_configs = None
    existing_usernames = None
    server_states = None
    state_changed = False
    if action == "create":
        test_configs = load_test_configs()
        try:
            existing_usernames, server_states = _build_bulk_test_config_state()
        except RecordedUsernameLoadError as exc:
            logging.getLogger("ajib.usernames").error(
                "Bulk test creation blocked because username history could not be loaded. error=%s",
                exc,
            )
            text, markup = build_waiting_management_menu()
            bot.edit_message_text(
                f"❌ Local username history could not be read. No accounts were created.\n\n{text}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            return
        if not server_states:
            text, markup = build_waiting_management_menu()
            bot.edit_message_text(
                f"❌ No healthy enabled VPN servers were available.\n\n{text}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            return

    for user_key, user_data in selected_users:
        user_id = user_data.get("telegram_id") or int(user_key)
        language = user_data.get("language") or get_user_language(user_id)
        telegram_username = user_data.get("telegram_username")
        success = False

        try:
            if action == "create":
                server_state = _select_bulk_server_state(server_states)
                success = _create_test_config_with_client(
                    user_id,
                    user_id,
                    server_state["client"],
                    existing_usernames,
                    test_configs,
                    is_automatic=True,
                    language=language,
                    telegram_username=telegram_username,
                )
                if success:
                    server_state["allocated_count"] += 1
            elif action == "notify":
                bot.send_message(user_id, get_message_text(language, "test_config_waitlist_eligible"))
                success = True
        except Exception as e:
            print(f"Waiting list {action} failed for {user_id}: {e}")
            success = False

        if success:
            waiting_users.pop(user_key, None)
            state_changed = True
            processed_count += 1
            if processed_count % 25 == 0:
                save_waiting_users(waiting_users)
        else:
            failure_count += 1

        time.sleep(0.1)

    if state_changed:
        save_waiting_users(waiting_users)

    remaining_count = len(waiting_users)
    text, markup = build_waiting_management_menu()
    bot.edit_message_text(
        f"✅ *Waiting list chunk complete!*\n\n"
        f"• Action: `{action}`\n"
        f"• Processed: *{processed_count}*\n"
        f"• Failed: *{failure_count}*\n"
        f"• Remaining in waiting list: *{remaining_count}*\n\n"
        f"{text}",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("reset_test:"))
def handle_reset_test_selection(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    mode = call.data.split(":", 1)[1]

    if mode == "cancel":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "❌ Reset cancelled.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        return

    # Ask for confirmation before proceeding
    label = "expired users only" if mode == "expired" else "ALL users"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Confirm", callback_data=f"reset_test_confirm:{mode}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="reset_test:cancel"),
    )
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"⚠️ You are about to reset test eligibility for *{label}*.\n\n"
        "The original database entries will be preserved. Reset users will be able to request a new test config.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("reset_test_confirm:"))
def handle_reset_test_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized")
        return

    mode = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)

    count = reset_test_users(mode=mode)

    label = "expired" if mode == "expired" else "all"
    bot.edit_message_text(
        f"✅ *Reset complete!*\n\n"
        f"• Mode: `{label}`\n"
        f"• Users reset: *{count}*\n\n"
        f"These users can now request a new test config. "
        f"Their entries in the database are preserved for broadcasting.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode="Markdown"
    )
