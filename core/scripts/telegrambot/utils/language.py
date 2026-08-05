import os
import json
import threading
from telebot import types
from utils.command import bot
from utils.translations import LANGUAGES, BUTTON_TRANSLATIONS, get_message_text
from utils.atomic_store import locked_json, read_json

# Path to store user language preferences - using relative path for better compatibility
LANGUAGE_PREFS_FILE = '/etc/ajib/core/scripts/telegrambot/user_languages.json'
_PENDING_REFERRAL_CONFIRMATIONS = set()
_PENDING_REFERRAL_LOCK = threading.Lock()


def defer_referral_confirmation(user_id):
    with _PENDING_REFERRAL_LOCK:
        _PENDING_REFERRAL_CONFIRMATIONS.add(str(user_id))


def consume_deferred_referral_confirmation(user_id):
    key = str(user_id)
    with _PENDING_REFERRAL_LOCK:
        if key not in _PENDING_REFERRAL_CONFIRMATIONS:
            return False
        _PENDING_REFERRAL_CONFIRMATIONS.remove(key)
        return True


def normalize_telegram_language(language_code):
    """Return a supported AJIB language for a Telegram language code."""
    if not language_code:
        return None
    normalized = str(language_code).strip().lower().replace("_", "-")
    primary = normalized.split("-", 1)[0]
    return primary if primary in LANGUAGES else None

def load_user_languages():
    """Load user language preferences from file"""
    data = read_json(LANGUAGE_PREFS_FILE, {})
    return data if isinstance(data, dict) else {}

def save_user_languages(languages_data):
    """Save user language preferences to file"""
    try:
        with locked_json(LANGUAGE_PREFS_FILE, {}) as stored:
            if not isinstance(stored, dict):
                raise ValueError("Language preference store must contain an object.")
            stored.clear()
            stored.update(languages_data if isinstance(languages_data, dict) else {})
    except Exception as e:
        print(f"Error saving language preferences: {e}")

# Function to get user language - this overrides the one in translations.py
def get_user_language(user_id):
    """Get the language preference for a user"""
    user_id_str = str(user_id)
    languages = load_user_languages()
    return languages.get(user_id_str, "en")


def has_user_language(user_id):
    """Whether the user has explicitly selected or had a supported language detected."""
    return str(user_id) in load_user_languages()


def resolve_user_language(user_id, telegram_language_code=None):
    """Resolve a stored language, or persist a supported Telegram language.

    ``None`` means Telegram did not provide a language we support and the bot
    should ask the customer to choose before showing onboarding copy.
    """
    if has_user_language(user_id):
        return get_user_language(user_id)
    detected = normalize_telegram_language(telegram_language_code)
    if detected:
        set_user_language(user_id, detected)
    return detected

# Function to set user language - this overrides the one in translations.py
def set_user_language(user_id, language_code):
    """Set the language preference for a user"""
    user_id_str = str(user_id)
    with locked_json(LANGUAGE_PREFS_FILE, {}) as languages:
        if not isinstance(languages, dict):
            raise ValueError("Language preference store must contain an object.")
        languages[user_id_str] = language_code


def build_language_selection_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        types.InlineKeyboardButton(name, callback_data=f"lang:{code}")
        for code, name in LANGUAGES.items()
    ])
    return markup

@bot.message_handler(func=lambda message: any(
    message.text == translations["language"] 
    for translations in BUTTON_TRANSLATIONS.values()
))
def language_selection(message):
    """Display language selection menu"""
    bot.reply_to(
        message,
        get_message_text("en", "language_selection_prompt"),
        reply_markup=build_language_selection_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang:'))
def handle_language_selection(call):
    """Handle language selection from the inline keyboard"""
    language_code = call.data.split(':', 1)[1]
    user_id = call.from_user.id
    if language_code not in LANGUAGES:
        bot.answer_callback_query(call.id)
        return

    set_user_language(user_id, language_code)
    language_name = LANGUAGES.get(language_code, "Unknown")

    from utils.common import build_customer_welcome, create_main_markup

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        get_message_text(language_code, "language_selected").format(language=language_name),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )

    welcome_text, welcome_markup = build_customer_welcome(user_id, language_code)
    bot.send_message(
        call.message.chat.id,
        welcome_text,
        reply_markup=welcome_markup,
        parse_mode="Markdown",
    )
    bot.send_message(
        call.message.chat.id,
        get_message_text(language_code, "main_menu_ready"),
        reply_markup=create_main_markup(is_admin=False, user_id=user_id),
    )
    if consume_deferred_referral_confirmation(user_id):
        bot.send_message(
            call.message.chat.id,
            get_message_text(language_code, "referral_registered"),
        )
