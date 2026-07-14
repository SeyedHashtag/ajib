#!/usr/bin/env python3
"""Isolated polling worker for one reseller-owned Telegram bot."""

import io
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

os.environ["AJIB_BOT_ROLE"] = "hosted"

import qrcode
import telebot
from dotenv import load_dotenv
from telebot import types

BOT_DIR = os.getenv("AJIB_BOT_DIR", "/etc/ajib/core/scripts/telegrambot")
load_dotenv(os.path.join(BOT_DIR, ".env"))

from utils.api_client import APIClient, MultiServerAPI
from utils.atomic_store import locked_json, read_json
from utils.currency_format import format_toman_amount, format_usd_amount
from utils.hosted_bots import (
    add_referral_liability, calculate_quote, consume_credit,
    consume_renewal_credit, credit_crypto_sale, get_ledger, get_settings,
    release_credit, request_earnings_withdrawal, reserve_credit,
    set_bot_runtime_status, settle_referral_liability, tenant_file, transfer_earnings_to_debt,
    update_settings,
)
from utils.hosted_translations import HOSTED_TRANSLATIONS, hosted_text
from utils.payments import CryptoPayment
from utils.reseller import (
    can_reseller_add_debt, get_reseller_data, get_reseller_total_paid,
    get_reseller_trust_limit, record_funded_reseller_config,
    record_funded_reseller_renewal,
)
from utils.translations import BUTTON_TRANSLATIONS, LANGUAGES, get_button_text, get_message_text
from utils.username_utils import allocate_username, build_user_note


TOKEN = os.environ["AJIB_HOSTED_BOT_TOKEN"]
OWNER_ID = int(os.environ["AJIB_HOSTED_RESELLER_ID"])
BOT_USERNAME = os.getenv("AJIB_HOSTED_BOT_USERNAME", "").lstrip("@")
PLANS_FILE = os.path.join(BOT_DIR, "plans.json")
GLOBAL_TEST_FILE = os.path.join(BOT_DIR, "test_configs.json")
INPUT_STATE = {}
RENEWAL_TOKENS = {}
PAYMENT_LOCK = threading.RLock()

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=4)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _reseller(active_only=False):
    data = get_reseller_data(OWNER_ID) or {}
    allowed = {"approved"} if active_only else {"approved", "suspended"}
    return data if data.get("status") in allowed else None


def _load_plans():
    plans = read_json(PLANS_FILE, {})
    return plans if isinstance(plans, dict) else {}


def _sellable_plans():
    settings = get_settings(OWNER_ID)
    enabled = {str(value) for value in settings.get("enabled_plan_ids", [])}
    result = {}
    for plan_id, plan in _load_plans().items():
        if not isinstance(plan, dict) or plan.get("target", "both") == "customer":
            continue
        if settings.get("plan_selection_configured") and str(plan_id) not in enabled:
            continue
        result[str(plan_id)] = plan
    return result


def _languages():
    return read_json(tenant_file(OWNER_ID, "languages.json"), {})


def _language(user_id):
    return _languages().get(str(user_id), "en")


def _set_language(user_id, value):
    with locked_json(tenant_file(OWNER_ID, "languages.json"), {}) as languages:
        languages[str(user_id)] = value


def _button(user_id, key, fallback):
    return BUTTON_TRANSLATIONS.get(_language(user_id), BUTTON_TRANSLATIONS["en"]).get(key, fallback)


def _message(user_id, key):
    return get_message_text(_language(user_id), key)


def _hosted_message(user_id, key, **values):
    return hosted_text(_language(user_id), key, **values)


def _all_button_values(key, fallback):
    return {items.get(key, fallback) for items in BUTTON_TRANSLATIONS.values()}


def _main_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(_button(user_id, "my_configs", "📱 My Configs"), _button(user_id, "purchase_plan", "💳 Purchase Plan"))
    markup.row(_button(user_id, "downloads", "⬇️ Downloads"), _button(user_id, "test_config", "🎁 Test Config"))
    markup.row(_button(user_id, "referral", "💰 Earn Crypto"), _button(user_id, "support", "📞 Support"))
    markup.row(_button(user_id, "language", "🌐 Language/زبان"))
    if user_id == OWNER_ID:
        markup.row(_hosted_message(user_id, "owner_panel"))
    return markup


def _tenant_payments():
    data = read_json(tenant_file(OWNER_ID, "payments.json"), {})
    return data if isinstance(data, dict) else {}


def _save_payment(payment_id, record):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        current = payments.get(payment_id, {})
        current.update(record)
        current.setdefault("created_at", _now())
        current["updated_at"] = _now()
        payments[payment_id] = current
        return dict(current)


def _claim_payment(payment_id, allowed):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        record = payments.get(payment_id)
        if not record or record.get("status") not in set(allowed):
            return None
        record["status"] = "processing"
        record["updated_at"] = _now()
        return dict(record)


def _find_customer_configs(user_id):
    reseller = get_reseller_data(OWNER_ID) or {}
    return [item for item in reseller.get("configs", []) if isinstance(item, dict)
            and str(item.get("customer_telegram_id")) == str(user_id)
            and not item.get("removed_from_vpn")]


def _referral_data():
    return read_json(tenant_file(OWNER_ID, "referrals.json"), {
        "referrals": {}, "codes": {}, "user_codes": {}, "stats": {}, "wallets": {},
        "pending_withdrawals": [], "payouts": [],
    })


