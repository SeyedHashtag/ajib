import json
import os
import re
import threading
from datetime import datetime

try:
    from telebot import types
except ImportError:
    class _InlineKeyboardButton:
        def __init__(self, text, callback_data=None, **kwargs):
            self.text = text
            self.callback_data = callback_data
            self.kwargs = kwargs

    class _InlineKeyboardMarkup:
        def __init__(self, row_width=1, **kwargs):
            self.row_width = row_width
            self.keyboard = []

        def add(self, *buttons):
            for button in buttons:
                self.keyboard.append([button])
            return self

    class _Types:
        InlineKeyboardButton = _InlineKeyboardButton
        InlineKeyboardMarkup = _InlineKeyboardMarkup

    types = _Types()

from utils.api_client import MultiServerAPI
from utils.account_state import PanelState, inspect_account, remaining_full_days, resolve_service_cycle
from utils.command import bot
from utils.language import get_user_language
from utils.translations import get_button_text, get_message_text

ALERTS_FILE = '/etc/ajib/core/scripts/telegrambot/traffic_alerts.json'
RESELLERS_FILE = '/etc/ajib/core/scripts/telegrambot/resellers.json'
ALERT_THRESHOLDS = [80, 90]
ALERT_RESET_RATIO = 0.05
PAID_PAYMENT_STATUSES = {'completed', 'paid', 'succeeded'}

_alerts_lock = threading.Lock()


def _reminders_enabled():
    try:
        from utils.growth_features import REMINDERS, is_growth_feature_enabled

        return is_growth_feature_enabled(REMINDERS)
    except ImportError:
        raw = os.getenv('AJIB_GROWTH_REMINDERS_ENABLED', 'true')
        return str(raw).strip().lower() not in {'0', 'false', 'no', 'off', 'disabled'}


def _record_renewal_prompt(
    recipient_id,
    username,
    language,
    threshold,
    basis,
    state,
    plan_id=None,
    server_id=None,
    source='customer',
):
    """Best-effort funnel measurement; alert delivery never depends on it."""
    try:
        from utils.growth_events import EVENT_RENEWAL_PROMPTED, record_growth_event

        cycle = int((state or {}).get('renewal_cycle', 1) or 1)
        record_growth_event(
            EVENT_RENEWAL_PROMPTED,
            user_id=recipient_id,
            language=language,
            plan_id=plan_id,
            deduplication_key=(
                f"renewal-alert:{server_id or 'primary'}:{username}:{cycle}:{int(threshold)}"
            ),
            metadata={
                'basis': basis,
                'threshold': int(threshold),
                'username': str(username),
                'source': source,
            },
        )
    except Exception:
        return


def _atomic_helpers():
    try:
        from utils.atomic_store import locked_json, read_json
        return locked_json, read_json
    except ImportError:
        return None


