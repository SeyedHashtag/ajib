"""Admin user inspection, exact-server editing, and panel-aware user copying."""

import io
import secrets
import threading
import time
from types import SimpleNamespace

import qrcode
from telebot import types

from utils.command import bot, is_admin
from utils.common import (
    admin_action_text,
    create_admin_markup,
    is_admin_main_menu_button,
    resolve_admin_menu_view,
)
from utils.api_client import APIClient, MultiServerAPI
try:
    from utils.api_client import BLITZ_PANEL, THREE_X_UI_PANEL, UserCopySpec, UserRef
except ImportError:  # Lightweight handler tests may provide the legacy surface.
    BLITZ_PANEL = "blitz"
    THREE_X_UI_PANEL = "3x-ui"
    UserRef = SimpleNamespace
    UserCopySpec = SimpleNamespace
from utils.account_state import inspect_account


CONTEXT_TTL_SECONDS = 20 * 60
_CONTEXT_LOCK = threading.RLock()
_USER_CONTEXTS = {}
_COPY_CONTEXTS = {}


def _escape_markdown(value):
    escaped = str(value or "").replace("\\", "\\\\")
    for char in ("`", "*", "_", "["):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _format_server_label(api_client):
    server_name = str(getattr(api_client, "server_name", None) or "").strip()
    server_id = str(getattr(api_client, "server_id", None) or "").strip()
    if server_name and server_id and server_name != server_id:
        return f"{_escape_markdown(server_name)} (`{server_id}`)"
    if server_id:
        return f"`{server_id}`"
    if server_name:
        return _escape_markdown(server_name)
    return None


def _new_token():
    return secrets.token_urlsafe(7)


def _prune_contexts():
    cutoff = time.monotonic() - CONTEXT_TTL_SECONDS
    with _CONTEXT_LOCK:
        for store in (_USER_CONTEXTS, _COPY_CONTEXTS):
            for token in list(store):
                if store[token].get("created_at", 0) < cutoff:
                    store.pop(token, None)


def _store_user_context(ref):
    _prune_contexts()
    token = _new_token()
    with _CONTEXT_LOCK:
        _USER_CONTEXTS[token] = {"ref": ref, "created_at": time.monotonic()}
    return token


def _get_user_context(token):
    _prune_contexts()
    with _CONTEXT_LOCK:
        entry = _USER_CONTEXTS.get(token)
    return entry.get("ref") if entry else None


def _store_copy_context(source_ref, destination_server_id, inbound_ids=None, inbound_options=None):
    _prune_contexts()
    token = _new_token()
    with _CONTEXT_LOCK:
        _COPY_CONTEXTS[token] = {
            "source_ref": source_ref,
            "destination_server_id": destination_server_id,
            "inbound_ids": list(inbound_ids or []),
            "inbound_options": list(inbound_options or []),
            "created_at": time.monotonic(),
        }
    return token


def _get_copy_context(token):
    _prune_contexts()
    with _CONTEXT_LOCK:
        return _COPY_CONTEXTS.get(token)


def _make_ref(client, username):
    return UserRef(
        server_id=str(client.server_id),
        username=str(username),
        panel_type=getattr(client, "panel_type", BLITZ_PANEL),
    )


def _resolve_user_context(token, multi_api=None):
    ref = _get_user_context(token)
    if ref is not None:
        return ref
    # Old callback messages carried only a username. Resolve them only when
    # that username is unique across every configured server.
    multi_api = multi_api or MultiServerAPI()
    if hasattr(multi_api, "find_user_matches"):
        matches = multi_api.find_user_matches(token, force_refresh=True)
        if len(matches) == 1:
            return matches[0]["ref"]
    return None


def _find_matches(multi_api, username):
    if hasattr(multi_api, "find_user_matches"):
        return multi_api.find_user_matches(username, force_refresh=True)
    client, user = multi_api.find_user(username)
    if client is None or user is None:
        return []
    return [{"client": client, "user": user, "ref": _make_ref(client, username)}]