def _ensure_referral_code(user_id):
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        key = str(user_id)
        if key in data.setdefault("user_codes", {}):
            return data["user_codes"][key]
        code = uuid.uuid4().hex[:8]
        while code in data.setdefault("codes", {}):
            code = uuid.uuid4().hex[:8]
        data["codes"][code] = key
        data["user_codes"][key] = code
        data.setdefault("stats", {}).setdefault(key, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        return code


def _register_referral(user_id, code):
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        key = str(user_id)
        referrer = data.setdefault("codes", {}).get(code)
        if not referrer or referrer == key or key in data.setdefault("referrals", {}):
            return False
        data["referrals"][key] = referrer
        stats = data.setdefault("stats", {}).setdefault(referrer, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        stats["count"] = int(stats.get("count", 0)) + 1
        return True


def _credit_referral(order_id, customer_id, reward):
    if float(reward or 0) <= 0:
        return 0.0
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        referrer = data.setdefault("referrals", {}).get(str(customer_id))
        if not referrer:
            return 0.0
        rewarded = data.setdefault("rewarded_orders", {})
        if order_id in rewarded:
            return float(rewarded[order_id])
        stats = data.setdefault("stats", {}).setdefault(referrer, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        stats["total_earnings"] = round(float(stats.get("total_earnings", 0)) + float(reward), 2)
        stats["available_balance"] = round(float(stats.get("available_balance", 0)) + float(reward), 2)
        rewarded[order_id] = float(reward)
    return float(reward)


def _create_user(customer_id, plan, note):
    multi = MultiServerAPI()
    prefix = f"h{OWNER_ID}u"

    def allocate(existing):
        return allocate_username(prefix, customer_id, existing)

    def create(client, username):
        payload = build_user_note(username, plan["gb"], plan["days"], unlimited=plan.get("unlimited", False), note_text=note)
        result = client.add_user(username, int(plan["gb"]), int(plan["days"]), unlimited=plan.get("unlimited", False), note=payload)
        return result if result is not None else client.add_user(username, int(plan["gb"]), int(plan["days"]), unlimited=plan.get("unlimited", False))

    return multi.create_user_with_retry(allocate, create)


def _deliver_config(chat_id, username, client, renewed=False):
    uri = client.get_user_uri(username) if client else None
    action = _hosted_message(chat_id, "renewed" if renewed else "created")
    if not uri or not uri.get("normal_sub"):
        bot.send_message(chat_id, _hosted_message(chat_id, "config_no_url", action=action, username=username),
                         parse_mode="Markdown")
        return
    url = uri.get("ipv4") or uri["normal_sub"]
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.send_photo(chat_id, image,
                   caption=_hosted_message(chat_id, "config_ready", action=action, username=username,
                                           subscription=uri["normal_sub"]),
                   parse_mode="Markdown")


def _provision_payment(payment_id, record, funded):
    customer_id = int(record["user_id"])
    username = record.get("renew_username")
    client = None
    renewed = bool(username)
    reseller_snapshot = get_reseller_data(OWNER_ID) or {}
    existing_config = None
    for item in reseller_snapshot.get("configs", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("retail_order_id") or "") == str(payment_id):
            existing_config = item
            break
        if any(str(renewal.get("retail_order_id") or "") == str(payment_id)
               for renewal in item.get("renewals", []) if isinstance(renewal, dict)):
            existing_config = item
            renewed = True
            break
    if existing_config:
        username = existing_config.get("username")
        client, _ = MultiServerAPI().find_user(username, preferred_server_id=existing_config.get("server_id"))
        metadata = {"username": username, "server_id": existing_config.get("server_id"),
                    "retail_order_id": payment_id, "customer_telegram_id": customer_id}
        if funded:
            credit_crypto_sale(OWNER_ID, payment_id, record["margin"], record.get("referral_reward", 0), metadata)
        else:
            release_credit(OWNER_ID, payment_id, kind="credit_recovered")
            add_referral_liability(OWNER_ID, payment_id, record.get("referral_reward", 0), metadata)
        _credit_referral(payment_id, customer_id, record.get("referral_reward", 0))
        _save_payment(payment_id, {"status": "completed", "username": username,
                                   "server_id": existing_config.get("server_id")})
        _deliver_config(customer_id, username, client, renewed=renewed)
        return True, username
    if renewed:
        client, live = MultiServerAPI().find_user(username, preferred_server_id=record.get("server_id"))
        if not client or not live or client.reset_user(username) is None:
            return False, "VPN renewal failed"
        _save_payment(payment_id, {"provisioned_username": username,
                                   "provisioned_server_id": getattr(client, "server_id", None)})
    else:
        provisioned_username = record.get("provisioned_username")
        if provisioned_username:
            client, live = MultiServerAPI().find_user(provisioned_username,
                                                       preferred_server_id=record.get("provisioned_server_id"))
            username = provisioned_username if client and live else None
        if not username:
            plan = {"gb": record["plan_gb"], "days": record["days"], "unlimited": record.get("unlimited", False)}
            username, result, client = _create_user(customer_id, plan, f"hosted reseller {OWNER_ID}")
            if result is None:
                return False, "VPN user creation failed"
            _save_payment(payment_id, {"provisioned_username": username,
                                       "provisioned_server_id": getattr(client, "server_id", None)})
    server_id = getattr(client, "server_id", None)
    common = {
        "username": username, "customer_telegram_id": customer_id,
        "customer_telegram_username": record.get("telegram_username"),
        "server_id": server_id, "reseller_id": str(OWNER_ID),
        "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"), "retail_order_id": payment_id,
        "retail_price": record["retail_price"], "price": record["wholesale_price"],
        "plan_gb": record["plan_gb"], "days": record["days"],
    }
    if funded:
        accounted = (record_funded_reseller_renewal(OWNER_ID, username, record["wholesale_price"], common, server_id)
                     if renewed else record_funded_reseller_config(OWNER_ID, record["wholesale_price"], common))
    else:
        accounted = (consume_renewal_credit(OWNER_ID, payment_id, username, common, server_id)
                     if renewed else consume_credit(OWNER_ID, payment_id, common))
    if not accounted:
        if not renewed and client:
            client.delete_user(username)
        return False, "Reseller accounting failed"
    if funded:
        credit_crypto_sale(OWNER_ID, payment_id, record["margin"], record.get("referral_reward", 0), common)
    else:
        add_referral_liability(OWNER_ID, payment_id, record.get("referral_reward", 0), common)
    _credit_referral(payment_id, customer_id, record.get("referral_reward", 0))
    _save_payment(payment_id, {"status": "completed", "username": username, "server_id": server_id})
    _deliver_config(customer_id, username, client, renewed=renewed)
    return True, username


def _show_plans(chat_id, user_id, message_id=None):
    language = _language(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    settings = get_settings(OWNER_ID)
    for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
        quote = calculate_quote(plan["price"], settings["markup_percent"])
        account_type = get_message_text(
            language, "unlimited_users" if plan.get("unlimited", False) else "single_user"
        )
        markup.add(types.InlineKeyboardButton(
            f"{plan_id} GB · {plan.get('days', 30)} days · ${format_usd_amount(quote['retail'])}{account_type}",
            callback_data=f"hb:buy:{plan_id}",
        ))
    text = _message(user_id, "select_plan") if markup.keyboard else _message(user_id, "plan_not_found")
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


def _purchase_options(chat_id, user_id, plan_id, renewal=None, message_id=None):
    reseller = _reseller(active_only=True)
    if not reseller:
        bot.send_message(chat_id, _hosted_message(user_id, "purchase_unavailable"))
        return
    plan = _sellable_plans().get(str(plan_id))
    if not plan:
        bot.send_message(chat_id, _message(user_id, "plan_not_found"))
        return
    settings = get_settings(OWNER_ID)
    language = _language(user_id)
    quote = calculate_quote(plan["price"], settings["markup_percent"], settings["referral_margin_percent"],
                            referred=str(user_id) in _referral_data().get("referrals", {}))
    markup = types.InlineKeyboardMarkup(row_width=1)
    suffix = ""
    if renewal:
        token = uuid.uuid4().hex[:12]
        RENEWAL_TOKENS[token] = dict(renewal)
        suffix = f":{token}"
    if settings.get("card_number"):
        markup.add(types.InlineKeyboardButton(get_button_text(language, "card_to_card"),
                                              callback_data=f"hb:pay:card:{plan_id}{suffix}"))
    if settings.get("crypto_enabled") and quote["crypto_supported"] and os.getenv("CRYPTO_MERCHANT_ID") and os.getenv("CRYPTO_API_KEY"):
        markup.add(types.InlineKeyboardButton(
            get_message_text(language, "crypto_discount_button").format(percent=5),
            callback_data=f"hb:pay:crypto:{plan_id}{suffix}",
        ))
    if not markup.keyboard:
        bot.send_message(chat_id, _message(user_id, "no_payment_methods"))
        return
    markup.add(types.InlineKeyboardButton(get_button_text(language, "back"), callback_data="hb:plans"))
    exchange_rate = float(settings.get("exchange_rate", 1) or 1)
    unlimited_text = get_button_text(language, "yes" if plan.get("unlimited", False) else "no")
    text = _message(user_id, "plan_details")
    text += _message(user_id, "data").format(plan_gb=plan_id)
    text += _message(user_id, "duration").format(days=plan.get("days", 30))
    text += _message(user_id, "unlimited").format(unlimited_text=unlimited_text)
    text += _message(user_id, "price").format(price=format_usd_amount(quote["retail"]))
    text += _message(user_id, "exchange_rate").format(exchange_rate=format_toman_amount(exchange_rate))
    text += _message(user_id, "toman_price").format(
        toman_price=format_toman_amount(quote["retail"] * exchange_rate)
    )
    text += _message(user_id, "purchase_connection_warning")
    text += _message(user_id, "select_payment_method")
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


@bot.message_handler(commands=["start"])
def start(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        _register_referral(message.from_user.id, parts[1].strip())
    settings = get_settings(OWNER_ID)
    bot.reply_to(message, settings.get("welcome_text") or "Welcome!", reply_markup=_main_markup(message.from_user.id))


@bot.message_handler(func=lambda m: m.text in _all_button_values("purchase_plan", "💳 Purchase Plan"))
def plans(message):
    _show_plans(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "hb:plans")
def plans_back(call):
    bot.answer_callback_query(call.id)
    _show_plans(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:buy:"))
def buy(call):
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, call.data.split(":")[2],
                      message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:pay:"))
def payment_method(call):
    parts = call.data.split(":")
    method, plan_id = parts[2], parts[3]
    renewal = RENEWAL_TOKENS.get(parts[4]) if len(parts) >= 5 else None
    plan = _sellable_plans().get(plan_id)
    settings = get_settings(OWNER_ID)
    if not plan or not _reseller(active_only=True):
        bot.answer_callback_query(call.id, _message(call.from_user.id, "plan_not_found"), show_alert=True)
        return
    referred = str(call.from_user.id) in _referral_data().get("referrals", {})
    quote = calculate_quote(plan["price"], settings["markup_percent"], settings["referral_margin_percent"], referred)
    order_id = str(uuid.uuid4())
    record = {
        "id": order_id, "user_id": call.from_user.id, "telegram_username": call.from_user.username,
        "reseller_id": str(OWNER_ID), "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
        "plan_gb": plan_id, "days": plan.get("days", 30), "unlimited": plan.get("unlimited", False),
        "wholesale_price": quote["wholesale"], "retail_price": quote["retail"],
        "referral_reward": quote["card_referral_reward"] if method == "card" else quote["crypto_referral_reward"],
        "payment_method": method,
        "renew_username": renewal and renewal["username"], "server_id": renewal and renewal.get("server_id"),
    }
    if len(parts) >= 5:
        RENEWAL_TOKENS.pop(parts[4], None)
    if method == "card":
        reseller = get_reseller_data(OWNER_ID) or {}
        _, _, available = can_reseller_add_debt(reseller, 0)
        if not reserve_credit(OWNER_ID, order_id, quote["wholesale"], available):
            bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "credit_unavailable"),
                                      show_alert=True)
            return
        exchange_rate = float(settings.get("exchange_rate", 1) or 1)
        toman_price = quote["retail"] * exchange_rate
        record.update({"status": "waiting_receipt", "reservation_id": order_id,
                       "exchange_rate": exchange_rate, "converted_amount": toman_price,
                       "converted_currency": "TOMAN"})
        _save_payment(order_id, record)
        INPUT_STATE[call.from_user.id] = {"kind": "receipt", "payment_id": order_id}
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(get_button_text(_language(call.from_user.id), "cancel"),
                                              callback_data=f"hb:cancel:{order_id}"))
        bot.edit_message_text(
            _message(call.from_user.id, "card_to_card_payment").format(
                price=format_toman_amount(toman_price),
                exchange_rate=format_toman_amount(exchange_rate),
                card_number=settings["card_number"],
            ), call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup,
        )
        return
    if not settings.get("crypto_enabled") or not quote["crypto_supported"]:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "crypto_disabled"), show_alert=True)
        return
    response = CryptoPayment().create_payment(quote["crypto_collected"], plan_id, call.from_user.id,
                                               additional_data={"reseller_id": str(OWNER_ID), "hosted_order_id": order_id})
    if "error" in response:
        bot.answer_callback_query(
            call.id, _message(call.from_user.id, "error_creating_payment").format(error=response["error"]),
            show_alert=True,
        )
        return
    gateway = response.get("result", {})
    gateway_id, url = gateway.get("uuid"), gateway.get("url")
    if not gateway_id or not url:
        bot.answer_callback_query(
            call.id, _message(call.from_user.id, "error_creating_payment").format(error="Invalid gateway response"),
            show_alert=True,
        )
        return
    record.update({"status": "pending", "gateway_payment_id": gateway_id,
                   "crypto_collected": quote["crypto_collected"], "margin": quote["crypto_margin"]})
    _save_payment(order_id, record)
    markup = types.InlineKeyboardMarkup()
    language = _language(call.from_user.id)
    markup.add(types.InlineKeyboardButton(get_button_text(language, "payment_link"), url=url),
               types.InlineKeyboardButton(get_button_text(language, "check_status"),
                                          callback_data=f"hb:check:{order_id}"))
    caption = _message(call.from_user.id, "payment_instructions").format(
        price=format_usd_amount(quote["crypto_collected"]), payment_url=url, payment_id=gateway_id
    )
    caption += "\n\n" + _message(call.from_user.id, "crypto_discount_summary").format(
        percent=5,
        original_price=format_usd_amount(quote["retail"]),
        discount_amount=format_usd_amount(quote["retail"] - quote["crypto_collected"]),
        discounted_price=format_usd_amount(quote["crypto_collected"]),
    )
    caption += _message(call.from_user.id, "purchase_connection_warning")
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.answer_callback_query(call.id)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_photo(call.message.chat.id, image, caption=caption, parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:cancel:"))
def cancel_card_payment(call):
    payment_id = call.data.split(":", 2)[2]
    record = _tenant_payments().get(payment_id)
    if (not record or str(record.get("user_id")) != str(call.from_user.id)
            or record.get("status") not in {"waiting_receipt", "pending_approval"}):
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "already_processed"),
                                  show_alert=True)
        return
    release_credit(OWNER_ID, payment_id, kind="credit_canceled")
    _save_payment(payment_id, {"status": "canceled"})
    INPUT_STATE.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(_message(call.from_user.id, "purchase_canceled"),
                          call.message.chat.id, call.message.message_id)


