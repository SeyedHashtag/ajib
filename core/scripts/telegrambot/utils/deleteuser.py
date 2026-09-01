from telebot import types
from utils.command import bot, is_admin
from utils.common import (
    admin_action_text,
    create_admin_markup,
    create_main_markup,
    is_admin_main_menu_button,
    resolve_admin_menu_view,
)
from utils.api_client import APIClient, MultiServerAPI


@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete")
def handle_cancel_delete(call):
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    bot.edit_message_text("Operation canceled.", chat_id=call.message.chat.id, message_id=call.message.message_id)

@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == admin_action_text("delete_user"))
def delete_user(message):
    markup = types.InlineKeyboardMarkup()
    cancel_button = types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete")
    markup.add(cancel_button)

    msg = bot.reply_to(message, "Enter username:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_delete_user)

def process_delete_user(message):
    username = message.text.strip().lower()

    if is_admin_main_menu_button(message.text):
        view = resolve_admin_menu_view(message.text) or "root"
        bot.reply_to(message, "Operation canceled.", reply_markup=create_admin_markup(view))
        return

    if not username:
        bot.reply_to(message, "Username cannot be empty. Operation canceled.", reply_markup=create_main_markup(is_admin=True))
        return

    multi_api = MultiServerAPI()
    api_client, _user_data, lookup = multi_api.resolve_unique_user(
        username,
        allow_exact_on_partial=False,
        force_refresh=True,
    )

    if lookup.get('status') == 'duplicate':
        bot.reply_to(
            message,
            f"Error: '{username}' exists on multiple servers. Nothing was deleted; select an exact server in the admin tools.",
            reply_markup=create_main_markup(is_admin=True),
        )
        return
    if lookup.get('status') == 'unavailable':
        bot.reply_to(
            message,
            f"Error: Could not verify '{username}' across every server. Nothing was deleted.",
            reply_markup=create_main_markup(is_admin=True),
        )
        return
    if lookup.get('status') == 'missing':
        bot.reply_to(
            message,
            f"User '{username}' does not exist on any configured server.",
            reply_markup=create_main_markup(is_admin=True),
        )
        return

    bot.send_chat_action(message.chat.id, 'typing')
    result = api_client.delete_user(username) if api_client else None

    if result is None:
        bot.reply_to(message, f"Error: Failed to delete user '{username}'. They may not exist.", reply_markup=create_main_markup(is_admin=True))
    else:
        bot.reply_to(message, f"User '{username}' removed successfully.", reply_markup=create_main_markup(is_admin=True))
