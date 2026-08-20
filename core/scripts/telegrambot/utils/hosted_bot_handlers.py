import telebot
from telebot import types

from utils.command import bot, is_admin
from utils.common import admin_action_text
from utils.hosted_bots import (
    disconnect_bot, get_bot, get_ledger, list_bots,
    list_pending_earnings_withdrawals, register_bot,
    resolve_earnings_withdrawal, set_bot_enabled,
)
from utils.hosted_translations import hosted_text
from utils.language import get_user_language
from utils.reseller import get_reseller_data
from utils.telegram_safe import safe_answer_callback_query, safe_send_message


TOKEN_INPUT = set()


def _owner_text(user_id, key, **values):
    return hosted_text(get_user_language(user_id), key, **values)


def _registration_error_text(user_id, detail):
    normalized = str(detail or "").lower()
    if "already connected" in normalized:
        return _owner_text(user_id, "bot_already_connected")
    if "already has" in normalized:
        return _owner_text(user_id, "bot_capacity_reached")
    if "primary service bot token" in normalized:
        return _owner_text(user_id, "main_token_forbidden")
    return _owner_text(user_id, "token_rejected")


def _menu_markup(reseller_id, record):
    connected = bool(record)
    markup = types.InlineKeyboardMarkup(row_width=1)
    if connected:
        username = str(record.get("username", "")).lstrip("@")
        if username:
            markup.add(types.InlineKeyboardButton(
                _owner_text(reseller_id, "open_owner_setup"),
                url=f"https://t.me/{username}?start=owner_setup",
            ))
            markup.add(types.InlineKeyboardButton(
                _owner_text(reseller_id, "open_storefront"),
                url=f"https://t.me/{username}",
            ))
        markup.add(types.InlineKeyboardButton(
            _owner_text(reseller_id, "refresh_status"),
            callback_data="hosted:refresh",
        ))
    else:
        markup.add(types.InlineKeyboardButton("@BotFather", url="https://t.me/BotFather"))
    markup.add(types.InlineKeyboardButton(
        _owner_text(reseller_id, "replace_token" if connected else "connect_bot"),
        callback_data="hosted:token",
    ))
    if connected:
        markup.add(types.InlineKeyboardButton(
            _owner_text(reseller_id, "disconnect_bot"),
            callback_data="hosted:disconnect",
        ))
    return markup


def _render_menu(chat_id, reseller_id, message_id=None):
    record = get_bot(reseller_id)
    if record:
        error_text = record.get("last_error")
        status = str(record.get("status", "unknown"))
        lines = [
            _owner_text(reseller_id, "hosted_connection_title"),
            "",
            _owner_text(reseller_id, "hosted_bot_line", username=record.get("username", "unknown")),
            _owner_text(
                reseller_id,
                "hosted_status_line",
                status=_owner_text(reseller_id, f"status_{status}")
                if status in {"starting", "active", "error", "blocked", "disabled", "disconnected"}
                else _owner_text(reseller_id, "status_unknown"),
            ),
        ]
        if error_text:
            lines.append(_owner_text(
                reseller_id,
                "hosted_last_error",
                error=str(error_text).replace("`", "'")[:300],
            ))
        lines.extend(["", _owner_text(reseller_id, "hosted_connected_help")])
        text = "\n".join(lines)
    else:
        text = f"{_owner_text(reseller_id, 'hosted_connection_title')}\n\n{_owner_text(reseller_id, 'hosted_connect_intro')}"
    kwargs = {"reply_markup": _menu_markup(reseller_id, record), "parse_mode": "Markdown"}
    if message_id:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
    else:
        bot.send_message(chat_id, text, **kwargs)


@bot.callback_query_handler(func=lambda call: call.data == "hosted:menu")
def hosted_menu(call):
    reseller = get_reseller_data(call.from_user.id)
    if not reseller or reseller.get("status") not in {"approved", "suspended"}:
        safe_answer_callback_query(
            bot, call.id, _owner_text(call.from_user.id, "reseller_required"), show_alert=True
        )
        return
    safe_answer_callback_query(bot, call.id)
    _render_menu(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "hosted:refresh")
def hosted_refresh(call):
    safe_answer_callback_query(bot, call.id)
    _render_menu(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "hosted:token")
def hosted_token_prompt(call):
    reseller = get_reseller_data(call.from_user.id)
    if not reseller or reseller.get("status") != "approved":
        safe_answer_callback_query(
            bot, call.id, _owner_text(call.from_user.id, "active_reseller_required"), show_alert=True
        )
        return
    TOKEN_INPUT.add(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    bot.send_message(call.message.chat.id, _owner_text(call.from_user.id, "token_prompt"))


@bot.message_handler(func=lambda message: message.from_user.id in TOKEN_INPUT)
def hosted_token_input(message):
    if (message.text or "").strip().lower() == "/cancel":
        TOKEN_INPUT.discard(message.from_user.id)
        bot.reply_to(message, _owner_text(message.from_user.id, "connection_canceled"))
        return
    token = (message.text or "").strip()
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if not token or ":" not in token:
        bot.send_message(message.chat.id, _owner_text(message.from_user.id, "token_shape_invalid"))
        return
    candidate = None
    try:
        candidate = telebot.TeleBot(token, threaded=False)
        info = candidate.get_me()
        main_id = bot.get_me().id
    except Exception:
        bot.send_message(message.chat.id, _owner_text(message.from_user.id, "token_rejected"))
        return
    finally:
        if candidate is not None:
            try:
                candidate.close_session()
            except Exception:
                pass
    try:
        success, result = register_bot(message.from_user.id, token, info, main_bot_id=main_id)
    except Exception as error:
        print(f"Hosted bot registration failed: {type(error).__name__}", flush=True)
        bot.send_message(message.chat.id, _owner_text(message.from_user.id, "token_save_failed"))
        return
    if not success:
        bot.send_message(message.chat.id, _registration_error_text(message.from_user.id, result))
        return
    TOKEN_INPUT.discard(message.from_user.id)
    username = str(result["username"]).lstrip("@")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        _owner_text(message.from_user.id, "open_owner_setup"),
        url=f"https://t.me/{username}?start=owner_setup",
    ))
    markup.add(types.InlineKeyboardButton(
        _owner_text(message.from_user.id, "open_storefront"),
        url=f"https://t.me/{username}",
    ))
    bot.send_message(
        message.chat.id,
        _owner_text(message.from_user.id, "connected_success", username=username),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "hosted:disconnect")