@bot.callback_query_handler(func=lambda call: call.data == "cancel_show_user")
def handle_cancel_show_user(call):
    bot.answer_callback_query(call.id)
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    bot.edit_message_text("Operation canceled.", chat_id=call.message.chat.id, message_id=call.message.message_id)


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == admin_action_text("show_user"))
def show_user(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_show_user"))
    msg = bot.reply_to(message, "Enter username:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_show_user)


def process_show_user(message):
    if is_admin_main_menu_button(message.text):
        view = resolve_admin_menu_view(message.text) or "root"
        bot.reply_to(message, "Operation canceled.", reply_markup=create_admin_markup(view))
        return
    username = str(message.text or "").strip().lower()
    if not username:
        bot.reply_to(message, "Username cannot be empty.")
        return
    bot.send_chat_action(message.chat.id, "typing")
    multi_api = MultiServerAPI()
    matches = _find_matches(multi_api, username)
    if not matches:
        bot.reply_to(message, f"User '{username}' not found or API error.")
        return
    if len(matches) > 1:
        markup = types.InlineKeyboardMarkup(row_width=1)
        for match in matches:
            ref = match["ref"]
            token = _store_user_context(ref)
            client = match["client"]
            label = f"{client.server_name} ({client.server_id}, {getattr(client, 'panel_type', BLITZ_PANEL)})"
            markup.add(types.InlineKeyboardButton(label, callback_data=f"show_user_ref:{token}"))
        bot.reply_to(message, "This username exists on more than one server. Select the exact account:", reply_markup=markup)
        return
    match = matches[0]
    _send_user_details(message, match["client"], match["user"], match["ref"])


@bot.callback_query_handler(func=lambda call: call.data.startswith("show_user_ref:"))
def handle_show_user_ref(call):
    bot.answer_callback_query(call.id)
    ref = _get_user_context(call.data.split(":", 1)[1])
    if ref is None:
        bot.send_message(call.message.chat.id, "This selection expired. Open Show User again.")
        return
    client, user, result = MultiServerAPI().find_user_on_server(ref.username, ref.server_id)
    if result.get("status") != "found":
        bot.send_message(call.message.chat.id, "The selected account is no longer available.")
        return
    _send_user_details(call.message, client, user, ref)


def _send_user_details(message, api_client, user_details, ref):
    actual_username = user_details.get("username") or ref.username
    try:
        upload_bytes = user_details.get("upload_bytes")
        download_bytes = user_details.get("download_bytes")
        status = user_details.get("status", "Unknown")
        if upload_bytes is None or download_bytes is None:
            traffic_message = "**Traffic Data:**\nUser not active or no traffic data available."
        else:
            upload_gb = upload_bytes / (1024 ** 3)
            download_gb = download_bytes / (1024 ** 3)
            traffic_message = (
                f"🔼 Upload: {upload_gb:.2f} GB\n"
                f"🔽 Download: {download_gb:.2f} GB\n"
                f"📊 Total Usage: {upload_gb + download_gb:.2f} GB\n"
                f"🌐 Status: {status}"
            )
        traffic_limit = int(user_details.get("max_download_bytes") or 0) / (1024 ** 3)
    except (TypeError, ValueError, OverflowError) as error:
        bot.reply_to(message, f"Failed to process user data: {error}")
        return

    shared_state = inspect_account(user_details, source="admin_user_detail")
    server_label = _format_server_label(api_client)
    panel_type = getattr(api_client, "panel_type", user_details.get("panel_type", BLITZ_PANEL))
    unlimited_duration = shared_state.configured_days == 0
    configured_duration = (
        "Unlimited"
        if unlimited_duration
        else (
            f"{shared_state.configured_days} days"
            if shared_state.configured_days is not None
            else "Unknown"
        )
    )
    panel_remaining_line = (
        "⏱ Panel Time Remaining: Unlimited\n"
        if unlimited_duration
        else (
            f"⏱ Panel Time Remaining: {shared_state.panel_days_remaining} days\n"
            if shared_state.panel_days_remaining is not None
            else ""
        )
    )
    formatted_details = (
        f"\n🆔 Name: {_escape_markdown(actual_username)}\n"
        f"🌐 Server: {server_label}\n"
        f"🧩 Panel: `{panel_type}`\n"
        f"📊 Traffic Limit: {traffic_limit:.2f} GB\n"
        f"🔖 State: {shared_state.state}\n"
        f"📅 Configured Duration: {configured_duration}\n"
        f"{panel_remaining_line}"
        f"⏳ Timer Start: {user_details.get('account_creation_date') or 'First connection'}\n"
        f"💡 Blocked: {user_details.get('blocked')}\n\n"
        f"{traffic_message}"
    )
    uri_data = api_client.get_user_uri(actual_username)
    if not uri_data or not uri_data.get("normal_sub"):
        bot.reply_to(message, f"Error: Could not retrieve subscription URL for user '{actual_username}'. Check API configuration.")
        return
    sub_url = uri_data["normal_sub"]
    ipv4_url = uri_data.get("ipv4", "")
    qr_code = qrcode.make(ipv4_url or sub_url)
    bio = io.BytesIO()
    qr_code.save(bio, "PNG")
    bio.seek(0)

    token = _store_user_context(_make_ref(api_client, actual_username))
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton("Reset User", callback_data=f"reset_user:{token}"))
    markup.add(
        types.InlineKeyboardButton("Edit Username", callback_data=f"edit_username:{token}"),
        types.InlineKeyboardButton("Edit Traffic Limit", callback_data=f"edit_traffic:{token}"),
    )
    markup.add(
        types.InlineKeyboardButton("Edit Expiration Days", callback_data=f"edit_expiration:{token}"),
        types.InlineKeyboardButton("Renew Password", callback_data=f"renew_password:{token}"),
    )
    markup.add(
        types.InlineKeyboardButton("Renew Creation Date", callback_data=f"renew_creation:{token}"),
        types.InlineKeyboardButton("Block User", callback_data=f"block_user:{token}"),
    )
    if panel_type in {BLITZ_PANEL, THREE_X_UI_PANEL}:
        markup.add(types.InlineKeyboardButton("📋 Copy User", callback_data=f"copy_user:{token}"))

    caption = f"{formatted_details}\n\n"
    if ipv4_url:
        caption += f"IPv4 URL: `{ipv4_url}`\n\n"
    caption += f"Subscription URL:\n{sub_url}"
    bot.send_photo(message.chat.id, bio, caption=caption, reply_markup=markup, parse_mode="Markdown")


EDIT_ACTIONS = (
    "edit_username:", "edit_traffic:", "edit_expiration:", "renew_password:",
    "renew_creation:", "block_user:", "reset_user:",
)


@bot.callback_query_handler(func=lambda call: any(call.data.startswith(prefix) for prefix in EDIT_ACTIONS))
def handle_edit_callback(call):
    action, token = call.data.split(":", 1)
    multi_api = MultiServerAPI()
    ref = _resolve_user_context(token, multi_api)
    if ref is None:
        bot.send_message(call.message.chat.id, "This account selection expired or is ambiguous. Open Show User again.")
        return
    api_client, _, result = multi_api.find_user_on_server(ref.username, ref.server_id)
    if result.get("status") != "found":
        bot.send_message(call.message.chat.id, f"User '{ref.username}' is unavailable on server '{ref.server_id}'.")
        return
    if action == "edit_username":
        msg = bot.send_message(call.message.chat.id, f"Enter new username for {ref.username}:")
        bot.register_next_step_handler(msg, process_edit_username, token)
    elif action == "edit_traffic":
        msg = bot.send_message(call.message.chat.id, f"Enter new traffic limit (GB) for {ref.username}:")
        bot.register_next_step_handler(msg, process_edit_traffic, token)
    elif action == "edit_expiration":
        msg = bot.send_message(
            call.message.chat.id,
            f"Enter new expiration days for {ref.username} (0 = unlimited):",
        )
        bot.register_next_step_handler(msg, process_edit_expiration, token)
    elif action == "renew_password":
        _report_update(call.message.chat.id, api_client.update_user(ref.username, {"renew_password": True}), "Password renewed.", "Password renewal failed.")
    elif action == "renew_creation":
        _report_update(call.message.chat.id, api_client.update_user(ref.username, {"renew_creation_date": True}), "Creation date renewed.", "Creation-date renewal failed.")
    elif action == "block_user":
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("True", callback_data=f"confirm_block:{token}:true"),
            types.InlineKeyboardButton("False", callback_data=f"confirm_block:{token}:false"),
        )
        bot.send_message(call.message.chat.id, f"Set block status for {ref.username}:", reply_markup=markup)
    elif action == "reset_user":
        _report_update(call.message.chat.id, api_client.reset_user(ref.username), "User reset successfully.", "User reset failed. For imported 3x-ui users, reset is refused when the original duration is unknown.")


