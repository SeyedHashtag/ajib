"""Transport-independent trial eligibility and history transitions."""
import datetime
from .time_utils import format_utc_timestamp, parse_utc_timestamp, utc_now

TEST_CREATION_CLAIM_TIMEOUT_MINUTES = 15
_parse_config_time = parse_utc_timestamp

def _creation_claim_is_active(entry, now=None):
    if not isinstance(entry, dict):
        return False
    if entry.get('web_creation_pending'):
        return True
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
    entry.pop('web_creation_pending', None)
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
