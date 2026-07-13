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
from utils.hosted_bots import (
    add_referral_liability, calculate_quote, consume_credit,
    consume_renewal_credit, credit_crypto_sale, get_ledger, get_settings,
    release_credit, request_earnings_withdrawal, reserve_credit,
    set_bot_runtime_status, settle_referral_liability, tenant_file, transfer_earnings_to_debt,
    update_settings,
)
from utils.payments import CryptoPayment
from utils.reseller import (
    can_reseller_add_debt, get_reseller_data, get_reseller_total_paid,
    get_reseller_trust_limit, record_funded_reseller_config,
    record_funded_reseller_renewal,
)
from utils.translations import BUTTON_TRANSLATIONS, LANGUAGES
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


def _all_button_values(key, fallback):
    return {items.get(key, fallback) for items in BUTTON_TRANSLATIONS.values()}


def _main_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(_button(user_id, "my_configs", "📱 My Configs"), _button(user_id, "purchase_plan", "💳 Purchase Plan"))
    markup.row(_button(user_id, "downloads", "⬇️ Downloads"), _button(user_id, "test_config", "🎁 Test Config"))
    markup.row(_button(user_id, "referral", "💰 Earn Crypto"), _button(user_id, "support", "📞 Support"))
    markup.row(_button(user_id, "language", "🌐 Language/زبان"))
    if user_id == OWNER_ID:
        markup.row("🛠 Owner Panel")
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
    if not uri or not uri.get("normal_sub"):
        bot.send_message(chat_id, f"✅ {'Renewed' if renewed else 'Created'} `{username}`, but its subscription URL is not available yet.", parse_mode="Markdown")
        return
    url = uri.get("ipv4") or uri["normal_sub"]
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.send_photo(chat_id, image, caption=f"✅ {'Renewed' if renewed else 'Your config is ready'}\nUsername: `{username}`\nSubscription: `{uri['normal_sub']}`", parse_mode="Markdown")


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


def _purchase_options(chat_id, user_id, plan_id, renewal=None):
    reseller = _reseller(active_only=True)
    if not reseller:
        bot.send_message(chat_id, "Purchases are temporarily unavailable.")
        return
    plan = _sellable_plans().get(str(plan_id))
    if not plan:
        bot.send_message(chat_id, "Plan is no longer available.")
        return
    settings = get_settings(OWNER_ID)
    quote = calculate_quote(plan["price"], settings["markup_percent"], settings["referral_margin_percent"],
                            referred=str(user_id) in _referral_data().get("referrals", {}))
    markup = types.InlineKeyboardMarkup(row_width=1)
    suffix = ""
    if renewal:
        token = uuid.uuid4().hex[:12]
        RENEWAL_TOKENS[token] = dict(renewal)
        suffix = f":{token}"
    if settings.get("card_number"):
        markup.add(types.InlineKeyboardButton("📄 Card to Card", callback_data=f"hb:pay:card:{plan_id}{suffix}"))
    if settings.get("crypto_enabled") and quote["crypto_supported"] and os.getenv("CRYPTO_MERCHANT_ID") and os.getenv("CRYPTO_API_KEY"):
        markup.add(types.InlineKeyboardButton("💳 Crypto (5% off)", callback_data=f"hb:pay:crypto:{plan_id}{suffix}"))
    if not markup.keyboard:
        bot.send_message(chat_id, "No payment method is currently available.")
        return
    bot.send_message(chat_id, f"{plan_id} GB · {plan.get('days', 30)} days\nRetail price: ${quote['retail']:.2f}\nSelect payment method:", reply_markup=markup)


