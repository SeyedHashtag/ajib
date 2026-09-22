import logging
import os
from concurrent.futures import ThreadPoolExecutor
import threading

from utils.bot_logging import get_bot_log_file
from utils.command import bot, is_admin
from utils.common import admin_action_text


BOT_LOGS_BUTTON_TEXT = admin_action_text("bot_logs")
BOT_LOGS_LOCK = threading.Lock()
BOT_LOGS_INFLIGHT = set()


def _int_env(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


BOT_LOGS_EXECUTOR = ThreadPoolExecutor(
    max_workers=_int_env("AJIB_BOT_LOG_WORKERS", 1),
    thread_name_prefix="ajib-bot-logs",
)


def _send_bot_log_file(message, log_file):
    try:
        if not _authorized(message):
            return
        with open(log_file, 'rb') as document:
            bot.send_document(message.from_user.id, document,
                              visible_file_name='service.log', caption='Current service log.',
                              _operator_document=True)
    except Exception:
        logging.getLogger(__name__).exception('Administrator log delivery failed')
        if _authorized(message):
            bot.reply_to(message, 'Failed to send logs. Please try again.')
    finally:
        with BOT_LOGS_LOCK:
            BOT_LOGS_INFLIGHT.discard(message.from_user.id)


def _authorized(message):
    return (is_admin(message.from_user.id) and message.chat.id == message.from_user.id
            and getattr(message.chat, 'type', None) == 'private')


def _queue_bot_log_send(message, log_file):
    user_id = message.from_user.id
    with BOT_LOGS_LOCK:
        if user_id in BOT_LOGS_INFLIGHT:
            return False
        BOT_LOGS_INFLIGHT.add(user_id)
    try:
        BOT_LOGS_EXECUTOR.submit(_send_bot_log_file, message, log_file)
    except Exception:
        with BOT_LOGS_LOCK:
            BOT_LOGS_INFLIGHT.discard(user_id)
        raise
    return True


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == BOT_LOGS_BUTTON_TEXT)
def send_bot_logs(message):
    if not _authorized(message):
        return
    log_file = get_bot_log_file()

    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        bot.reply_to(message, "Bot log file is missing or empty.")
        return

    _queue_bot_log_send(message, log_file)
