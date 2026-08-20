from telebot import types
from types import MethodType


ADMIN_ACTIONS = {
    "add_user": {"text": "➕ Add User", "style": "success", "group": "users"},
    "show_user": {"text": "👤 Show User", "style": None, "group": "users"},
    "delete_user": {"text": "❌ Delete User", "style": "danger", "group": "users"},
    "server_info": {"text": "📊 Server Info", "style": "primary", "group": "reports"},
    "backup_bot": {"text": "💾 Backup Bot", "style": None, "group": "system"},
    "payment_settings": {"text": "💳 Payment Settings", "style": None, "group": "sales"},
    "edit_plans": {"text": "📝 Edit Plans", "style": None, "group": "sales"},
    "broadcast_message": {"text": "📢 Broadcast Message", "style": None, "group": "messaging"},
    "edit_support": {"text": "📞 Edit Support", "style": None, "group": "messaging"},
    "update_keyboards": {"text": "🔄 Update Keyboards", "style": None, "group": "messaging"},
    "manage_resellers": {"text": "💼 Manage Resellers", "style": None, "group": "resellers"},
    "manage_test_accounts": {"text": "🧪 Manage Test Accounts", "style": None, "group": "users"},
    "referral_payouts": {"text": "💰 Referral Payouts", "style": None, "group": "sales"},
    "vpn_servers": {"text": "⚖️ VPN Servers", "style": None, "group": "system"},
    "confirmations": {"text": "✅ Confirmations", "style": "success", "group": "sales"},
    "expired_cleanup": {"text": "🧹 Expired Cleanup", "style": "danger", "group": "users"},
    "bulk_transfer": {"text": "🔁 Mass Copy / Migrate", "style": "primary", "group": "users"},
    "bot_logs": {"text": "📄 Bot Logs", "style": None, "group": "system"},
    "hosted_bots": {"text": "🤖 Hosted Bots", "style": None, "group": "resellers"},
    "growth_funnel": {"text": "📈 Growth Funnel", "style": "primary", "group": "reports"},
}


def admin_action_text(key):
    """Return the stable display text for an admin action key."""
    return ADMIN_ACTIONS[key]["text"]


GROWTH_FUNNEL_BUTTON_TEXT = admin_action_text("growth_funnel")

ADMIN_CATEGORIES = {
    "users": {"text": "👥 Users", "style": "primary"},
    "sales": {"text": "💳 Sales", "style": "primary"},
    "resellers": {"text": "💼 Resellers", "style": "primary"},
    "system": {"text": "⚙️ System", "style": "primary"},
    "reports": {"text": "📊 Reports", "style": "primary"},
    "messaging": {"text": "📣 Messaging", "style": "primary"},
}

ADMIN_HOME_BUTTON_TEXT = "🏠 Admin Menu"

ADMIN_ROOT_MENU_ROWS = (
    (admin_action_text("confirmations"), admin_action_text("server_info")),
    (ADMIN_CATEGORIES["users"]["text"], ADMIN_CATEGORIES["sales"]["text"]),
    (ADMIN_CATEGORIES["resellers"]["text"], ADMIN_CATEGORIES["system"]["text"]),
    (ADMIN_CATEGORIES["reports"]["text"], ADMIN_CATEGORIES["messaging"]["text"]),
)

