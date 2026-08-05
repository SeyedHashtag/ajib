from telebot import types
import datetime

GROWTH_FUNNEL_BUTTON_TEXT = '📈 Growth Funnel'

ADMIN_MAIN_MENU_ROWS = (
    ('➕ Add User', '👤 Show User'),
    ('❌ Delete User', '📊 Server Info'),
    ('💾 Backup Bot', '💳 Payment Settings'),
    ('📝 Edit Plans', '📢 Broadcast Message'),
    ('📞 Edit Support', '🔄 Update Keyboards'),
    ('💼 Manage Resellers', '🧪 Manage Test Accounts'),
    ('💰 Referral Payouts', '⚖️ VPN Servers'),
    ('✅ Confirmations', '🧹 Expired Cleanup'),
    ('📄 Bot Logs', '🤖 Hosted Bots'),
    (GROWTH_FUNNEL_BUTTON_TEXT,),
)

ADMIN_MAIN_MENU_BUTTONS = {button for row in ADMIN_MAIN_MENU_ROWS for button in row}


def record_main_growth_event(
    event_type,
    user_id,
    *,
    language=None,
    plan_id=None,
    payment_method=None,
    referral_campaign=None,
    deduplication_key=None,
    **metadata,
):
    """Best-effort main-bot hook for the optional local growth-event store."""
    try:
        from utils import growth_events

        aliases = {
            "onboarding_viewed": growth_events.EVENT_ONBOARDING_VIEWED,
            "trial_started": growth_events.EVENT_TRIAL_STARTED,
            "trial_activated": growth_events.EVENT_TRIAL_ACTIVATED,
            "plan_viewed": growth_events.EVENT_PLAN_VIEWED,
            "plan_selected": growth_events.EVENT_PLAN_SELECTED,
            "referral_attributed": growth_events.EVENT_REFERRAL_ATTRIBUTED,
        }
        event = aliases.get(event_type, event_type)
        stable_key = deduplication_key or f"main:{event}:{user_id}:{plan_id or 'none'}"
        growth_events.record_growth_event(
            event,
            user_id=user_id,
            surface=growth_events.SURFACE_MAIN,
            hosted_tenant_id=None,
            language=language,
            plan_id=plan_id,
            payment_method=payment_method,
            referral_campaign=referral_campaign,
            deduplication_key=stable_key,
            metadata=metadata or None,
        )
        return True
    except Exception:
        return False


def is_admin_main_menu_button(text):
    return isinstance(text, str) and text in ADMIN_MAIN_MENU_BUTTONS


def create_main_markup_with_language(language_translations, is_admin=False, user_id=None):
    """
    Create a main menu markup with the given language translations.
    This function doesn't import language or translations to avoid circular imports.
    """
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_admin:
        # Admin menu
        for row in ADMIN_MAIN_MENU_ROWS:
            markup.row(*row)
    else:
        # Non-admin menu with translations
        markup.row(
            language_translations.get("my_configs", "📱 My Configs"),
            language_translations.get("purchase_plan", "💳 Purchase Plan")
        )
        markup.row(
            language_translations.get("downloads", "⬇️ Downloads"),
            language_translations.get("test_config", "🎁 Test Config")
        )
        markup.row(
            language_translations.get("referral", "🎁 Invite & Earn"),
            language_translations.get("reseller_panel", "💼 Reseller Panel")
        )
        markup.row(
            language_translations.get("support", "📞 Support"),
            language_translations.get("language", "🌐 Language/زبان")
        )
        try:
            from utils.receipt_checker import is_receipt_checker
            if user_id is not None and is_receipt_checker(user_id):
                markup.row('✅ Confirmations')
        except Exception:
            pass
    return markup

def create_main_markup(is_admin=False, user_id=None):
    """
    Create a main menu markup with language detection.
    This function handles imports internally to avoid circular imports.
    """
    if is_admin:
        return create_main_markup_with_language({}, is_admin=True, user_id=user_id)

    # Import here to avoid circular imports
    from utils.translations import BUTTON_TRANSLATIONS, DEFAULT_LANGUAGE

    # Get user language - importing here to avoid circular import
    try:
        from utils.language import get_user_language
        language_code = get_user_language(user_id) if user_id else DEFAULT_LANGUAGE
    except (ImportError, Exception):
        language_code = DEFAULT_LANGUAGE

    # Get language translations
    language_translations = BUTTON_TRANSLATIONS.get(language_code, BUTTON_TRANSLATIONS[DEFAULT_LANGUAGE])

    return create_main_markup_with_language(language_translations, is_admin=False, user_id=user_id)


