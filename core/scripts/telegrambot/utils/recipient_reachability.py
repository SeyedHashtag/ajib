"""Shared Telegram recipient reachability registry.

The registry intentionally reuses the historical broadcast exclusion state so
existing backups, reset controls, and operational data remain compatible.
"""

import json
import logging
import os


UNREACHABLE_RECIPIENTS_PATH = (
    "/etc/ajib/core/scripts/telegrambot/broadcast_failed_users.json"
)

logger = logging.getLogger("ajib.telegram.delivery")


def _state_helpers():
    try:
        from utils.atomic_store import locked_json, read_json
        from utils.state_store import delete_state

        return locked_json, read_json, delete_state
    except ImportError:
        return None


def _normalized_user_ids(values):
    if not isinstance(values, list):
        return set()
    return {str(user_id) for user_id in values}


def load_unreachable_recipients():
    """Return recipient IDs Telegram has permanently rejected."""

    try:
        helpers = _state_helpers()
        if helpers:
            data = helpers[1](UNREACHABLE_RECIPIENTS_PATH, [])
        elif os.path.exists(UNREACHABLE_RECIPIENTS_PATH):
            with open(UNREACHABLE_RECIPIENTS_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        else:
            data = []
        return _normalized_user_ids(data)
    except Exception as error:
        logger.warning("Recipient reachability load failed error_type=%s", type(error).__name__)
        return set()


def save_unreachable_recipients(user_ids):
    """Replace the registry, preserving the legacy broadcast reset API."""

    values = sorted({str(user_id) for user_id in user_ids})
    try:
        helpers = _state_helpers()
        if helpers:
            with helpers[0](UNREACHABLE_RECIPIENTS_PATH, []) as stored:
                if not isinstance(stored, list):
                    raise ValueError("Recipient exclusions must contain a JSON list.")
                stored.clear()
                stored.extend(values)
        else:
            os.makedirs(os.path.dirname(UNREACHABLE_RECIPIENTS_PATH) or ".", exist_ok=True)
            with open(UNREACHABLE_RECIPIENTS_PATH, "w", encoding="utf-8") as handle:
                json.dump(values, handle)
        return True
    except Exception as error:
        logger.warning("Recipient reachability save failed error_type=%s", type(error).__name__)
        return False


def mark_recipients_unreachable(user_ids):
    """Register recipients atomically and return the IDs newly added."""

    keys = {str(user_id) for user_id in user_ids}
    if not keys:
        return set()
    try:
        helpers = _state_helpers()
        if helpers:
            with helpers[0](UNREACHABLE_RECIPIENTS_PATH, []) as stored:
                if not isinstance(stored, list):
                    raise ValueError("Recipient exclusions must contain a JSON list.")
                values = {str(value) for value in stored}
                added = keys - values
                if not added:
                    return set()
                values.update(added)
                stored.clear()
                stored.extend(sorted(values))
                return added
        values = load_unreachable_recipients()
        added = keys - values
        if not added:
            return set()
        values.update(added)
        return added if save_unreachable_recipients(values) else set()
    except Exception as error:
        logger.warning("Recipient reachability mark failed error_type=%s", type(error).__name__)
        return set()


def mark_recipient_unreachable(user_id):
    """Register one recipient. Return True only when state changed."""

    return str(user_id) in mark_recipients_unreachable((user_id,))


def clear_recipient_unreachable(user_id):
    """Clear one recipient after inbound activity proves renewed reachability."""

    key = str(user_id)
    try:
        if key not in load_unreachable_recipients():
            return False
        helpers = _state_helpers()
        if helpers:
            with helpers[0](UNREACHABLE_RECIPIENTS_PATH, []) as stored:
                if not isinstance(stored, list):
                    raise ValueError("Recipient exclusions must contain a JSON list.")
                values = {str(value) for value in stored}
                if key not in values:
                    return False
                values.remove(key)
                stored.clear()
                stored.extend(sorted(values))
                return True
        values = load_unreachable_recipients()
        if key not in values:
            return False
        values.remove(key)
        return save_unreachable_recipients(values)
    except Exception as error:
        logger.warning("Recipient reachability clear failed error_type=%s", type(error).__name__)
        return False


def reset_unreachable_recipients():
    """Delete all recipient exclusions."""

    try:
        helpers = _state_helpers()
        if helpers:
            helpers[2](UNREACHABLE_RECIPIENTS_PATH)
        elif os.path.exists(UNREACHABLE_RECIPIENTS_PATH):
            os.remove(UNREACHABLE_RECIPIENTS_PATH)
    except Exception as error:
        logger.warning("Recipient reachability reset failed error_type=%s", type(error).__name__)