ADMIN_GROUP_MENU_ROWS = {
    "users": (
        (admin_action_text("add_user"), admin_action_text("show_user")),
        (admin_action_text("delete_user"), admin_action_text("manage_test_accounts")),
        (admin_action_text("bulk_transfer"),),
        (admin_action_text("expired_cleanup"),),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
    "sales": (
        (admin_action_text("confirmations"), admin_action_text("payment_settings")),
        (admin_action_text("edit_plans"), admin_action_text("referral_payouts")),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
    "resellers": (
        (admin_action_text("manage_resellers"), admin_action_text("hosted_bots")),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
    "system": (
        (admin_action_text("vpn_servers"), admin_action_text("backup_bot")),
        (admin_action_text("bot_logs"),),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
    "reports": (
        (admin_action_text("server_info"), admin_action_text("growth_funnel")),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
    "messaging": (
        (admin_action_text("broadcast_message"), admin_action_text("edit_support")),
        (admin_action_text("update_keyboards"),),
        (ADMIN_HOME_BUTTON_TEXT,),
    ),
}

# Backwards-compatible name for callers that render the admin's main keyboard.
ADMIN_MAIN_MENU_ROWS = ADMIN_ROOT_MENU_ROWS

ADMIN_ACTION_BUTTONS = {action["text"] for action in ADMIN_ACTIONS.values()}
ADMIN_NAVIGATION_BUTTONS = {
    category["text"] for category in ADMIN_CATEGORIES.values()
} | {ADMIN_HOME_BUTTON_TEXT}
ADMIN_MAIN_MENU_BUTTONS = ADMIN_ACTION_BUTTONS | ADMIN_NAVIGATION_BUTTONS

_ADMIN_BUTTON_STYLES = {
    action["text"]: action["style"] for action in ADMIN_ACTIONS.values()
}
_ADMIN_BUTTON_STYLES.update({
    category["text"]: category["style"] for category in ADMIN_CATEGORIES.values()
})
_ADMIN_BUTTON_STYLES[ADMIN_HOME_BUTTON_TEXT] = "primary"

_ADMIN_MENU_VIEW_BY_TEXT = {
    category["text"]: view for view, category in ADMIN_CATEGORIES.items()
}
_ADMIN_MENU_VIEW_BY_TEXT[ADMIN_HOME_BUTTON_TEXT] = "root"


def resolve_admin_menu_view(text):
    """Resolve a navigation label to its admin view, or return ``None``."""
    if not isinstance(text, str):
        return None
    return _ADMIN_MENU_VIEW_BY_TEXT.get(text)


def _create_admin_button(text):
    """Create a reply button with its configured semantic style."""
    style = _ADMIN_BUTTON_STYLES[text]
    try:
        return types.KeyboardButton(text, **({"style": style} if style else {}))
    except TypeError as error:
        # pyTelegramBotAPI releases predating Telegram's semantic button style
        # field reject the constructor keyword.  Retain wire compatibility so
        # rolling upgrades do not make the entire admin keyboard unusable.
        if not style or "style" not in str(error):
            raise
        button = types.KeyboardButton(text)
        base_to_dict = button.to_dict
        button.style = style

        def styled_to_dict(self):
            payload = base_to_dict()
            payload["style"] = self.style
            return payload

        button.to_dict = MethodType(styled_to_dict, button)
        return button


def create_admin_markup(view="root"):
    """Build the compact admin root keyboard or one of its grouped views."""
    if view == "root":
        rows = ADMIN_ROOT_MENU_ROWS
    else:
        try:
            rows = ADMIN_GROUP_MENU_ROWS[view]
        except KeyError as exc:
            raise ValueError(f"Unknown admin menu view: {view!r}") from exc

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in rows:
        markup.row(*(_create_admin_button(text) for text in row))
    return markup


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
    if is_admin:
        return create_admin_markup("root")

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
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
        return create_admin_markup("root")

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


def _payment_cleanup_proves_deleted(record):
    if record.get("cleanup_deleted_at") or record.get("removed_from_vpn"):
        return True
    delete_results = {"deleted", "already_missing"}
    if str(record.get("cleanup_delete_result", "")).lower() in delete_results:
        return True
    if str(record.get("cleanup_status", "")).lower() in delete_results:
        return True
    return False


def _payment_entitlement_state(record, now=None, multi_api=None):
    from utils.account_state import EntitlementState, inspect_account, resolve_service_cycle
    from utils.api_client import MultiServerAPI

    if _payment_cleanup_proves_deleted(record):
        return "expired"
    username = record.get("renewal_username") or record.get("username")
    server_id = record.get("renewal_server_id") or record.get("server_id") or "primary"
    if not username:
        return "unknown"
    cycle = resolve_service_cycle(
        record,
        username=username,
        server_id=server_id,
        source="customer_welcome",
    )
    try:
        api = multi_api or MultiServerAPI()
        finder = getattr(api, "find_user_on_server_cached", None)
        if callable(finder):
            _client, live, lookup = finder(username, server_id)
        else:
            _client, live, lookup = api.find_user_on_server(username, server_id)
    except Exception:
        return "unknown"
    if not isinstance(lookup, dict) or lookup.get("status") != "found" or not isinstance(live, dict):
        return "unknown"
    snapshot = inspect_account(live, cycle=cycle, now=now, source="customer_welcome")
    if snapshot.entitlement_state == EntitlementState.EXPIRED:
        return "expired"
    if snapshot.entitlement_state == EntitlementState.CURRENT:
        return "paid"
    return "unknown"


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


def get_customer_journey_state(user_id, now=None, multi_api=None):
    """Return a fail-closed customer state from the exact live VPN identity."""
    paid_records = _customer_payment_records(user_id)
    if paid_records:
        payment_id, latest = paid_records[0]
        state = _payment_entitlement_state(latest, now=now, multi_api=multi_api)
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
    elif state in {"paid", "expired"}:
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
    else:
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "my_configs"),
            callback_data="welcome:configs",
        ))
        markup.add(types.InlineKeyboardButton(
            get_button_text(language, "see_plans"),
            callback_data="welcome:plans",
        ))

    return text, markup