@bot.message_handler(content_types=["photo"])
def receipt_photo(message):
    state = INPUT_STATE.get(message.from_user.id)
    if not state or state.get("kind") != "receipt":
        return
    payment_id = state["payment_id"]
    record = _tenant_payments().get(payment_id)
    if not record or record.get("status") != "waiting_receipt":
        INPUT_STATE.pop(message.from_user.id, None)
        return
    try:
        info = bot.get_file(message.photo[-1].file_id)
        content = bot.download_file(info.file_path)
        receipt_path = tenant_file(OWNER_ID, os.path.join("receipts", f"{payment_id}.jpg"))
        os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
        with open(receipt_path, "wb") as handle:
            handle.write(content)
        _save_payment(payment_id, {"status": "pending_approval", "receipt_path": receipt_path})
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(_hosted_message(OWNER_ID, "approved"),
                                              callback_data=f"hb:approve:{payment_id}"),
                   types.InlineKeyboardButton(_hosted_message(OWNER_ID, "rejected"),
                                              callback_data=f"hb:reject:{payment_id}"))
        caption = _hosted_message(
            OWNER_ID, "receipt_owner_caption", user_id=message.from_user.id,
            plan_gb=record["plan_gb"], days=record["days"],
            toman_price=format_toman_amount(record.get("converted_amount", record["retail_price"])),
        )
        with open(receipt_path, "rb") as handle:
            bot.send_photo(OWNER_ID, handle, caption=caption, parse_mode="Markdown", reply_markup=markup)
        bot.reply_to(message, _message(message.from_user.id, "receipt_submitted"))
    finally:
        INPUT_STATE.pop(message.from_user.id, None)