def _parse_journey_timestamp(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _customer_payment_records(user_id):
    try:
        from utils.payment_records import get_user_payments

        payments = get_user_payments(user_id)
    except Exception:
        return []

    records = []
    for payment_id, record in (payments or {}).items():
        if not isinstance(record, dict):
            continue
        if record.get("type") == "settlement" or record.get("plan_gb") == "Settlement":
            continue
        if str(record.get("status", "")).lower() not in {"completed", "paid", "approved"}:
            continue
        records.append((str(payment_id), record))
    records.sort(
        key=lambda item: str(
            item[1].get("completed_at")
            or item[1].get("updated_at")
            or item[1].get("created_at")
            or ""
        ),
        reverse=True,
    )
    return records


def _payment_looks_expired(record, now=None):
    if any(record.get(key) for key in (
        "cleanup_deleted_at",
        "removed_from_vpn",
        "cleanup_delete_result",
    )):
        return True
    if str(record.get("cleanup_status", "")).lower() in {
        "deleted",
        "already_missing",
        "expired",
    }:
        return True

    started = _parse_journey_timestamp(
        record.get("completed_at") or record.get("updated_at") or record.get("created_at")
    )
    try:
        days = int(record.get("days"))
    except (TypeError, ValueError):
        return False
    return bool(started and days > 0 and (now or datetime.datetime.now()) >= started + datetime.timedelta(days=days))


def _renewal_token_for_record(user_id, payment_id, record):
    username = record.get("renewal_username") or record.get("username")
    if not username:
        return None
    server_id = record.get("renewal_server_id") or record.get("server_id")
    try:
        from utils.renewal import customer_renewal_token

        return customer_renewal_token(user_id, payment_id, username, server_id)
    except Exception:
        return None


def get_customer_journey_state(user_id, now=None):
    """Return a lightweight customer state without making a VPN API request."""
    paid_records = _customer_payment_records(user_id)
    if paid_records:
        payment_id, latest = paid_records[0]
        state = "expired" if _payment_looks_expired(latest, now=now) else "paid"
        return {
            "state": state,
            "renewal_token": _renewal_token_for_record(user_id, payment_id, latest),
        }

    try:
        from utils.test_config import get_test_config_journey

        trial = get_test_config_journey(user_id, now=now)
    except Exception:
        trial = None
    if trial:
        return {
            "state": "activated_trial" if trial.get("connected_at") else "unused_trial",
            **trial,
        }
    return {"state": "new"}


def build_customer_welcome(user_id, language):
    """Build state-aware welcome copy and its progressive next actions."""
    from utils.translations import get_button_text, get_message_text

    journey = get_customer_journey_state(user_id)
    state = journey["state"]
    record_main_growth_event(
        "onboarding_viewed",
        user_id,
        language=language,
        deduplication_key=f"main:onboarding_viewed:{user_id}:{state}",
        journey_state=state,
    )
    text = get_message_text(language, f"welcome_{state}").format(
        remaining_days=journey.get("remaining_days", 0),
        traffic_gb=journey.get("traffic_gb", 1),
    )
    markup = types.InlineKeyboardMarkup(row_width=1)

    if state == "new":
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "start_free_test"),
            callback_data="start_free_test",
        ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_plans"),
            callback_data="welcome:plans",
        ))
    elif state == "unused_trial":
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "need_help"),
            callback_data="trial_need_help",
        ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "connected"),
            callback_data="trial_connected",
        ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_plans"),
            callback_data="welcome:plans",
        ))
    elif state == "activated_trial":
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_plans"),
            callback_data="welcome:plans",
        ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "my_configs"),
            callback_data="welcome:configs",
        ))
    else:
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "my_configs"),
            callback_data="welcome:configs",
        ))
        renewal_token = journey.get("renewal_token")
        if renewal_token:
            markup.add(types.InlineKeyboardButton(
                get_button_text(
                    language,
                    "renew_plan" if state == "expired" else "reserve_renewal",
                ),
                callback_data=f"renew_plan:{renewal_token}",
            ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_plans"),
            callback_data="welcome:plans",
        ))

    return text, markup