def hosted_disconnect(call):
    record = get_bot(call.from_user.id)
    if not record:
        safe_answer_callback_query(
            bot, call.id, _owner_text(call.from_user.id, "no_hosted_bot"), show_alert=True
        )
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        _owner_text(call.from_user.id, "confirm_disconnect"),
        callback_data="hosted:disconnect:confirm",
    ))
    markup.add(types.InlineKeyboardButton(
        _owner_text(call.from_user.id, "cancel_disconnect"),
        callback_data="hosted:disconnect:cancel",
    ))
    safe_answer_callback_query(bot, call.id)
    bot.edit_message_text(
        _owner_text(
            call.from_user.id,
            "disconnect_warning",
            username=record.get("username", "unknown"),
        ),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("hosted:disconnect:"))
def hosted_disconnect_resolution(call):
    action = call.data.rsplit(":", 1)[-1]
    if action == "confirm" and disconnect_bot(call.from_user.id):
        safe_answer_callback_query(
            bot, call.id, _owner_text(call.from_user.id, "disconnect_confirmed"), show_alert=True
        )
    else:
        safe_answer_callback_query(bot, call.id)
    _render_menu(call.message.chat.id, call.from_user.id, call.message.message_id)


def _admin_text():
    records = list_bots()
    active = sum(1 for item in records.values() if item.get("status") == "active")
    errors = sum(1 for item in records.values() if item.get("status") == "error")
    withdrawals = list_pending_earnings_withdrawals()
    lines = ["🤖 *Hosted Bots*", "", f"Registered: {len(records)}", f"Active: {active}",
             f"Errors: {errors}", f"Pending earnings withdrawals: {len(withdrawals)}", ""]
    for reseller_id, record in sorted(records.items()):
        ledger = get_ledger(reseller_id)
        lines.append(
            f"`{reseller_id}` · @{record.get('username', '?')} · {record.get('status', '?')} · "
            f"earnings ${float(ledger.get('earnings_available', 0)):.2f} · "
            f"referrals owed ${float(ledger.get('referral_liability', 0)):.2f}"
        )
    return "\n".join(lines)


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == admin_action_text("hosted_bots"))
def hosted_admin(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for reseller_id, record in sorted(list_bots().items()):
        action = "disable" if record.get("enabled", True) else "enable"
        markup.add(types.InlineKeyboardButton(
            f"{'🛑' if action == 'disable' else '▶️'} {reseller_id} @{record.get('username', '?')}",
            callback_data=f"hosted_admin:{action}:{reseller_id}",
        ))
    for request in list_pending_earnings_withdrawals():
        markup.add(types.InlineKeyboardButton(
            f"💸 {request['reseller_id']} · ${request['amount']:.2f}",
            callback_data=f"hosted_admin:withdrawal:{request['reseller_id']}:{request['id']}",
        ))
    bot.reply_to(message, _admin_text(), reply_markup=markup, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: call.data.startswith("hosted_admin:"))
def hosted_admin_callback(call):
    if not is_admin(call.from_user.id):
        safe_answer_callback_query(bot, call.id, "Unauthorized", show_alert=True)
        return
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in {"enable", "disable"} and len(parts) == 3:
        set_bot_enabled(parts[2], action == "enable")
        safe_answer_callback_query(bot, call.id, f"Hosted bot {action}d.", show_alert=True)
        return
    if action == "withdrawal" and len(parts) == 4:
        reseller_id, request_id = parts[2], parts[3]
        request = next((item for item in get_ledger(reseller_id).get("withdrawals", []) if item.get("id") == request_id), None)
        if not request:
            safe_answer_callback_query(bot, call.id, "Withdrawal not found.", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Mark Paid", callback_data=f"hosted_admin:resolve:paid:{reseller_id}:{request_id}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"hosted_admin:resolve:rejected:{reseller_id}:{request_id}"),
        )
        bot.send_message(call.message.chat.id,
                         f"Reseller: {reseller_id}\nAmount: ${request['amount']:.2f}\nDestination: {request['destination']}",
                         reply_markup=markup)
        safe_answer_callback_query(bot, call.id)
        return
    if action == "resolve" and len(parts) == 5:
        resolution, reseller_id, request_id = parts[2], parts[3], parts[4]
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
        success, result = resolve_earnings_withdrawal(reseller_id, request_id, resolution, call.from_user.id)
        feedback = f"Hosted-bot withdrawal marked {resolution}." if success else str(result)
        safe_answer_callback_query(bot, call.id, feedback, show_alert=True)
        if success:
            safe_send_message(bot, int(reseller_id), f"Your earnings withdrawal was {resolution} by an operator.")
            safe_send_message(bot, call.message.chat.id, feedback)
        return
    safe_answer_callback_query(bot, call.id, "Invalid action.", show_alert=True)