@bot.callback_query_handler(func=lambda c: c.data.startswith(("hb:approve:", "hb:reject:")))
def owner_receipt(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "owner_panel"), show_alert=True)
        return
    action, payment_id = call.data.split(":")[1:]
    record = _claim_payment(payment_id, {"pending_approval"})
    if not record:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "already_processed"), show_alert=True)
        return
    if action == "reject":
        release_credit(OWNER_ID, payment_id)
        _save_payment(payment_id, {"status": "rejected"})
        bot.send_message(record["user_id"], _hosted_message(record["user_id"], "receipt_rejected"))
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "rejected"))
        return
    success, result = _provision_payment(payment_id, record, funded=False)
    if not success:
        _save_payment(payment_id, {"status": "pending_approval", "last_error": result})
        bot.answer_callback_query(call.id, result, show_alert=True)
        return
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "approved"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:check:"))
def check_crypto(call):
    payment_id = call.data.split(":")[2]
    current = _tenant_payments().get(payment_id)
    if not current or str(current.get("user_id")) != str(call.from_user.id):
        bot.answer_callback_query(call.id, _message(call.from_user.id, "payment_record_not_found"), show_alert=True)
        return
    if current.get("status") == "completed":
        bot.answer_callback_query(
            call.id, _message(call.from_user.id, "payment_already_processed").format(status="completed"),
            show_alert=True,
        )
        return
    response = CryptoPayment().check_payment_status(current["gateway_payment_id"])
    result = response.get("result", {}) if isinstance(response, dict) else {}
    status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
    if str(status).lower() != "paid":
        bot.answer_callback_query(
            call.id,
            (_message(call.from_user.id, "payment_pending") if not status else
             _message(call.from_user.id, "payment_status").format(status=status)),
            show_alert=True,
        )
        return
    record = _claim_payment(payment_id, {"pending", "paid_provision_failed"})
    if not record:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "payment_processing"), show_alert=True)
        return
    success, detail = _provision_payment(payment_id, record, funded=True)
    if not success:
        _save_payment(payment_id, {"status": "paid_provision_failed", "last_error": detail})
        bot.send_message(OWNER_ID, f"⚠️ Paid order `{payment_id}` needs retry: {detail}", parse_mode="Markdown")
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "paid_needs_attention"),
                                  show_alert=True)
        return
    bot.answer_callback_query(call.id, _message(call.from_user.id, "payment_status").format(status="completed"),
                              show_alert=True)


