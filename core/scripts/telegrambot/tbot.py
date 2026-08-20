import os

os.environ.setdefault("AJIB_BOT_ROLE", "main")
BOT_DIR = os.getenv("AJIB_BOT_DIR", os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AJIB_BOT_DIR", BOT_DIR)

if __name__ == "__main__":
    from migrate_state import bootstrap_storage

    bootstrap_storage(BOT_DIR)

from telebot import types
from utils import *
import threading
import time
import traceback
import logging
from types import SimpleNamespace
from utils.telegram_safe import safe_reply_to, safe_send_message

EXPIRED_CLEANUP_INTERVAL_SECONDS = 3600

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    admin_user = is_admin(user_id)
    language = None
    if not admin_user:
        language = resolve_user_language(
            user_id,
            getattr(message.from_user, "language_code", None),
        )
    
    # Check for referral
    args = message.text.split()
    if len(args) > 1:
        referral_code = args[1]
        try:
            success, result = process_referral(
                user_id,
                referral_code,
                telegram_username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name
            )
            lang = language or get_user_language(user_id)
            if success:
                record_main_growth_event(
                    "referral_attributed",
                    user_id,
                    language=lang,
                    referral_campaign="main_invite",
                    deduplication_key=f"main:referral_attributed:{user_id}",
                    referrer_id=str(result),
                )
                if language:
                    safe_send_message(bot, user_id, get_message_text(lang, "referral_registered"))
                else:
                    from utils.language import defer_referral_confirmation

                    defer_referral_confirmation(user_id)
        except Exception as e:
            print(f"Error processing referral: {e}")

    if admin_user:
        markup = create_main_markup(is_admin=True)
        safe_reply_to(bot, message, "Welcome to the Admin Dashboard!", reply_markup=markup)
    else:
        if not language:
            safe_reply_to(
                bot,
                message,
                get_message_text("en", "language_selection_prompt"),
                reply_markup=build_language_selection_markup(),
            )
            return

        welcome_text, welcome_markup = build_customer_welcome(user_id, language)
        safe_reply_to(
            bot,
            message,
            welcome_text,
            reply_markup=welcome_markup,
            parse_mode="Markdown",
        )
        safe_send_message(
            bot,
            message.chat.id,
            get_message_text(language, "main_menu_ready"),
            reply_markup=create_main_markup(is_admin=False, user_id=user_id),
        )


@bot.message_handler(
    func=lambda message: (
        is_admin(message.from_user.id)
        and resolve_admin_menu_view(message.text) is not None
    )
)
def handle_admin_menu_navigation(message):
    """Render an admin category keyboard or return to the dashboard root."""
    if not is_admin(message.from_user.id):
        return

    view = resolve_admin_menu_view(message.text)
    if view is None:
        return

    if view == "root":
        reply_text = "Admin dashboard is ready."
    else:
        reply_text = f"{ADMIN_CATEGORIES[view]['text']}\nChoose an action:"

    safe_reply_to(
        bot,
        message,
        reply_text,
        reply_markup=create_admin_markup(view),
    )


@bot.callback_query_handler(func=lambda call: call.data == "welcome:plans")
def handle_welcome_plans(call):
    safe_answer_callback_query(bot, call.id)
    show_plans(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "welcome:configs")
def handle_welcome_configs(call):
    safe_answer_callback_query(bot, call.id)
    proxy_message = SimpleNamespace(
        text=get_button_text(get_user_language(call.from_user.id), "my_configs"),
        from_user=call.from_user,
        chat=call.message.chat,
    )
    my_configs(proxy_message)


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == "❌ Cancel")
def handle_admin_cancel_fallback(message):
    safe_reply_to(
        bot,
        message,
        "Operation canceled.",
        reply_markup=create_main_markup(is_admin=True),
    )


@bot.message_handler(
    func=lambda message: (
        is_admin(message.from_user.id)
        and message.text == GROWTH_FUNNEL_BUTTON_TEXT
    )
)
def show_admin_growth_funnel(message):
    """Render a private aggregate 30-day funnel comparison for administrators."""
    if not is_admin(message.from_user.id):
        return
    try:
        # Keep reporting optional at import time so a damaged analytics store
        # cannot prevent the primary bot and its customer handlers from starting.
        from utils.growth_reporting import (
            format_growth_comparison,
            main_growth_comparison,
        )

        report = main_growth_comparison(days=30)
        text = format_growth_comparison(
            report,
            title="Main bot growth funnel",
        )
    except Exception as error:
        logging.getLogger("ajib.growth_reporting").exception(
            "Error building admin growth funnel: %s",
            error,
        )
        safe_reply_to(
            bot,
            message,
            "Growth funnel data is temporarily unavailable.",
            reply_markup=create_main_markup(is_admin=True),
        )
        return

    safe_reply_to(
        bot,
        message,
        text,
        reply_markup=create_main_markup(is_admin=True),
        parse_mode="Markdown",
    )


