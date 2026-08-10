from telebot import types
from utils.command import *
from utils.api_client import MultiServerAPI
from utils.account_state import inspect_account


def _account_description(username, details):
    state = inspect_account(details, source="admin_search")
    traffic = (details.get("max_download_bytes", 0) or 0) / (1024 ** 3)
    description = (
        f"Traffic Limit: {traffic:.2f} GB, "
        f"State: {state.state}, "
        f"Configured Duration: {state.configured_days if state.configured_days is not None else 'Unknown'} days"
    )
    remaining = (
        f"\nPanel time remaining: {state.panel_days_remaining} days"
        if state.panel_days_remaining is not None
        else ""
    )
    message = (
        f"Name: {username}\n"
        f"Traffic limit: {traffic:.2f} GB\n"
        f"State: {state.state}\n"
        f"Configured duration: {state.configured_days if state.configured_days is not None else 'Unknown'} days"
        f"{remaining}\n"
        f"Account Creation: {details.get('account_creation_date') or 'Not started'}\n"
        f"Blocked: {details.get('blocked')}"
    )
    return description, message

@bot.inline_handler(lambda query: is_admin(query.from_user.id))
def handle_inline_query(query):
    multi_api = MultiServerAPI()
    if not multi_api.servers:
        bot.answer_inline_query(query.id, results=[], switch_pm_text="Error retrieving users.", switch_pm_user_id=query.from_user.id)
        return

    users = {}
    for api_client, username, details in multi_api.iter_all_users():
        if username:
            users[username] = details

    query_text = query.query.lower()
    results = []

    if query_text == "block":
        for username, details in users.items():
            if details.get('blocked', False):
                title = f"{username} (Blocked)"
                description, message = _account_description(username, details)
                results.append(types.InlineQueryResultArticle(
                    id=username,
                    title=title,
                    description=description,
                    input_message_content=types.InputTextMessageContent(
                        message_text=message
                    )
                ))
    else:
        for username, details in users.items():
            if query_text in username.lower():
                title = f"{username}"
                description, message = _account_description(username, details)
                results.append(types.InlineQueryResultArticle(
                    id=username,
                    title=title,
                    description=description,
                    input_message_content=types.InputTextMessageContent(
                        message_text=message
                    )
                ))

    bot.answer_inline_query(query.id, results, cache_time=0)