@bot.message_handler(func=lambda m: m.text in _all_button_values("my_configs", "📱 My Configs"))
def my_configs(message):
    configs = _find_customer_configs(message.from_user.id)
    if not configs:
        bot.reply_to(message, "You have no configs in this bot.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, config in enumerate(configs):
        markup.add(types.InlineKeyboardButton(config.get("username", f"Config {index + 1}"), callback_data=f"hb:cfg:{index}"))
    bot.reply_to(message, "Your configs:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:cfg:"))
def config_detail(call):
    configs = _find_customer_configs(call.from_user.id)
    try:
        config = configs[int(call.data.split(":")[2])]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Config not found", show_alert=True)
        return
    client, live = MultiServerAPI().find_user(config.get("username"), preferred_server_id=config.get("server_id"))
    if not client or not live:
        bot.answer_callback_query(call.id, "Config unavailable", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _deliver_config(call.message.chat.id, config["username"], client)
    if int(live.get("expiration_days", 0) or 0) <= 0:
        plan_id = str(config.get("plan_gb") or config.get("gb") or "")
        if plan_id in _sellable_plans():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Renew", callback_data=f"hb:renew:{plan_id}:{config['username']}:{config.get('server_id') or '-'}"))
            bot.send_message(call.message.chat.id, "This config is eligible for renewal.", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:renew:"))
def renew(call):
    _, _, plan_id, username, server_id = call.data.split(":", 4)
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, plan_id,
                      {"username": username, "server_id": None if server_id == "-" else server_id})


@bot.message_handler(func=lambda m: m.text in _all_button_values("support", "📞 Support"))
def support(message):
    bot.reply_to(message, get_settings(OWNER_ID).get("support_text") or
                 _hosted_message(message.from_user.id, "support_default"))


@bot.message_handler(func=lambda m: m.text in _all_button_values("language", "🌐 Language/زبان"))
def language(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(name, callback_data=f"hb:lang:{code}") for code, name in LANGUAGES.items()])
    bot.reply_to(message, "Select language:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:lang:"))
def language_set(call):
    code = call.data.split(":")[2]
    if code in LANGUAGES:
        _set_language(call.from_user.id, code)
    bot.answer_callback_query(call.id, "Language updated", show_alert=True)
    bot.send_message(call.message.chat.id, "Menu updated.", reply_markup=_main_markup(call.from_user.id))


DOWNLOADS = {
    "iOS": "https://apps.apple.com/ca/app/karing/id6472431552",
    "Android": "https://github.com/2dust/v2rayNG/releases",
    "Windows": "https://github.com/2dust/v2rayN/releases",
}


@bot.message_handler(func=lambda m: m.text in _all_button_values("downloads", "⬇️ Downloads"))
def downloads(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for name, url in DOWNLOADS.items():
        markup.add(types.InlineKeyboardButton(name, url=url))
    bot.reply_to(message, "Download a compatible client:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text in _all_button_values("test_config", "🎁 Test Config"))
def free_test(message):
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        key = str(message.from_user.id)
        if key in tests:
            bot.reply_to(message, "You have already used a free test on this infrastructure.")
            return
        tests[key] = {"telegram_id": message.from_user.id, "creation_pending_at": _now(),
                      "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"), "reseller_id": str(OWNER_ID)}
    plan = {"gb": 1, "days": 30, "unlimited": False}
    username, result, client = _create_user(message.from_user.id, plan, f"hosted test {OWNER_ID}")
    if result is None:
        with locked_json(GLOBAL_TEST_FILE, {}) as tests:
            tests.pop(str(message.from_user.id), None)
        bot.reply_to(message, "Test creation failed. Please try again later.")
        return
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        tests[str(message.from_user.id)].update({"username": username, "server_id": getattr(client, "server_id", None),
                                                 "used_at": _now(), "creation_pending_at": None})
    _deliver_config(message.chat.id, username, client)


@bot.message_handler(func=lambda m: m.text in _all_button_values("referral", "💰 Earn Crypto"))
def referral(message):
    data = _referral_data()
    code = _ensure_referral_code(message.from_user.id)
    stats = data.get("stats", {}).get(str(message.from_user.id), {})
    wallet = data.get("wallets", {}).get(str(message.from_user.id))
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Set wallet", callback_data="hb:refwallet"),
               types.InlineKeyboardButton("Withdraw", callback_data="hb:refwithdraw"))
    bot.reply_to(message,
                 f"Invites: {stats.get('count', 0)}\nEarned: ${float(stats.get('total_earnings', 0)):.2f}\n"
                 f"Available: ${float(stats.get('available_balance', 0)):.2f}\nWallet: {wallet or 'not set'}\n"
                 f"Link: https://t.me/{BOT_USERNAME}?start={code}", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data in {"hb:refwallet", "hb:refwithdraw"})