@bot.message_handler(commands=["start"])
def start(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        _register_referral(message.from_user.id, parts[1].strip())
    settings = get_settings(OWNER_ID)
    bot.reply_to(message, settings.get("welcome_text") or "Welcome!", reply_markup=_main_markup(message.from_user.id))


@bot.message_handler(func=lambda m: m.text in _all_button_values("purchase_plan", "💳 Purchase Plan"))
def plans(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    settings = get_settings(OWNER_ID)
    for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
        quote = calculate_quote(plan["price"], settings["markup_percent"])
        markup.add(types.InlineKeyboardButton(f"{plan_id} GB · {plan.get('days', 30)} days · ${quote['retail']:.2f}", callback_data=f"hb:buy:{plan_id}"))
    bot.reply_to(message, "Select a plan:" if markup.keyboard else "No plans are enabled.", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:buy:"))
def buy(call):
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, call.data.split(":")[2])


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:pay:"))
def payment_method(call):
    parts = call.data.split(":")
    method, plan_id = parts[2], parts[3]
    renewal = RENEWAL_TOKENS.get(parts[4]) if len(parts) >= 5 else None
    plan = _sellable_plans().get(plan_id)
    settings = get_settings(OWNER_ID)
    if not plan or not _reseller(active_only=True):
        bot.answer_callback_query(call.id, "Plan unavailable", show_alert=True)
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
            bot.answer_callback_query(call.id, "Reseller credit is temporarily unavailable.", show_alert=True)
            return
        record.update({"status": "waiting_receipt", "reservation_id": order_id})
        _save_payment(order_id, record)
        INPUT_STATE[call.from_user.id] = {"kind": "receipt", "payment_id": order_id}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
                         f"Send ${quote['retail']:.2f} using this card and upload the receipt photo:\n`{settings['card_number']}`",
                         parse_mode="Markdown")
        return
    if not settings.get("crypto_enabled") or not quote["crypto_supported"]:
        bot.answer_callback_query(call.id, "Crypto checkout is disabled.", show_alert=True)
        return
    response = CryptoPayment().create_payment(quote["crypto_collected"], plan_id, call.from_user.id,
                                               additional_data={"reseller_id": str(OWNER_ID), "hosted_order_id": order_id})
    if "error" in response:
        bot.answer_callback_query(call.id, response["error"], show_alert=True)
        return
    gateway = response.get("result", {})
    gateway_id, url = gateway.get("uuid"), gateway.get("url")
    if not gateway_id or not url:
        bot.answer_callback_query(call.id, "Invalid gateway response.", show_alert=True)
        return
    record.update({"status": "pending", "gateway_payment_id": gateway_id,
                   "crypto_collected": quote["crypto_collected"], "margin": quote["crypto_margin"]})
    _save_payment(order_id, record)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔗 Pay", url=url),
               types.InlineKeyboardButton("🔄 Check", callback_data=f"hb:check:{order_id}"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"Crypto total after 5% discount: ${quote['crypto_collected']:.2f}", reply_markup=markup)


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
        markup.add(types.InlineKeyboardButton("✅ Approve", callback_data=f"hb:approve:{payment_id}"),
                   types.InlineKeyboardButton("❌ Reject", callback_data=f"hb:reject:{payment_id}"))
        with open(receipt_path, "rb") as handle:
            bot.send_photo(OWNER_ID, handle, caption=f"Receipt from {message.from_user.id}\nRetail: ${record['retail_price']:.2f}", reply_markup=markup)
        bot.reply_to(message, "Receipt submitted to the reseller.")
    finally:
        INPUT_STATE.pop(message.from_user.id, None)


@bot.callback_query_handler(func=lambda c: c.data.startswith(("hb:approve:", "hb:reject:")))
def owner_receipt(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    action, payment_id = call.data.split(":")[1:]
    record = _claim_payment(payment_id, {"pending_approval"})
    if not record:
        bot.answer_callback_query(call.id, "Already processed", show_alert=True)
        return
    if action == "reject":
        release_credit(OWNER_ID, payment_id)
        _save_payment(payment_id, {"status": "rejected"})
        bot.send_message(record["user_id"], "Your receipt was rejected by the reseller.")
        bot.answer_callback_query(call.id, "Rejected")
        return
    success, result = _provision_payment(payment_id, record, funded=False)
    if not success:
        _save_payment(payment_id, {"status": "pending_approval", "last_error": result})
        bot.answer_callback_query(call.id, result, show_alert=True)
        return
    bot.answer_callback_query(call.id, "Approved")


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:check:"))
def check_crypto(call):
    payment_id = call.data.split(":")[2]
    current = _tenant_payments().get(payment_id)
    if not current or str(current.get("user_id")) != str(call.from_user.id):
        bot.answer_callback_query(call.id, "Payment not found", show_alert=True)
        return
    if current.get("status") == "completed":
        bot.answer_callback_query(call.id, "Already completed", show_alert=True)
        return
    response = CryptoPayment().check_payment_status(current["gateway_payment_id"])
    result = response.get("result", {}) if isinstance(response, dict) else {}
    status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
    if str(status).lower() != "paid":
        bot.answer_callback_query(call.id, f"Status: {status or 'unknown'}", show_alert=True)
        return
    record = _claim_payment(payment_id, {"pending", "paid_provision_failed"})
    if not record:
        bot.answer_callback_query(call.id, "Payment is already processing", show_alert=True)
        return
    success, detail = _provision_payment(payment_id, record, funded=True)
    if not success:
        _save_payment(payment_id, {"status": "paid_provision_failed", "last_error": detail})
        bot.send_message(OWNER_ID, f"⚠️ Paid order `{payment_id}` needs retry: {detail}", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Paid, but provisioning needs attention.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Payment completed", show_alert=True)


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
    bot.reply_to(message, get_settings(OWNER_ID).get("support_text") or "Contact the reseller for support.")


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
    bot.send_message(int(request["user_id"]), f"Your referral withdrawal was {action} by the reseller.")
    bot.answer_callback_query(call.id, "Updated", show_alert=True)


def _owner_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Generate config", callback_data="hb:owner:generate"),
               types.InlineKeyboardButton("My customers", callback_data="hb:owner:customers"))
    markup.add(types.InlineKeyboardButton("Debt & credit", callback_data="hb:owner:debt"))
    for label, action in (("Markup", "markup"), ("Card", "card"), ("Exchange rate", "rate"),
                          ("Support", "support"), ("Welcome", "welcome"), ("Referral %", "refpercent")):
        markup.add(types.InlineKeyboardButton(label, callback_data=f"hb:setting:{action}"))
    markup.add(types.InlineKeyboardButton("Toggle plans", callback_data="hb:owner:plans"),
               types.InlineKeyboardButton("Toggle crypto", callback_data="hb:owner:crypto"))
    markup.add(types.InlineKeyboardButton("Earnings", callback_data="hb:owner:earnings"),
               types.InlineKeyboardButton("Referral payouts", callback_data="hb:owner:referrals"))
    return markup


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and m.text == "🛠 Owner Panel")
def owner_panel(message):
    settings = get_settings(OWNER_ID)
    bot.reply_to(message,
                 f"Hosted Bot Owner Panel\nMarkup: {settings['markup_percent']}%\nCrypto: {'enabled' if settings['crypto_enabled'] else 'disabled'}\n"
                 f"Card: {settings['card_number'] or 'not set'}\nReferral share: {settings['referral_margin_percent']}%",
                 reply_markup=_owner_markup())


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:setting:"))
def owner_setting(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    field = call.data.split(":")[2]
    INPUT_STATE[OWNER_ID] = {"kind": "owner_setting", "field": field}
    prompts = {"markup": "Send markup percentage.", "card": "Send card number.", "rate": "Send exchange rate.",
               "support": "Send support text.", "welcome": "Send welcome text.", "refpercent": "Send referral percentage of margin (0-100)."}
    bot.send_message(call.message.chat.id, prompts[field])
    bot.answer_callback_query(call.id)


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
        bot.reply_to(message, "Setting updated.")
    except ValueError:
        bot.reply_to(message, "Invalid value.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:owner:"))
def owner_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    action = call.data.split(":")[2]
    settings = get_settings(OWNER_ID)
    if action == "generate":
        if not _reseller(active_only=True):
            bot.answer_callback_query(call.id, "Generation is unavailable while the reseller is suspended.", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
            markup.add(types.InlineKeyboardButton(
                f"{plan_id} GB · {plan.get('days', 30)} days · ${float(plan['price']):.2f} wholesale",
                callback_data=f"hb:ogen:{plan_id}",
            ))
        bot.send_message(call.message.chat.id, "Select a wholesale plan:", reply_markup=markup)
        bot.answer_callback_query(call.id)
        return
    if action == "customers":
        reseller = get_reseller_data(OWNER_ID) or {}
        configs = [item for item in reseller.get("configs", []) if isinstance(item, dict) and not item.get("removed_from_vpn")]
        lines = ["Your provisioned customers:", ""]
        for item in configs[-30:]:
            label = item.get("customer_name") or item.get("customer_telegram_username") or item.get("customer_telegram_id") or "manual"
            lines.append(f"• `{item.get('username', '?')}` · {label} · {item.get('plan_gb', item.get('gb', '?'))} GB")
        bot.send_message(call.message.chat.id, "\n".join(lines) if configs else "No reseller customers yet.", parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return
    if action == "debt":
        reseller = get_reseller_data(OWNER_ID) or {}
        total_paid = get_reseller_total_paid(reseller)
        limit = get_reseller_trust_limit(total_paid)
        _, _, available = can_reseller_add_debt(reseller, 0)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
                         f"Debt: ${float(reseller.get('debt', 0)):.2f}\nTrust limit: ${limit:.2f}\nAvailable credit: ${available:.2f}")
        return
    if action == "crypto":
        target = not settings.get("crypto_enabled")
        if target:
            if not os.getenv("CRYPTO_MERCHANT_ID") or not os.getenv("CRYPTO_API_KEY"):
                bot.answer_callback_query(call.id, "Operator crypto gateway is not configured.", show_alert=True)
                return
            unsupported = [pid for pid, plan in _sellable_plans().items()
                           if not calculate_quote(plan["price"], settings["markup_percent"])["crypto_supported"]]
            if unsupported:
                bot.answer_callback_query(call.id, "Increase markup before enabling crypto.", show_alert=True)
                return
        update_settings(OWNER_ID, {"crypto_enabled": target})
        bot.answer_callback_query(call.id, f"Crypto {'enabled' if target else 'disabled'}", show_alert=True)
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
        bot.send_message(call.message.chat.id, "Select plans sold by this bot:", reply_markup=markup)
        bot.answer_callback_query(call.id)
        return
    if action == "earnings":
        ledger = get_ledger(OWNER_ID)
        reseller = get_reseller_data(OWNER_ID) or {}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("Apply earnings to debt", callback_data="hb:earn:settle"),
                   types.InlineKeyboardButton("Request withdrawal", callback_data="hb:earn:withdraw"))
        bot.send_message(call.message.chat.id,
                         f"Available earnings: ${ledger['earnings_available']:.2f}\nReserved: ${ledger['earnings_reserved']:.2f}\n"
                         f"Debt: ${float(reseller.get('debt', 0)):.2f}\nReferral liability: ${ledger['referral_liability']:.2f}",
                         reply_markup=markup)
        bot.answer_callback_query(call.id)
        return
    if action == "referrals":
        pending = [item for item in _referral_data().get("pending_withdrawals", []) if item.get("status") == "pending"]
        bot.answer_callback_query(call.id, f"{len(pending)} pending referral payouts", show_alert=True)
        for request in pending:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Mark Paid", callback_data=f"hb:refresolve:paid:{request['id']}"),
                       types.InlineKeyboardButton("❌ Reject", callback_data=f"hb:refresolve:rejected:{request['id']}"))
            bot.send_message(call.message.chat.id,
                             f"User: `{request['user_id']}`\nAmount: ${request['amount']:.2f}\nWallet: `{request['wallet']}`",
                             parse_mode="Markdown", reply_markup=markup)


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