def monitoring_thread():
    while True:
        monitor_system_resources()
        time.sleep(60)

def payment_monitoring_thread():
    """Background thread to check pending payments periodically"""
    while True:
        try:
            from utils.purchase_plan import check_pending_payments
            check_pending_payments()
        except Exception:
            logging.getLogger("ajib.payments").exception("Error in payment monitoring")
        # Check every 5 minutes
        time.sleep(300)

def expired_cleanup_monitoring_thread():
    """Background thread to run expired user cleanup on its own cadence"""
    while True:
        try:
            from utils.expired_cleanup import (
                EXPIRED_CLEANUP_GRACE_HOURS,
                get_expired_cleanup_startup_delay,
                run_expired_user_cleanup_with_metadata,
            )
            delay_seconds = get_expired_cleanup_startup_delay(
                interval_seconds=EXPIRED_CLEANUP_INTERVAL_SECONDS
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)
                continue
            run_expired_user_cleanup_with_metadata(grace_hours=EXPIRED_CLEANUP_GRACE_HOURS)
        except Exception as e:
            print(f"Error in expired cleanup: {e}")
        time.sleep(EXPIRED_CLEANUP_INTERVAL_SECONDS)

def traffic_monitoring_thread():
    """Background thread to notify users when nearing traffic quota"""
    while True:
        try:
            monitor_user_traffic()
        except Exception as e:
            print(f"Error in traffic monitoring: {e}")
        # Check every 2 hours
        time.sleep(7200)

def automated_backup_thread():
    """Background thread to run automated backups every 3 hours"""
    while True:
        try:
            run_backup_and_send_to_admins()
        except Exception as e:
            print(f"Error in automated backup: {e}")
        # Run every 3 hours
        time.sleep(10800)


def bulk_transfer_monitoring_thread():
    """Resume persisted transfers and deliver main-bot migration notices."""
    from utils.bulk_transfer import (
        deliver_notifications,
        recover_stale_notification_claims,
        start_transfer_worker,
    )

    recover_stale_notification_claims()
    while True:
        try:
            start_transfer_worker()
            deliver_notifications(
                "main",
                lambda recipient_id, text: safe_send_message(bot, recipient_id, text),
                language_resolver=get_user_language,
            )
        except Exception:
            logging.getLogger("ajib.bulk_transfer").exception(
                "Error in bulk-transfer monitoring"
            )
        time.sleep(30)


def run_polling_forever():
    """Keep polling alive across transient Telegram/network failures."""
    retry_delay_seconds = 3
    max_retry_delay_seconds = 60

    while True:
        try:
            bot.polling(none_stop=True, timeout=25, long_polling_timeout=25)
            retry_delay_seconds = 3
        except Exception as e:
            print(f"Telegram polling crashed: {e}")
            traceback.print_exc()
            time.sleep(retry_delay_seconds)
            retry_delay_seconds = min(max_retry_delay_seconds, retry_delay_seconds * 2)

if __name__ == '__main__':
    monitor_thread = threading.Thread(target=monitoring_thread, daemon=True)
    monitor_thread.start()
    version_thread = threading.Thread(target=version_monitoring, daemon=True)
    version_thread.start()
    payment_thread = threading.Thread(target=payment_monitoring_thread, daemon=True)
    payment_thread.start()
    expired_cleanup_thread = threading.Thread(target=expired_cleanup_monitoring_thread, daemon=True)
    expired_cleanup_thread.start()
    traffic_thread = threading.Thread(target=traffic_monitoring_thread, daemon=True)
    traffic_thread.start()
    backup_thread = threading.Thread(target=automated_backup_thread, daemon=True)
    backup_thread.start()
    bulk_thread = threading.Thread(target=bulk_transfer_monitoring_thread, daemon=True)
    bulk_thread.start()
    run_polling_forever()