def referral_action(call):
    if call.data == "hb:refwallet":
        INPUT_STATE[call.from_user.id] = {"kind": "referral_wallet"}
        bot.send_message(call.message.chat.id, "Send your payout wallet/destination.")
        bot.answer_callback_query(call.id)
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        stats = data.get("stats", {}).get(str(call.from_user.id), {})
        amount = round(float(stats.get("available_balance", 0)), 2)
        wallet = data.get("wallets", {}).get(str(call.from_user.id))
        if amount < 2 or not wallet:
            bot.answer_callback_query(call.id, "Minimum $2 and a wallet are required.", show_alert=True)
            return
        request = {"id": str(uuid.uuid4()), "user_id": str(call.from_user.id), "amount": amount,
                   "wallet": wallet, "status": "pending", "requested_at": _now()}
        data.setdefault("pending_withdrawals", []).append(request)
        stats["available_balance"] = 0.0
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Mark Paid", callback_data=f"hb:refresolve:paid:{request['id']}"),
               types.InlineKeyboardButton("❌ Reject", callback_data=f"hb:refresolve:rejected:{request['id']}"))
    bot.send_message(OWNER_ID, f"Referral payout ${amount:.2f}\nUser: {call.from_user.id}\nWallet: `{wallet}`",
                     parse_mode="Markdown", reply_markup=markup)
    bot.answer_callback_query(call.id, "Withdrawal requested", show_alert=True)


@bot.message_handler(func=lambda m: m.from_user.id in INPUT_STATE and INPUT_STATE[m.from_user.id].get("kind") == "referral_wallet")
def referral_wallet_input(message):
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        data.setdefault("wallets", {})[str(message.from_user.id)] = (message.text or "").strip()
    INPUT_STATE.pop(message.from_user.id, None)
    bot.reply_to(message, "Wallet saved.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:refresolve:"))
def referral_resolve(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    _, _, action, request_id = call.data.split(":")
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        request = next((item for item in data.get("pending_withdrawals", []) if item.get("id") == request_id), None)
        if not request or request.get("status") != "pending":
            bot.answer_callback_query(call.id, "Already processed", show_alert=True)
            return
        request["status"] = action
        request["resolved_at"] = _now()
        if action == "rejected":
            stats = data.setdefault("stats", {}).setdefault(request["user_id"], {})
            stats["available_balance"] = round(float(stats.get("available_balance", 0)) + float(request["amount"]), 2)
        else:
            data.setdefault("payouts", []).append(dict(request))
            settle_referral_liability(OWNER_ID, request_id, request["amount"])
    bot.send_message(int(request["user_id"]),
                     _hosted_message(int(request["user_id"]), "referral_withdrawal_result", action=action))
    bot.answer_callback_query(call.id, "Updated", show_alert=True)


OWNER_MENU_ROWS = (
    ("generate", "customers"),
    ("debt", "markup"),
    ("card", "rate"),
    ("support", "welcome"),
    ("refpercent", "plans"),
    ("crypto", "earnings"),
    ("referrals", "back"),
)
OWNER_SETTING_KEYS = ("markup", "card", "rate", "support", "welcome", "refpercent")


def _owner_menu_command(text):
    for row in OWNER_MENU_ROWS:
        for key in row:
            if key == "back" and text in _all_button_values("back", "🔙 Back"):
                return "back", None
            if any(catalog.get(key) == text for catalog in HOSTED_TRANSLATIONS.values()):
                return ("setting" if key in OWNER_SETTING_KEYS else "action"), key
    return None


def _owner_menu_text(user_id, key):
    if key == "back":
        return _button(user_id, "back", "🔙 Back")
    return _hosted_message(user_id, key)


def _owner_markup(user_id=OWNER_ID):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in OWNER_MENU_ROWS:
        markup.row(*(_owner_menu_text(user_id, key) for key in row))
    return markup


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and m.text in {
    catalog["owner_panel"] for catalog in HOSTED_TRANSLATIONS.values()
})
def owner_panel(message):
    settings = get_settings(OWNER_ID)
    summary = _hosted_message(
        OWNER_ID, "owner_summary", markup=settings["markup_percent"],
        crypto=_hosted_message(OWNER_ID, "enabled" if settings["crypto_enabled"] else "disabled"),
        card=settings["card_number"] or _hosted_message(OWNER_ID, "not_set"),
        referral=settings["referral_margin_percent"],
    )
    bot.reply_to(message, f"{_hosted_message(OWNER_ID, 'owner_title')}\n\n{summary}"
                          f"{_hosted_message(OWNER_ID, 'owner_guide')}",
                 parse_mode="Markdown", reply_markup=_owner_markup())


