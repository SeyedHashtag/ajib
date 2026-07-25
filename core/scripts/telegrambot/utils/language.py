import os
import json
from telebot import types
from utils.command import bot
from utils.translations import LANGUAGES, BUTTON_TRANSLATIONS
from utils.atomic_store import locked_json, read_json

# Path to store user language preferences - using relative path for better compatibility
LANGUAGE_PREFS_FILE = '/etc/ajib/core/scripts/telegrambot/user_languages.json'

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

# Function to set user language - this overrides the one in translations.py
def set_user_language(user_id, language_code):
    """Set the language preference for a user"""
    user_id_str = str(user_id)
    with locked_json(LANGUAGE_PREFS_FILE, {}) as languages:
        if not isinstance(languages, dict):
            raise ValueError("Language preference store must contain an object.")
        languages[user_id_str] = language_code

@bot.message_handler(func=lambda message: any(
    message.text == translations["language"] 
    for translations in BUTTON_TRANSLATIONS.values()
))
def language_selection(message):
    """Display language selection menu"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    # Create buttons for each available language
    language_buttons = []
    for code, name in LANGUAGES.items():
        language_buttons.append(types.InlineKeyboardButton(name, callback_data=f"lang:{code}"))
    
    markup.add(*language_buttons)
    
    bot.reply_to(
        message,
        "🌐 Select your language / زبان خود را انتخاب کنید:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang:'))
def handle_language_selection(call):
    """Handle language selection from the inline keyboard"""
    language_code = call.data.split(':')[1]
    user_id = call.from_user.id
    
    # Debug print
    print(f"Setting language for user {user_id} to {language_code}")
    
    # Save user's language preference
    set_user_language(user_id, language_code)
    
    # Debug print
    languages = load_user_languages()
    print(f"Current language preferences: {languages}")
    
    # Get language name for the selected code
    language_name = LANGUAGES.get(language_code, "Unknown")
      # Import common here to avoid circular import
    from utils.common import create_main_markup
    
    # Update the message to indicate selected language and show main menu
    bot.edit_message_text(
        f"✅ Language set to {language_name}",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )
    
    # Update the main menu with the new language
    bot.send_message(
        call.message.chat.id,
        "👇 Main Menu",
        reply_markup=create_main_markup(is_admin=False, user_id=user_id)
    )
