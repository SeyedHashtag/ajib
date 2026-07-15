import telebot
from telebot import types

from utils.command import bot, is_admin
from utils.hosted_bots import (
    disconnect_bot, get_bot, get_ledger, list_bots,
    list_pending_earnings_withdrawals, register_bot,
    resolve_earnings_withdrawal, set_bot_enabled,
)
from utils.reseller import get_reseller_data
from utils.telegram_safe import safe_answer_callback_query, safe_send_message


TOKEN_INPUT = set()


def _menu_markup(connected):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        "🔄 Replace Bot Token" if connected else "➕ Connect Bot",
        callback_data="hosted:token",
    ))
    if connected:
        markup.add(types.InlineKeyboardButton("🛑 Disconnect Bot", callback_data="hosted:disconnect"))
    return markup


def _render_menu(chat_id, reseller_id, message_id=None):
    record = get_bot(reseller_id)
    if record:
        error_text = record.get("last_error")
        lines = [
            "🤖 *Your Hosted Bot*",
            "",
            f"Bot: @{record.get('username', 'unknown')}",
            f"Status: `{record.get('status', 'unknown')}`",
        ]
        if error_text:
            lines.append(f"Last error: `{str(error_text)[:300]}`")
        lines.extend(["", "Configure markup, payments, support, referrals, and earnings from its owner panel."])
        text = "\n".join(lines)
    else:
        text = (
            "🤖 *Your Hosted Bot*\n\nCreate a bot with @BotFather, then connect its token here. "
            "It will run on ajib's VPN and payment infrastructure."
        )
    kwargs = {"reply_markup": _menu_markup(bool(record)), "parse_mode": "Markdown"}
    if message_id:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
    else:
        bot.send_message(chat_id, text, **kwargs)


@bot.callback_query_handler(func=lambda call: call.data == "hosted:menu")
def hosted_menu(call):
    reseller = get_reseller_data(call.from_user.id)
    if not reseller or reseller.get("status") not in {"approved", "suspended"}:
        safe_answer_callback_query(bot, call.id, "Only approved resellers can host a bot.", show_alert=True)
        return
    safe_answer_callback_query(bot, call.id)
    _render_menu(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "hosted:token")
def hosted_token_prompt(call):
    reseller = get_reseller_data(call.from_user.id)
    if not reseller or reseller.get("status") != "approved":
        safe_answer_callback_query(bot, call.id, "An approved, active reseller account is required.", show_alert=True)
        return
    TOKEN_INPUT.add(call.from_user.id)
    safe_answer_callback_query(bot, call.id)
    bot.send_message(call.message.chat.id, "Send the BotFather token now. It will be deleted after validation.\n\nSend /cancel to stop.")


@bot.message_handler(func=lambda message: message.from_user.id in TOKEN_INPUT)
def hosted_token_input(message):
    TOKEN_INPUT.discard(message.from_user.id)
    if (message.text or "").strip().lower() == "/cancel":
        bot.reply_to(message, "Bot connection canceled.")
        return
    token = (message.text or "").strip()
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if not token or ":" not in token:
        bot.send_message(message.chat.id, "That does not look like a BotFather token.")
        return
    candidate = None
    try:
        candidate = telebot.TeleBot(token, threaded=False)
        info = candidate.get_me()
        main_id = bot.get_me().id
    except Exception:
        bot.send_message(message.chat.id, "Telegram rejected that token. Check it in BotFather and try again.")
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
        bot.send_message(message.chat.id, "The bot could not be saved safely. Please contact the operator.")
        return
    if not success:
        bot.send_message(message.chat.id, str(result))
        return
    bot.send_message(message.chat.id, f"✅ @{result['username']} is connected and will start within a few seconds.")


@bot.callback_query_handler(func=lambda call: call.data == "hosted:disconnect")
def hosted_disconnect(call):
    if disconnect_bot(call.from_user.id):
        safe_answer_callback_query(bot, call.id, "Hosted bot disconnected.", show_alert=True)
        _render_menu(call.message.chat.id, call.from_user.id, call.message.message_id)
    else:
        safe_answer_callback_query(bot, call.id, "No hosted bot is connected.", show_alert=True)


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


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == "🤖 Hosted Bots")
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
        success, result = resolve_earnings_withdrawal(reseller_id, request_id, resolution, call.from_user.id)
        safe_answer_callback_query(bot, call.id, "Updated." if success else str(result), show_alert=True)
        if success:
            safe_send_message(bot, int(reseller_id), f"Your earnings withdrawal was {resolution} by an operator.")
        return
    safe_answer_callback_query(bot, call.id, "Invalid action.", show_alert=True)