def _report_update(chat_id, result, success, failure):
    bot.send_message(chat_id, success if result is not None else failure)


@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_block:"))
def handle_block_confirmation(call):
    _, token, block_status = call.data.split(":", 2)
    multi_api = MultiServerAPI()
    ref = _resolve_user_context(token, multi_api)
    if ref is None:
        bot.send_message(call.message.chat.id, "This account selection expired. Open Show User again.")
        return
    api_client, _, lookup = multi_api.find_user_on_server(ref.username, ref.server_id)
    if lookup.get("status") != "found":
        bot.send_message(call.message.chat.id, "The selected account is unavailable.")
        return
    is_blocked = block_status == "true"
    result = api_client.update_user(ref.username, {"blocked": is_blocked})
    _report_update(call.message.chat.id, result, f"User '{ref.username}' {'blocked' if is_blocked else 'unblocked'} successfully.", "Failed to update block status.")


def _exact_client_for_step(token):
    multi_api = MultiServerAPI()
    ref = _resolve_user_context(token, multi_api)
    if ref is None:
        return None, None
    client, _, result = multi_api.find_user_on_server(ref.username, ref.server_id)
    return (client, ref) if result.get("status") == "found" else (None, ref)


def process_edit_username(message, token):
    new_username = str(message.text or "").strip()
    if not new_username:
        bot.reply_to(message, "Username cannot be empty.")
        return
    client, ref = _exact_client_for_step(token)
    result = client.update_user(ref.username, {"new_username": new_username}) if client and ref else None
    bot.reply_to(message, f"Username updated to '{new_username}' successfully." if result is not None else "Failed to update username.")