def _begin_owner_setting(chat_id, field):
    INPUT_STATE[OWNER_ID] = {"kind": "owner_setting", "field": field}
    bot.send_message(chat_id, _hosted_message(OWNER_ID, f"prompt_{field}"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:setting:"))
def owner_setting(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    field = call.data.split(":")[2]
    _begin_owner_setting(call.message.chat.id, field)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and _owner_menu_command(m.text) is not None)
def owner_menu_action(message):
    command, action = _owner_menu_command(message.text)
    INPUT_STATE.pop(OWNER_ID, None)
    if command == "back":
        settings = get_settings(OWNER_ID)
        bot.reply_to(message, settings.get("welcome_text") or "Welcome!",
                     reply_markup=_main_markup(OWNER_ID))
        return
    if command == "setting":
        _begin_owner_setting(message.chat.id, action)
        return
    _handle_owner_action(message.chat.id, action, lambda text: bot.reply_to(message, text))


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and INPUT_STATE.get(OWNER_ID, {}).get("kind") == "owner_setting")
def owner_setting_input(message):
    field = INPUT_STATE.pop(OWNER_ID)["field"]
    raw = (message.text or "").strip()
    try:
        if field in {"markup", "rate", "refpercent"}:
            value = float(raw)
            if value < 0 or (field == "refpercent" and value > 100) or (field == "rate" and value <= 0):
                raise ValueError
        else:
            value = raw
        key = {"markup": "markup_percent", "card": "card_number", "rate": "exchange_rate",
               "support": "support_text", "welcome": "welcome_text", "refpercent": "referral_margin_percent"}[field]
        update_settings(OWNER_ID, {key: value})
        bot.reply_to(message, _hosted_message(OWNER_ID, "setting_updated"))
    except ValueError:
        bot.reply_to(message, _hosted_message(OWNER_ID, "invalid_value"))


def _handle_owner_action(chat_id, action, feedback):
    settings = get_settings(OWNER_ID)
    if action == "generate":
        if not _reseller(active_only=True):
            feedback("Generation is unavailable while the reseller is suspended.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
            markup.add(types.InlineKeyboardButton(
                f"{plan_id} GB · {plan.get('days', 30)} days · ${float(plan['price']):.2f} wholesale",
                callback_data=f"hb:ogen:{plan_id}",
            ))
        bot.send_message(chat_id, "Select a wholesale plan:", reply_markup=markup)
        return
    if action == "customers":
        reseller = get_reseller_data(OWNER_ID) or {}
        configs = [item for item in reseller.get("configs", []) if isinstance(item, dict) and not item.get("removed_from_vpn")]
        lines = ["Your provisioned customers:", ""]
        for item in configs[-30:]:
            label = item.get("customer_name") or item.get("customer_telegram_username") or item.get("customer_telegram_id") or "manual"
            lines.append(f"• `{item.get('username', '?')}` · {label} · {item.get('plan_gb', item.get('gb', '?'))} GB")
        bot.send_message(chat_id, "\n".join(lines) if configs else "No reseller customers yet.", parse_mode="Markdown")
        return
    if action == "debt":
        reseller = get_reseller_data(OWNER_ID) or {}
        total_paid = get_reseller_total_paid(reseller)
        limit = get_reseller_trust_limit(total_paid)
        _, _, available = can_reseller_add_debt(reseller, 0)
        bot.send_message(chat_id,
                         f"Debt: ${float(reseller.get('debt', 0)):.2f}\nTrust limit: ${limit:.2f}\nAvailable credit: ${available:.2f}")
        return
    if action == "crypto":
        target = not settings.get("crypto_enabled")
        if target:
            if not os.getenv("CRYPTO_MERCHANT_ID") or not os.getenv("CRYPTO_API_KEY"):
                feedback("Operator crypto gateway is not configured.")
                return
            unsupported = [pid for pid, plan in _sellable_plans().items()
                           if not calculate_quote(plan["price"], settings["markup_percent"])["crypto_supported"]]
            if unsupported:
                feedback("Increase markup before enabling crypto.")
                return
        update_settings(OWNER_ID, {"crypto_enabled": target})
        feedback(f"Crypto {'enabled' if target else 'disabled'}")
        return
    if action == "plans":
        enabled = {str(item) for item in settings.get("enabled_plan_ids", [])}
        markup = types.InlineKeyboardMarkup(row_width=1)
        for plan_id, plan in _load_plans().items():
            if plan.get("target", "both") == "customer":
                continue
            selected = plan_id in enabled if settings.get("plan_selection_configured") else True
            markup.add(types.InlineKeyboardButton(f"{'✅' if selected else '❌'} {plan_id} GB",
                                                   callback_data=f"hb:plantoggle:{plan_id}"))
        bot.send_message(chat_id, "Select plans sold by this bot:", reply_markup=markup)
        return
    if action == "earnings":
        ledger = get_ledger(OWNER_ID)
        reseller = get_reseller_data(OWNER_ID) or {}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("Apply earnings to debt", callback_data="hb:earn:settle"),
                   types.InlineKeyboardButton("Request withdrawal", callback_data="hb:earn:withdraw"))
        bot.send_message(chat_id,
                         f"Available earnings: ${ledger['earnings_available']:.2f}\nReserved: ${ledger['earnings_reserved']:.2f}\n"
                         f"Debt: ${float(reseller.get('debt', 0)):.2f}\nReferral liability: ${ledger['referral_liability']:.2f}",
                         reply_markup=markup)
        return
    if action == "referrals":
        pending = [item for item in _referral_data().get("pending_withdrawals", []) if item.get("status") == "pending"]
        feedback(f"{len(pending)} pending referral payouts")
        for request in pending:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Mark Paid", callback_data=f"hb:refresolve:paid:{request['id']}"),
                       types.InlineKeyboardButton("❌ Reject", callback_data=f"hb:refresolve:rejected:{request['id']}"))
            bot.send_message(chat_id,
                             f"User: `{request['user_id']}`\nAmount: ${request['amount']:.2f}\nWallet: `{request['wallet']}`",
                             parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:owner:"))
def owner_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    callback_answered = False

    def feedback(text):
        nonlocal callback_answered
        bot.answer_callback_query(call.id, text, show_alert=True)
        callback_answered = True

    _handle_owner_action(call.message.chat.id, call.data.split(":")[2], feedback)
    if not callback_answered:
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:ogen:"))
def owner_generate_plan(call):
    if call.from_user.id != OWNER_ID or not _reseller(active_only=True):
        bot.answer_callback_query(call.id, "Owner generation is unavailable.", show_alert=True)
        return
    plan_id = call.data.split(":")[2]
    if plan_id not in _sellable_plans():
        bot.answer_callback_query(call.id, "Plan unavailable.", show_alert=True)
        return
    INPUT_STATE[OWNER_ID] = {"kind": "owner_generate", "plan_id": plan_id}
    bot.send_message(call.message.chat.id, "Send a short customer name/label.")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and INPUT_STATE.get(OWNER_ID, {}).get("kind") == "owner_generate")
def owner_generate_input(message):
    state = INPUT_STATE.pop(OWNER_ID)
    plan_id = state["plan_id"]
    plan = _sellable_plans().get(plan_id)
    label = (message.text or "customer").strip()[:64]
    reseller = get_reseller_data(OWNER_ID) or {}
    _, _, available = can_reseller_add_debt(reseller, 0)
    reservation_id = f"manual-{uuid.uuid4()}"
    if not plan or not reserve_credit(OWNER_ID, reservation_id, plan["price"], available):
        bot.reply_to(message, "Insufficient reseller credit or unavailable plan.")
        return
    username, result, client = _create_user(OWNER_ID, {"gb": plan_id, "days": plan.get("days", 30),
                                                       "unlimited": plan.get("unlimited", False)}, label)
    if result is None:
        release_credit(OWNER_ID, reservation_id)
        bot.reply_to(message, "VPN user creation failed.")
        return
    config = {"username": username, "customer_name": label, "server_id": getattr(client, "server_id", None),
              "reseller_id": str(OWNER_ID), "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
              "plan_gb": plan_id, "days": plan.get("days", 30), "price": float(plan["price"]),
              "retail_order_id": reservation_id}
    if not consume_credit(OWNER_ID, reservation_id, config):
        client.delete_user(username)
        bot.reply_to(message, "Accounting failed; the VPN user was rolled back.")
        return
    _deliver_config(message.chat.id, username, client)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:plantoggle:"))