def _load_alerts():
    with _alerts_lock:
        try:
            helpers = _atomic_helpers()
            if helpers:
                data = helpers[1](ALERTS_FILE, {})
            elif os.path.exists(ALERTS_FILE):
                with open(ALERTS_FILE, 'r') as f:
                    data = json.load(f)
            else:
                data = {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save_alerts(alerts):
    with _alerts_lock:
        helpers = _atomic_helpers()
        if helpers:
            with helpers[0](ALERTS_FILE, {}) as stored:
                if not isinstance(stored, dict):
                    raise ValueError("Traffic alerts must contain a JSON object.")
                stored.clear()
                stored.update(alerts if isinstance(alerts, dict) else {})
        else:
            os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
            with open(ALERTS_FILE, 'w') as f:
                json.dump(alerts, f, indent=2)


def _extract_telegram_id(username):
    if not username:
        return None

    match = re.match(r'^s(\d+)[a-z]*$', username, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    match = re.match(r'^t(\d+)[a-z]*$', username, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    match = re.match(r'^(\d+)t', username)
    if match:
        return int(match.group(1))

    match = re.match(r'^sell(\d+)t', username)
    if match:
        return int(match.group(1))

    match = re.match(r'^test(\d+)t', username)
    if match:
        return int(match.group(1))

    return None


def _should_reset_alerts(state, max_download_bytes, total_usage_bytes, cycle_marker=None):
    previous_limit = state.get('max_download_bytes')
    if previous_limit is not None and previous_limit != max_download_bytes:
        return True

    previous_marker = state.get('cycle_marker')
    if previous_marker and cycle_marker and previous_marker != cycle_marker:
        return True

    previous_usage = state.get('last_usage_bytes')
    if (
        previous_usage is not None
        and max_download_bytes > 0
        and previous_usage > max_download_bytes * ALERT_RESET_RATIO
        and total_usage_bytes <= max_download_bytes * ALERT_RESET_RATIO
    ):
        return True

    return False


def _select_threshold_alert(usage_percent, notified):
    crossed = sorted(threshold for threshold in ALERT_THRESHOLDS if usage_percent >= threshold)
    newly_crossed = [threshold for threshold in crossed if threshold not in notified]
    if not newly_crossed:
        return None, []

    alert_threshold = newly_crossed[-1]
    handled_thresholds = [threshold for threshold in newly_crossed if threshold <= alert_threshold]
    return alert_threshold, handled_thresholds


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cycle_marker(user_data):
    value = (user_data or {}).get('account_creation_date')
    return str(value).strip() if value else None


def _load_customer_context():
    try:
        from utils.edit_plans import load_plans
        from utils.payment_records import load_payments

        plans = load_plans()
        payments = load_payments()
        return (
            plans if isinstance(plans, dict) else {},
            payments if isinstance(payments, dict) else {},
        )
    except (ImportError, OSError, TypeError, ValueError):
        return {}, {}


def _matching_customer_payment(payments, user_id, username, server_id=None):
    matches = []
    for payment_id, record in (payments or {}).items():
        if not isinstance(record, dict):
            continue
        if str(record.get('status', '')).lower() not in PAID_PAYMENT_STATUSES:
            continue
        if record.get('type') == 'settlement' or record.get('plan_gb') == 'Settlement':
            continue
        if str(record.get('user_id')) != str(user_id):
            continue
        record_username = str(record.get('renewal_username') or record.get('username') or '')
        if record_username.casefold() != str(username).casefold():
            continue
        record_server_id = record.get('renewal_server_id') or record.get('server_id')
        if server_id and record_server_id and str(record_server_id) != str(server_id):
            continue
        total_days = _safe_int(record.get('days'))
        if total_days is None or total_days <= 0:
            continue
        matches.append((
            str(record.get('completed_at') or record.get('updated_at') or record.get('created_at') or ''),
            str(payment_id),
            record,
        ))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _customer_renewal_offer(
    user_id,
    username,
    api_client,
    user_data,
    plans,
    payments,
):
    try:
        from utils.renewal import find_customer_renewal_offer

        return find_customer_renewal_offer(
            user_id,
            username,
            api_client,
            user_data,
            plans,
            payments=payments,
            server_id=getattr(api_client, 'server_id', None),
            allow_reservation=True,
        )
    except (ImportError, OSError, TypeError, ValueError):
        return {'eligible': False}


def _reseller_renewal_offer(
    reseller_id,
    username,
    api_client,
    user_data,
    plans,
    reseller_data,
):
    try:
        from utils.renewal import find_reseller_renewal_offer

        configs = (reseller_data or {}).get('configs', [])
        config_index = next(
            (
                index
                for index, config in enumerate(configs)
                if isinstance(config, dict) and config.get('username') == username
            ),
            None,
        )
        if config_index is None:
            return {'eligible': False}
        return find_reseller_renewal_offer(
            reseller_id,
            config_index,
            api_client,
            user_data,
            plans,
            reseller_data=reseller_data,
            allow_reservation=True,
        )
    except (ImportError, OSError, TypeError, ValueError):
        return {'eligible': False}


def _renewal_markup(language, offer, callback_prefix):
    if not isinstance(offer, dict) or not offer.get('eligible') or not offer.get('token'):
        return None
    button_key = 'reserve_renewal' if offer.get('renewal_mode') == 'reserved' else 'renew_plan'
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        get_button_text(language, button_key),
        callback_data=f"{callback_prefix}{offer['token']}",
    ))
    return markup


def _extract_reseller_id(username):
    """Extract the reseller's Telegram ID from a reseller-created config username.

    Reseller configs use new format r{reseller_id}[suffix] and legacy
    format reseller{reseller_id}t{timestamp}{chosen_username}.
    Returns the reseller's integer Telegram ID, or None if the username is not a
    reseller-created config.
    """
    if not username:
        return None
    match = re.match(r'^r(\d+)[a-z]*$', username, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.match(r'^reseller(\d+)t', username)
    if match:
        return int(match.group(1))
    return None


def _get_reseller_data(reseller_id):
    try:
        try:
            from utils import state_store
            from utils.reseller import get_reseller_data
        except ImportError:
            state_store = None
            get_reseller_data = None
        if state_store and state_store.is_managed_path(RESELLERS_FILE):
            record = get_reseller_data(reseller_id) or {}
            return record if isinstance(record, dict) else {}
        if not os.path.exists(RESELLERS_FILE):
            return {}
        with open(RESELLERS_FILE, 'r') as f:
            resellers = json.load(f)
        record = resellers.get(str(reseller_id))
        return record if isinstance(record, dict) else {}
    except Exception:
        pass
    return {}


def _get_reseller_config(reseller_id, username, reseller_data=None):
    """Look up the reseller record for a client config."""
    record = reseller_data if isinstance(reseller_data, dict) else _get_reseller_data(reseller_id)
    for config in record.get('configs', []):
        if isinstance(config, dict) and config.get('username') == username:
            return config
    return {}


def _valid_reseller_customer_name(value):
    name = str(value or "").strip()
    if re.match(r"^[a-zA-Z0-9]{1,8}$", name):
        return name
    return ""


def _extract_customer_name_from_note(note):
    if not isinstance(note, str):
        return ""
    match = re.search(r"📝\s*([^|]+?)\s*\|", note)
    if not match:
        return ""
    return _valid_reseller_customer_name(match.group(1))


def _resolve_reseller_customer_name(config, user_data):
    stored_name = _valid_reseller_customer_name((config or {}).get('customer_name'))
    if stored_name:
        return stored_name
    note_name = _extract_customer_name_from_note((user_data or {}).get('note'))
    return note_name or "—"


def _get_reseller_total_days(config):
    try:
        return int((config or {})['days'])
    except (KeyError, TypeError, ValueError):
        return None


def _should_reset_days_alerts(state, total_days, expiration_days):
    """Reset day-based alerts when the plan is renewed (total_days reference changes)."""
    previous_total = state.get('total_days')
    if previous_total is not None and previous_total != total_days:
        return True

    previous_remaining = _safe_int(state.get('last_expiration_days'))
    if previous_remaining is not None and expiration_days > previous_remaining + 1:
        return True
    return False


def monitor_user_traffic():
    if not _reminders_enabled():
        return

    multi_api = MultiServerAPI()
    if not multi_api.servers:
        return

    plans, payments = _load_customer_context()
    alerts = _load_alerts()
    changed = False
    updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for api_client, username, user_data in multi_api.iter_all_users(include_disabled=False):
        if not username or not user_data:
            continue
        if inspect_account(user_data, source='traffic_monitor').panel_state == PanelState.UNKNOWN:
            continue

        # ── Regular user GB alerts ──────────────────────────────────────────
        telegram_id = _extract_telegram_id(username)
        if telegram_id is not None:
            previous_state = alerts.get(username, {})
            previous_state = previous_state if isinstance(previous_state, dict) else {}
            state = dict(previous_state)
            max_download_bytes = user_data.get('max_download_bytes', 0) or 0
            upload_bytes = user_data.get('upload_bytes', 0) or 0
            download_bytes = user_data.get('download_bytes', 0) or 0
            total_usage_bytes = upload_bytes + download_bytes
            marker = _cycle_marker(user_data)

            payment = _matching_customer_payment(
                payments,
                telegram_id,
                username,
                server_id=getattr(api_client, 'server_id', None),
            )
            cycle = resolve_service_cycle(
                payments,
                username=username,
                server_id=getattr(api_client, 'server_id', None),
                source='customer',
            )
            total_days = cycle.duration_days if cycle else None
            expiration_days = remaining_full_days(cycle.deadline) if cycle else None
            if cycle is not None:
                marker = cycle.fingerprint

            reset_cycle = False
            if max_download_bytes > 0:
                reset_cycle = _should_reset_alerts(
                    state,
                    max_download_bytes,
                    total_usage_bytes,
                    cycle_marker=marker,
                )
            if (
                total_days is not None
                and total_days > 0
                and expiration_days is not None
                and expiration_days >= 0
                and _should_reset_days_alerts(state, total_days, expiration_days)
            ):
                reset_cycle = True

            if reset_cycle:
                state = {'renewal_cycle': _safe_int(state.get('renewal_cycle'), 1) + 1}
            else:
                state.setdefault('renewal_cycle', 1)

            notified = set(state.get('renewal_notified', state.get('notified', [])))
            candidates = []
            if max_download_bytes > 0:
                candidates.append(((total_usage_bytes / max_download_bytes) * 100, 'traffic'))
            if total_days is not None and total_days > 0 and expiration_days is not None:
                candidates.append((
                    (max(0, total_days - expiration_days) / total_days) * 100,
                    'time',
                ))

            if candidates:
                usage_percent, basis = max(candidates, key=lambda item: item[0])
                alert_threshold, handled_thresholds = _select_threshold_alert(usage_percent, notified)
                if alert_threshold is not None:
                    language = get_user_language(telegram_id)
                    if basis == 'traffic':
                        message = get_message_text(language, "traffic_quota_alert").format(
                            percent=int(usage_percent),
                            username=username,
                            used_gb=total_usage_bytes / (1024 ** 3),
                            limit_gb=max_download_bytes / (1024 ** 3),
                        )
                    else:
                        message = get_message_text(language, "time_quota_alert").format(
                            percent=int(usage_percent),
                            username=username,
                            days_used=max(0, total_days - expiration_days),
                            total_days=total_days,
                            days_remaining=expiration_days,
                        )

                    offer = _customer_renewal_offer(
                        telegram_id,
                        username,
                        api_client,
                        user_data,
                        plans,
                        payments,
                    )
                    markup = _renewal_markup(language, offer, 'renew_plan:')
                    try:
                        bot.send_message(
                            telegram_id,
                            message,
                            parse_mode="Markdown",
                            reply_markup=markup,
                        )
                    except Exception as error:
                        print(f"Failed to notify user {telegram_id} for {username}: {error}")
                    else:
                        notified.update(handled_thresholds)
                        if payment is not None:
                            _record_renewal_prompt(
                                telegram_id,
                                username,
                                language,
                                alert_threshold,
                                basis,
                                state,
                                plan_id=payment.get('plan_gb'),
                                server_id=getattr(api_client, 'server_id', None),
                            )

            if notified:
                state['notified'] = sorted(notified)
                state['renewal_notified'] = sorted(notified)
            else:
                state.pop('notified', None)
                state.pop('renewal_notified', None)
            if max_download_bytes > 0:
                state['max_download_bytes'] = max_download_bytes
                state['last_usage_bytes'] = total_usage_bytes
            if total_days is not None and total_days > 0:
                state['total_days'] = total_days
            if expiration_days is not None:
                state['last_expiration_days'] = expiration_days
            if marker:
                state['cycle_marker'] = marker
            state['updated_at'] = updated_at
            alerts[username] = state
            changed = changed or state != previous_state

        # ── Reseller client alerts (GB + days) ─────────────────────────────
        reseller_id = _extract_reseller_id(username)
        if reseller_id is None:
            continue

        language = get_user_language(reseller_id)
        previous_state = alerts.get(username, {})
        previous_state = previous_state if isinstance(previous_state, dict) else {}
        state = dict(previous_state)
        reseller_data = _get_reseller_data(reseller_id)
        reseller_config = _get_reseller_config(
            reseller_id,
            username,
            reseller_data=reseller_data,
        )
        customer_name = _resolve_reseller_customer_name(reseller_config, user_data)

        # — GB alert for reseller client —
        max_download_bytes = user_data.get('max_download_bytes', 0) or 0
        upload_bytes = user_data.get('upload_bytes', 0) or 0
        download_bytes = user_data.get('download_bytes', 0) or 0
        total_usage_bytes = upload_bytes + download_bytes
        cycle = resolve_service_cycle(
            reseller_config,
            username=username,
            server_id=getattr(api_client, 'server_id', None),
            source='reseller_customer',
        )
        expiration_days = remaining_full_days(cycle.deadline) if cycle else None
        total_days = cycle.duration_days if cycle else None
        marker = _cycle_marker(user_data)
        if cycle is not None:
            marker = cycle.fingerprint

        reset_cycle = False
        if max_download_bytes > 0:
            reset_cycle = _should_reset_alerts(
                state,
                max_download_bytes,
                total_usage_bytes,
                cycle_marker=marker,
            )
        if (
            total_days is not None
            and total_days > 0
            and expiration_days is not None
            and expiration_days >= 0
            and _should_reset_days_alerts(state, total_days, expiration_days)
        ):
            reset_cycle = True

        if reset_cycle:
            state = {'renewal_cycle': _safe_int(state.get('renewal_cycle'), 1) + 1}
        else:
            state.setdefault('renewal_cycle', 1)

        notified = set(state.get('renewal_notified', []))
        notified.update(state.get('gb_notified', []))
        notified.update(state.get('days_notified', []))
        candidates = []
        if max_download_bytes > 0:
            candidates.append(((total_usage_bytes / max_download_bytes) * 100, 'traffic'))
        if total_days and total_days > 0 and expiration_days is not None:
            candidates.append((
                (max(0, total_days - expiration_days) / total_days) * 100,
                'time',
            ))

        if candidates:
            usage_percent, basis = max(candidates, key=lambda item: item[0])
            alert_threshold, handled_thresholds = _select_threshold_alert(usage_percent, notified)
            if alert_threshold is not None:
                if basis == 'traffic':
                    message = get_message_text(language, "reseller_client_traffic_alert").format(
                        percent=int(usage_percent),
                        customer_name=customer_name,
                        username=username,
                        used_gb=total_usage_bytes / (1024 ** 3),
                        limit_gb=max_download_bytes / (1024 ** 3),
                    )
                else:
                    message = get_message_text(language, "reseller_client_days_alert").format(
                        percent=int(usage_percent),
                        customer_name=customer_name,
                        username=username,
                        days_used=max(0, total_days - expiration_days),
                        total_days=total_days,
                        days_remaining=expiration_days,
                    )

                offer = _reseller_renewal_offer(
                    reseller_id,
                    username,
                    api_client,
                    user_data,
                    plans,
                    reseller_data,
                )
                markup = _renewal_markup(language, offer, 'reseller:renew:')
                try:
                    bot.send_message(
                        reseller_id,
                        message,
                        parse_mode="Markdown",
                        reply_markup=markup,
                    )
                except Exception as error:
                    print(
                        f"Failed to notify reseller {reseller_id} for client "
                        f"{username} ({basis}): {error}"
                    )
                else:
                    notified.update(handled_thresholds)
                    legacy_key = 'gb_notified' if basis == 'traffic' else 'days_notified'
                    legacy_notified = set(state.get(legacy_key, []))
                    legacy_notified.update(handled_thresholds)
                    state[legacy_key] = sorted(legacy_notified)
                    _record_renewal_prompt(
                        reseller_id,
                        username,
                        language,
                        alert_threshold,
                        basis,
                        state,
                        plan_id=reseller_config.get('gb'),
                        server_id=getattr(api_client, 'server_id', None),
                        source='reseller_customer',
                    )

        if notified:
            state['renewal_notified'] = sorted(notified)
        else:
            state.pop('renewal_notified', None)
        if max_download_bytes > 0:
            state['max_download_bytes'] = max_download_bytes
            state['last_usage_bytes'] = total_usage_bytes
        if total_days and total_days > 0:
            state['total_days'] = total_days
        if expiration_days is not None:
            state['last_expiration_days'] = expiration_days
        if marker:
            state['cycle_marker'] = marker

        state['updated_at'] = updated_at
        alerts[username] = state
        changed = changed or state != previous_state

    if changed:
        _save_alerts(alerts)
