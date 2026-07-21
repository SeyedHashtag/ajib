from utils.command import bot
from utils.download_guidance import render_download_callback, send_download_prompt
from utils.language import get_user_language
from utils.translations import BUTTON_TRANSLATIONS, get_message_text


@bot.message_handler(func=lambda message: any(
    message.text == translations["downloads"]
    for translations in BUTTON_TRANSLATIONS.values()
))
def downloads(message):
    """Show the shared platform and application download flow."""
    send_download_prompt(
        bot,
        message.chat.id,
        get_user_language(message.from_user.id),
        reply_to=message,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("download:"))
def handle_download_selection(call):
    """Handle platform, application, and back navigation."""
    language = get_user_language(call.from_user.id)
    try:
        render_download_callback(bot, call, language)
    except Exception:
        try:
            bot.answer_callback_query(
                call.id,
                text=get_message_text(language, "download_error"),
                show_alert=True,
            )
        except Exception:
            pass