def plan_toggle(call):
    if call.from_user.id != OWNER_ID:
        return
    plan_id = call.data.split(":")[2]
    settings = get_settings(OWNER_ID)
    all_ids = {pid for pid, plan in _load_plans().items() if plan.get("target", "both") != "customer"}
    enabled = set(settings.get("enabled_plan_ids", [])) if settings.get("plan_selection_configured") else set(all_ids)
    enabled.symmetric_difference_update({plan_id})
    update_settings(OWNER_ID, {"enabled_plan_ids": sorted(enabled), "plan_selection_configured": True})
    bot.answer_callback_query(call.id, "Updated", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:earn:"))
def earnings_action(call):
    if call.from_user.id != OWNER_ID:
        return
    action = call.data.split(":")[2]
    if action == "settle":
        success, result = transfer_earnings_to_debt(OWNER_ID)
        bot.answer_callback_query(call.id, f"Applied ${result['amount']:.2f}" if success else str(result), show_alert=True)
        return
    INPUT_STATE[OWNER_ID] = {"kind": "earnings_destination"}
    bot.send_message(call.message.chat.id, "Send the payout wallet/destination for the full available balance.")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and INPUT_STATE.get(OWNER_ID, {}).get("kind") == "earnings_destination")
def earnings_destination(message):
    INPUT_STATE.pop(OWNER_ID, None)
    success, result = request_earnings_withdrawal(OWNER_ID, (message.text or "").strip())
    bot.reply_to(message, f"Withdrawal requested for ${result['amount']:.2f}." if success else str(result))


def _crypto_monitor():
    while True:
        try:
            for payment_id, current in _tenant_payments().items():
                if current.get("status") in {"waiting_receipt", "pending_approval"}:
                    try:
                        created = datetime.strptime(current.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                    except (TypeError, ValueError):
                        created = datetime.now()
                    if datetime.now() - created >= timedelta(hours=24):
                        if release_credit(OWNER_ID, payment_id, kind="credit_expired"):
                            _save_payment(payment_id, {"status": "expired"})
                            try:
                                bot.send_message(current["user_id"], "Your pending card order expired. Start a new purchase if needed.")
                            except Exception:
                                pass
                    continue
                if current.get("status") not in {"pending", "paid_provision_failed"} or not current.get("gateway_payment_id"):
                    continue
                response = CryptoPayment().check_payment_status(current["gateway_payment_id"])
                result = response.get("result", {}) if isinstance(response, dict) else {}
                status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
                if str(status).lower() != "paid":
                    continue
                record = _claim_payment(payment_id, {"pending", "paid_provision_failed"})
                if not record:
                    continue
                success, detail = _provision_payment(payment_id, record, funded=True)
                if not success:
                    _save_payment(payment_id, {"status": "paid_provision_failed", "last_error": detail})
                    bot.send_message(OWNER_ID, f"⚠️ Paid order `{payment_id}` needs retry: {detail}", parse_mode="Markdown")
        except Exception as error:
            print(f"Hosted crypto monitor failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
        time.sleep(300)


def _customer_notification_monitor():
    while True:
        try:
            reseller = get_reseller_data(OWNER_ID) or {}
            with locked_json(tenant_file(OWNER_ID, "notifications.json"), {}) as sent:
                for config in reseller.get("configs", []):
                    if not isinstance(config, dict) or not config.get("customer_telegram_id") or config.get("removed_from_vpn"):
                        continue
                    username = config.get("username")
                    client, live = MultiServerAPI().find_user(username, preferred_server_id=config.get("server_id"))
                    if not client or not live:
                        continue
                    user_id = int(config["customer_telegram_id"])
                    expiration = int(live.get("expiration_days", 0) or 0)
                    cycle = str(config.get("timestamp") or config.get("retail_order_id") or "initial")
                    if expiration <= 0 and sent.get(f"expired:{username}") != cycle:
                        bot.send_message(user_id, f"Your config `{username}` has expired. Open My Configs to renew it.", parse_mode="Markdown")
                        sent[f"expired:{username}"] = cycle
                    maximum = float(live.get("max_download_bytes", 0) or 0)
                    used = float(live.get("upload_bytes", 0) or 0) + float(live.get("download_bytes", 0) or 0)
                    if maximum > 0:
                        percent = int((used / maximum) * 100)
                        threshold = 90 if percent >= 90 else 80 if percent >= 80 else None
                        alert_key = f"traffic:{username}:{cycle}"
                        if threshold and int(sent.get(alert_key, 0) or 0) < threshold:
                            bot.send_message(user_id, f"Config `{username}` has used {percent}% of its traffic quota.", parse_mode="Markdown")
                            sent[alert_key] = threshold
        except Exception as error:
            print(f"Hosted notification monitor failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
        time.sleep(7200)


def run():
    try:
        bot.get_me()
    except Exception as error:
        set_bot_runtime_status(OWNER_ID, "error", f"Telegram authentication failed: {type(error).__name__}")
        raise SystemExit(2)
    set_bot_runtime_status(OWNER_ID, "active")
    threading.Thread(target=_crypto_monitor, daemon=True, name="hosted-crypto").start()
    threading.Thread(target=_customer_notification_monitor, daemon=True, name="hosted-notifications").start()
    retry = 3
    while True:
        try:
            bot.polling(none_stop=False, timeout=25, long_polling_timeout=25, skip_pending=True)
            retry = 3
        except Exception as error:
            set_bot_runtime_status(OWNER_ID, "error", f"Telegram polling failed: {type(error).__name__}")
            print(f"Hosted bot polling failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
            time.sleep(retry)
            retry = min(60, retry * 2)
            set_bot_runtime_status(OWNER_ID, "active")


if __name__ == "__main__":
    run()