def process_edit_traffic(message, token):
    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError
    except (TypeError, ValueError):
        bot.reply_to(message, "Invalid traffic limit. Please enter a positive number.")
        return
    client, ref = _exact_client_for_step(token)
    result = client.update_user(ref.username, {"new_traffic_limit": value}) if client and ref else None
    bot.reply_to(message, f"Traffic limit updated to {value} GB successfully." if result is not None else "Failed to update traffic limit.")


def process_edit_expiration(message, token):
    try:
        value = int(message.text.strip())
        if value < 0:
            raise ValueError
    except (TypeError, ValueError):
        bot.reply_to(message, "Invalid expiration. Enter zero for unlimited or a positive number of days.")
        return
    client, ref = _exact_client_for_step(token)
    result = client.update_user(ref.username, {"new_expiration_days": value}) if client and ref else None
    success = (
        "Expiration updated to unlimited successfully."
        if value == 0
        else f"Expiration updated to {value} days successfully."
    )
    bot.reply_to(message, success if result is not None else "Failed to update expiration.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_user:"))
def handle_copy_user(call):
    ref = _get_user_context(call.data.split(":", 1)[1])
    if ref is None or ref.panel_type not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
        bot.send_message(call.message.chat.id, "This copy selection expired or the source panel is unsupported.")
        return
    multi_api = MultiServerAPI()
    destinations = [server for server in multi_api.servers if str(server.get("id")) != str(ref.server_id)]
    if ref.panel_type == THREE_X_UI_PANEL:
        destinations = [
            server for server in destinations
            if server.get("panel", BLITZ_PANEL) == BLITZ_PANEL
        ]
    if not destinations:
        bot.send_message(call.message.chat.id, "No compatible destination VPN server is configured.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for server in destinations:
        copy_token = _store_copy_context(ref, server["id"])
        suffix = "disabled / copy-only" if not server.get("enabled", True) else "enabled"
        markup.add(types.InlineKeyboardButton(
            f"{server.get('name', server['id'])} ({server.get('panel', BLITZ_PANEL)}, {suffix})",
            callback_data=f"copy_dest:{copy_token}",
        ))
    bot.send_message(call.message.chat.id, f"Copy '{ref.username}' to which server?", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_dest:"))
def handle_copy_destination(call):
    token = call.data.split(":", 1)[1]
    context = _get_copy_context(token)
    if context is None:
        bot.send_message(call.message.chat.id, "This copy selection expired.")
        return
    destination = MultiServerAPI().get_client(context["destination_server_id"])
    if destination is None:
        bot.send_message(call.message.chat.id, "Destination server is no longer configured.")
        return
    if getattr(destination, "panel_type", BLITZ_PANEL) != THREE_X_UI_PANEL:
        _send_copy_confirmation(call.message.chat.id, token, context, destination)
        return
    options = destination.get_inbound_options()
    allowed = {"hysteria", "hysteria2", "hy2"}
    hysteria = [item for item in (options or []) if str(item.get("protocol") or "").lower() in allowed]
    if not hysteria:
        bot.send_message(call.message.chat.id, "No live Hysteria2 inbound is available on that 3x-ui server.")
        return
    with _CONTEXT_LOCK:
        context["inbound_options"] = hysteria
    _send_inbound_selector(call.message.chat.id, token, context, destination)


def _send_inbound_selector(chat_id, token, context, destination):
    selected = set(context.get("inbound_ids") or [])
    markup = types.InlineKeyboardMarkup(row_width=1)
    for item in context.get("inbound_options") or []:
        marker = "✅" if item["id"] in selected else "⬜"
        markup.add(types.InlineKeyboardButton(
            f"{marker} {item.get('remark')} (#{item['id']})",
            callback_data=f"copy_in:{token}:{item['id']}",
        ))
    markup.add(types.InlineKeyboardButton("Continue", callback_data=f"copy_in_done:{token}"))
    bot.send_message(chat_id, f"Select one or more Hysteria2 inbounds on {destination.server_name}:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_in:"))
def handle_copy_inbound_toggle(call):
    _, token, raw_id = call.data.split(":", 2)
    context = _get_copy_context(token)
    if context is None:
        bot.send_message(call.message.chat.id, "This inbound selection expired.")
        return
    inbound_id = int(raw_id)
    valid_ids = {item["id"] for item in context.get("inbound_options") or []}
    if inbound_id not in valid_ids:
        bot.send_message(call.message.chat.id, "That inbound is no longer selectable.")
        return
    with _CONTEXT_LOCK:
        selected = context["inbound_ids"]
        selected.remove(inbound_id) if inbound_id in selected else selected.append(inbound_id)
    destination = MultiServerAPI().get_client(context["destination_server_id"])
    _send_inbound_selector(call.message.chat.id, token, context, destination)


@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_in_done:"))
def handle_copy_inbound_done(call):
    token = call.data.split(":", 1)[1]
    context = _get_copy_context(token)
    if context is None or not context.get("inbound_ids"):
        bot.send_message(call.message.chat.id, "Select at least one Hysteria2 inbound.")
        return
    destination = MultiServerAPI().get_client(context["destination_server_id"])
    _send_copy_confirmation(call.message.chat.id, token, context, destination)


def _send_copy_confirmation(chat_id, token, context, destination):
    selected = context.get("inbound_ids") or []
    inbound_line = f"\nHysteria2 inbound IDs: {', '.join(map(str, selected))}" if selected else ""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Confirm copy", callback_data=f"copy_confirm:{token}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="copy_cancel"),
    )
    bot.send_message(
        chat_id,
        f"Copy '{context['source_ref'].username}' from '{context['source_ref'].server_id}' "
        f"({context['source_ref'].panel_type}) to "
        f"'{destination.server_name}' ({destination.panel_type})?{inbound_line}\n\n"
        "The source will not be changed. A destination collision will stop the copy.",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "copy_cancel")
