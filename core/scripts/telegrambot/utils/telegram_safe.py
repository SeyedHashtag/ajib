import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps


DEFAULT_TELEGRAM_TIMEOUT_SECONDS = 5
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 3
DEFAULT_CALLBACK_WORKERS = 4
DEFAULT_POLL_RETRY_SECONDS = 3
MAX_POLL_RETRY_SECONDS = 60
STABLE_POLLING_SECONDS = 300
EXPECTED_TELEGRAM_ERROR_MARKERS = (
    "query is too old",
    "response timeout expired",
    "query id is invalid",
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
)
PARSE_ENTITY_ERROR_MARKERS = (
    "can't parse entities",
    "can't find end of the entity",
    "can't find end of entity",
)
PERMANENT_RECIPIENT_ERROR_MARKERS = (
    "blocked",
    "deactivated",
    "chat not found",
    "user is bot",
    "forbidden",
)
TRANSIENT_TRANSPORT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection error",
    "connection aborted",
    "connection reset",
    "remote disconnected",
    "temporarily unavailable",
    "bad gateway",
)


def _int_env(name, default, minimum=1):
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def get_telegram_timeout_seconds():
    return _int_env("AJIB_TELEGRAM_TIMEOUT_SECONDS", DEFAULT_TELEGRAM_TIMEOUT_SECONDS)


def get_callback_timeout_seconds():
    return _int_env("AJIB_CALLBACK_TIMEOUT_SECONDS", DEFAULT_CALLBACK_TIMEOUT_SECONDS)


def get_callback_worker_count():
    return _int_env("AJIB_CALLBACK_WORKERS", DEFAULT_CALLBACK_WORKERS)


CALLBACK_ANSWER_EXECUTOR = ThreadPoolExecutor(
    max_workers=get_callback_worker_count(),
    thread_name_prefix="ajib-callback-answer",
)


def is_expected_telegram_error(error):
    text = str(error).lower()
    return any(marker in text for marker in EXPECTED_TELEGRAM_ERROR_MARKERS)


def is_parse_entity_error(error):
    text = str(error).lower()
    code = telegram_error_code(error)
    return code in (None, 400) and any(marker in text for marker in PARSE_ENTITY_ERROR_MARKERS)


def telegram_error_code(error):
    value = getattr(error, "error_code", None)
    if value is None:
        result = getattr(error, "result_json", None)
        value = result.get("error_code") if isinstance(result, dict) else None
    if value is None:
        match = re.search(r"\berror code\s*:\s*(\d{3})\b", str(error), re.IGNORECASE)
        value = match.group(1) if match else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_telegram_delivery_error(error):
    """Classify a failed Telegram delivery without coupling callers to SDK types."""

    text = str(error).lower()
    code = telegram_error_code(error)
    if any(marker in text for marker in PERMANENT_RECIPIENT_ERROR_MARKERS):
        return "permanent_recipient"
    if code == 429 or "too many requests" in text or "retry after" in text:
        return "rate_limited"
    if code is not None and 500 <= code <= 599:
        return "transient_transport"
    if any(marker in text for marker in TRANSIENT_TRANSPORT_ERROR_MARKERS):
        return "transient_transport"
    if code == 400:
        return "invalid_request"
    return "unknown"


def is_permanent_recipient_error(error):
    return classify_telegram_delivery_error(error) == "permanent_recipient"


def telegram_retry_after_seconds(error):
    result = getattr(error, "result_json", None)
    parameters = result.get("parameters") if isinstance(result, dict) else None
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def is_permanent_telegram_auth_error(error):
    return telegram_error_code(error) == 401


def polling_retry_delay(error, fallback_seconds):
    retry_after = telegram_retry_after_seconds(error)
    if telegram_error_code(error) == 429 and retry_after is not None:
        return retry_after
    return max(1, min(MAX_POLL_RETRY_SECONDS, int(fallback_seconds)))