def handle_copy_cancel(call):
    bot.answer_callback_query(call.id, "Copy canceled.")


COPY_ERRORS = {
    "source_missing": "The source no longer exists.",
    "source_unavailable": "The source server is unavailable; nothing was copied.",
    "source_password_missing": "The source password is unavailable; nothing was copied.",
    "source_auth_missing": "The 3x-ui source has no reusable Hysteria2 auth credential.",
    "source_inbounds_unavailable": "The 3x-ui source inbound list is unavailable.",
    "source_not_hysteria2": "The 3x-ui source is not attached to a Hysteria2 inbound.",
    "source_state_malformed": "The source quota, traffic, duration, or block state is malformed.",
    "destination_panel_not_supported": "That source-to-destination panel combination is not supported.",
    "destination_exists": "That username already exists on the destination; nothing was changed.",
    "destination_unavailable": "The destination could not be checked safely; nothing was copied.",
    "destination_create_outcome_unknown": "The create request had an uncertain outcome.",
    "destination_note_failed": "The account was created, but its note could not be restored.",
    "inbounds_required": "Select at least one Hysteria2 inbound.",
    "inbounds_unavailable": "The destination inbound list is unavailable.",
    "inbounds_not_hysteria2": "The selected destination inbounds are not all Hysteria2.",
    "blitz_unlimited_not_representable": "An unlimited allowance cannot be copied safely to Blitz.",
    "blitz_allowance_exhausted": "The source has no remaining allowance to copy to Blitz.",
}


@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_confirm:"))
def handle_copy_confirm(call):
    token = call.data.split(":", 1)[1]
    context = _get_copy_context(token)
    if context is None:
        bot.send_message(call.message.chat.id, "This copy confirmation expired.")
        return
    bot.send_chat_action(call.message.chat.id, "typing")
    multi_api = MultiServerAPI()
    copy_spec = UserCopySpec(
        source=context["source_ref"],
        destination_server_id=context["destination_server_id"],
        inbound_ids=tuple(context.get("inbound_ids") or []),
    )
    if hasattr(multi_api, "copy_user"):
        result = multi_api.copy_user(copy_spec)
    else:  # Rolling-upgrade compatibility.
        result = multi_api.copy_blitz_user(
            context["source_ref"],
            context["destination_server_id"],
            context.get("inbound_ids"),
        )
    if not result.get("ok"):
        message = COPY_ERRORS.get(result.get("error"), f"Copy failed safely ({result.get('error')}).")
        if result.get("rollback_failed"):
            message += (
                f"\n\n⚠️ Rollback also failed. A partial account may remain on server "
                f"'{result.get('partial_destination')}'. Inspect it manually before retrying."
            )
        elif result.get("partial_destination"):
            message += (
                f" A partial account may exist on server '{result.get('partial_destination')}'. "
                "Inspect it manually before retrying; it was not deleted because ownership could not be proven safely."
            )
        elif result.get("rolled_back"):
            message += " The newly-created destination account was rolled back."
        bot.send_message(call.message.chat.id, message)
        return

    sub_url = result["normal_sub"]
    qr_code = qrcode.make(sub_url)
    bio = io.BytesIO()
    qr_code.save(bio, "PNG")
    bio.seek(0)
    inbound_line = (
        f"\nHysteria2 inbound IDs: {', '.join(map(str, result.get('inbound_ids') or []))}"
        if result.get("inbound_ids") else ""
    )
    link_type = "Direct connection link" if result.get("direct_link") else "Subscription URL"
    expiry_line = ""
    if result.get("expiry_rounded"):
        extension_seconds = max(0, int(result.get("expiry_extension_seconds") or 0))
        extension_hours = extension_seconds / 3600
        expiry_line = (
            f"\n⚠️ Blitz day precision rounded expiry outward by {extension_hours:.2f} hours."
        )
    caption = (
        f"User '{_escape_markdown(result['username'])}' copied successfully.\n"
        f"Source: `{result.get('source_server_id')}`, `{result.get('source_panel_type', BLITZ_PANEL)}`\n"
        f"Target: {_escape_markdown(result['destination_server_name'])} "
        f"(`{result['destination_server_id']}`, `{result['panel_type']}`){inbound_line}"
        f"{expiry_line}\n\n"
        f"{link_type}:\n{sub_url}"
    )
    bot.send_photo(call.message.chat.id, bio, caption=caption, parse_mode="Markdown")