class PollingExceptionCapture:
    """Capture TeleBot polling failures so the outer loop owns backoff."""

    def __init__(self):
        self._lock = threading.Lock()
        self._error = None

    def clear(self):
        with self._lock:
            self._error = None

    def handle(self, error):
        with self._lock:
            self._error = error
        # False asks TeleBot's one-shot polling loop to stop cleanly.
        return False

    def pop(self):
        with self._lock:
            error = self._error
            self._error = None
            return error


def authenticate_bot_with_backoff(
    bot,
    *,
    on_error=None,
    sleep=time.sleep,
):
    """Authenticate indefinitely across transient failures, but reject 401."""

    retry_seconds = DEFAULT_POLL_RETRY_SECONDS
    while True:
        try:
            return bot.get_me()
        except Exception as error:
            if is_permanent_telegram_auth_error(error):
                raise
            wait_seconds = polling_retry_delay(error, retry_seconds)
            logging.getLogger("ajib.bot.telegram").warning(
                "Telegram authentication interrupted code=%s retry_in=%ss error=%s",
                telegram_error_code(error),
                wait_seconds,
                type(error).__name__,
            )
            if callable(on_error):
                on_error(error, wait_seconds)
            sleep(wait_seconds)
            retry_seconds = min(MAX_POLL_RETRY_SECONDS, retry_seconds * 2)


def run_polling_with_backoff(
    bot,
    *,
    on_error=None,
    on_retry=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    stop_event=None,
    **polling_kwargs,
):
    """Run one-shot TeleBot polling with rate-limit-aware outer backoff."""

    capture = PollingExceptionCapture()
    previous_handler = getattr(bot, "exception_handler", None)
    bot.exception_handler = capture
    retry_seconds = DEFAULT_POLL_RETRY_SECONDS
    options = {
        "non_stop": False,
        "timeout": 25,
        "long_polling_timeout": 25,
        "logger_level": 0,
    }
    options.update(polling_kwargs)
    try:
        while stop_event is None or not stop_event.is_set():
            capture.clear()
            started_at = monotonic()
            caught = None
            try:
                bot.polling(**options)
            except Exception as error:
                caught = error
            error = caught or capture.pop()
            uptime = max(0, monotonic() - started_at)
            if uptime >= STABLE_POLLING_SECONDS:
                retry_seconds = DEFAULT_POLL_RETRY_SECONDS
            if error is None:
                if stop_event is not None and stop_event.is_set():
                    return
                error = RuntimeError("Telegram polling exited unexpectedly")
            wait_seconds = polling_retry_delay(error, retry_seconds)
            logging.getLogger("ajib.bot.telegram").warning(
                "Telegram polling interrupted code=%s retry_in=%ss uptime_s=%s error=%s",
                telegram_error_code(error),
                wait_seconds,
                int(uptime),
                type(error).__name__,
            )
            if callable(on_error):
                on_error(error, wait_seconds)
            sleep(wait_seconds)
            retry_seconds = min(MAX_POLL_RETRY_SECONDS, retry_seconds * 2)
            if callable(on_retry):
                on_retry()
    finally:
        bot.exception_handler = previous_handler


def _invoke_with_timeout(func, timeout_seconds, args, kwargs):
    call_kwargs = dict(kwargs)
    if timeout_seconds is not None:
        call_kwargs.setdefault("timeout", timeout_seconds)
    try:
        return func(*args, **call_kwargs)
    except TypeError as error:
        if "unexpected keyword argument 'timeout'" not in str(error):
            raise
        call_kwargs.pop("timeout", None)
        return func(*args, **call_kwargs)


def _rewind_media_payload(func, args, kwargs):
    method_name = str(getattr(func, "__name__", ""))
    media_key_by_method = {
        "send_photo": "photo",
        "send_document": "document",
        "send_video": "video",
        "send_audio": "audio",
        "send_animation": "animation",
    }
    media_key = media_key_by_method.get(method_name)
    if not media_key:
        return
    payload = kwargs.get(media_key)
    if payload is None and len(args) >= 2:
        payload = args[1]
    seek = getattr(payload, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except Exception:
            pass


def _call_with_timeout(func, timeout_seconds, *args, ignore_expected=True, **kwargs):
    try:
        return _invoke_with_timeout(func, timeout_seconds, args, kwargs)
    except Exception as error:
        if ignore_expected and is_expected_telegram_error(error):
            logging.getLogger("ajib.bot.telegram").debug("Ignored Telegram API error: %s", error)
            return None
        if kwargs.get("parse_mode") and is_parse_entity_error(error):
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("parse_mode", None)
            _rewind_media_payload(func, args, fallback_kwargs)
            logging.getLogger("ajib.bot.telegram").warning(
                "Telegram rejected formatted entities; retrying without parse mode method=%s",
                getattr(func, "__name__", type(func).__name__),
            )
            try:
                return _invoke_with_timeout(func, timeout_seconds, args, fallback_kwargs)
            except Exception as fallback_error:
                if ignore_expected and is_expected_telegram_error(fallback_error):
                    logging.getLogger("ajib.bot.telegram").debug(
                        "Ignored Telegram API error after parse fallback: %s",
                        fallback_error,
                    )
                    return None
                raise
        raise


def safe_answer_callback_query(bot, callback_query_id, *args, **kwargs):
    return _call_with_timeout(
        bot.answer_callback_query,
        get_callback_timeout_seconds(),
        callback_query_id,
        *args,
        **kwargs,
    )


def safe_edit_message_text(bot, *args, **kwargs):
    return _call_with_timeout(bot.edit_message_text, get_telegram_timeout_seconds(), *args, **kwargs)


def safe_delete_message(bot, *args, **kwargs):
    return _call_with_timeout(bot.delete_message, get_telegram_timeout_seconds(), *args, **kwargs)


def safe_send_message(bot, *args, **kwargs):
    return _call_with_timeout(bot.send_message, get_telegram_timeout_seconds(), *args, **kwargs)


def safe_send_photo(bot, *args, **kwargs):
    return _call_with_timeout(bot.send_photo, get_telegram_timeout_seconds(), *args, **kwargs)


def safe_reply_to(bot, *args, **kwargs):
    return _call_with_timeout(bot.reply_to, get_telegram_timeout_seconds(), *args, **kwargs)


def safe_send_chat_action(bot, *args, **kwargs):
    return _call_with_timeout(bot.send_chat_action, get_telegram_timeout_seconds(), *args, **kwargs)


def install_safe_telegram_methods(bot):
    if getattr(bot, "_ajib_safe_telegram_installed", False):
        return bot

    methods = {
        "edit_message_text": get_telegram_timeout_seconds,
        "edit_message_caption": get_telegram_timeout_seconds,
        "edit_message_reply_markup": get_telegram_timeout_seconds,
        "delete_message": get_telegram_timeout_seconds,
        "send_message": get_telegram_timeout_seconds,
        "send_photo": get_telegram_timeout_seconds,
        "reply_to": get_telegram_timeout_seconds,
        "send_chat_action": get_telegram_timeout_seconds,
    }

    answer_callback_query = getattr(bot, "answer_callback_query", None)
    if answer_callback_query is not None:
        @wraps(answer_callback_query)
        def wrapped_answer_callback_query(*args, __original=answer_callback_query, **kwargs):
            return CALLBACK_ANSWER_EXECUTOR.submit(
                _call_with_timeout,
                __original,
                get_callback_timeout_seconds(),
                *args,
                **kwargs,
            )

        setattr(bot, "_ajib_original_answer_callback_query", answer_callback_query)
        setattr(bot, "answer_callback_query", wrapped_answer_callback_query)

    for method_name, timeout_getter in methods.items():
        original = getattr(bot, method_name, None)
        if original is None:
            continue

        @wraps(original)
        def wrapped(*args, __original=original, __timeout_getter=timeout_getter, **kwargs):
            return _call_with_timeout(__original, __timeout_getter(), *args, **kwargs)

        setattr(bot, f"_ajib_original_{method_name}", original)
        setattr(bot, method_name, wrapped)

    setattr(bot, "_ajib_safe_telegram_installed", True)
    return bot
