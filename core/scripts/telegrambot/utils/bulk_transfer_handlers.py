"""Telegram admin workflow for persistent mass copy and migration jobs."""

from __future__ import annotations

import io
import threading

from telebot import types

from .api_client import BLITZ_PANEL, THREE_X_UI_PANEL, BulkUserTransferSpec, MultiServerAPI
from .bulk_transfer import (
    compatible_destinations,
    create_transfer_job,
    decide_deferred_notifications,
    deferred_recipient_preview,
    export_job_csv,
    get_active_job,
    get_job,
    get_latest_job,
    job_counts,
    notification_counts,
    preflight_transfer,
    request_cancel,
    resume_job,
    set_progress_callback,
    start_transfer_worker,
)
from .command import bot, is_admin
from .common import admin_action_text
from .telegram_safe import safe_answer_callback_query, safe_edit_message_text, safe_reply_to


_contexts = {}
_context_lock = threading.RLock()


def _context(admin_id):
    with _context_lock:
        return _contexts.get(str(admin_id))


def _save_context(admin_id, value):
    with _context_lock:
        _contexts[str(admin_id)] = value
    return value


def _drop_context(admin_id):
    with _context_lock:
        _contexts.pop(str(admin_id), None)


def _server_label(server):
    panel = str(server.get("panel") or server.get("panel_type") or BLITZ_PANEL)
    return f"{server.get('name') or server.get('id')} ({panel})"


def _public_servers(servers):
    return [
        {
            key: server.get(key)
            for key in (
                "id", "name", "panel", "panel_type", "enabled", "weight",
                "default_inbound_ids", "default_limit_ip",
            )
            if key in server
        }
        for server in servers
    ]


def _edit(call, text, markup=None):
    return safe_edit_message_text(
        bot, text, chat_id=call.message.chat.id, message_id=call.message.message_id,
        reply_markup=markup,
    )


def _root_markup(has_job=False):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📋 Mass Copy", callback_data="bulk:new:copy"))
    markup.add(types.InlineKeyboardButton("🚚 Migrate Server Users", callback_data="bulk:new:migrate"))
    if has_job:
        markup.add(types.InlineKeyboardButton("📊 Latest Transfer", callback_data="bulk:latest"))
    return markup


def _root_text():
    active = get_active_job()
    latest = active or get_latest_job()
    text = (
        "🔁 Mass Copy / Migrate\n\n"
        "Mass Copy duplicates a fixed source snapshot and never changes source accounts or bot records.\n\n"
        "Migrate verifies each destination, updates exact bot records, then deletes and verifies each source."
    )
    if active:
        text += f"\n\nAn active {active['mode']} job is {active['status']}."
    elif latest:
        text += f"\n\nLatest job: {latest['mode']} — {latest['status']}."
    return text, _root_markup(latest is not None)


@bot.message_handler(
    func=lambda message: is_admin(message.from_user.id)
    and message.text == admin_action_text("bulk_transfer")
)
def show_bulk_transfer(message):
    text, markup = _root_text()
    safe_reply_to(bot, message, text, reply_markup=markup)


def _render_sources(call, mode):
    if get_active_job() is not None:
        safe_answer_callback_query(bot, call.id, "Another transfer is active.", show_alert=True)
        return
    servers = _public_servers(MultiServerAPI().servers)
    _save_context(call.from_user.id, {"mode": mode, "servers": servers})
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, server in enumerate(servers):
        markup.add(types.InlineKeyboardButton(_server_label(server), callback_data=f"bulk:src:{index}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="bulk:home"))
    _edit(call, f"Choose the source server for {'migration' if mode == 'migrate' else 'mass copy'}:", markup)


def _render_destinations(call, source_index):
    context = _context(call.from_user.id)
    if not context or source_index < 0 or source_index >= len(context.get("servers") or []):
        _edit(call, "This setup expired. Open Mass Copy / Migrate again.", _root_markup(bool(get_latest_job())))
        return
    source = context["servers"][source_index]
    destinations = compatible_destinations(source.get("id"))
    context.update({"source": source, "destinations": destinations})
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, server in enumerate(destinations):
        markup.add(types.InlineKeyboardButton(_server_label(server), callback_data=f"bulk:dst:{index}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data=f"bulk:new:{context['mode']}"))
    text = f"Source: {_server_label(source)}\n\nChoose a compatible destination:"
    if not destinations:
        text += "\nNo compatible destinations are configured."
    _edit(call, text, markup)


def _destination_panel(server):
    return str(server.get("panel") or server.get("panel_type") or BLITZ_PANEL).lower()


def _render_inbounds(call):
    context = _context(call.from_user.id)
    destination = (context or {}).get("destination")
    client = MultiServerAPI().get_client(destination.get("id")) if destination else None
    options = client.get_inbound_options() if client and _destination_panel(destination) == THREE_X_UI_PANEL else None
    if options is None:
        _edit(call, "Destination Hysteria inbounds are unavailable. Choose another destination.")
        return
    allowed = [
        item for item in options
        if str(item.get("protocol") or "").lower() in {"hysteria", "hysteria2", "hy2"}
    ]
    allowed_ids = {int(item["id"]) for item in allowed}
    defaults = {
        int(value) for value in (destination.get("default_inbound_ids") or [])
        if str(value).isdigit() and int(value) in allowed_ids
    }
    context["inbound_options"] = allowed
    context.setdefault("inbound_ids", defaults)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, item in enumerate(allowed):
        inbound_id = int(item["id"])
        selected = inbound_id in context["inbound_ids"]
        label = f"{'✅' if selected else '⬜'} {item.get('remark') or item.get('name') or inbound_id}"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"bulk:inb:{index}"))
    markup.add(types.InlineKeyboardButton("Continue", callback_data="bulk:inbdone"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="bulk:sourceagain"))
    _edit(call, "Select one or more shared Hysteria/Hysteria2 destination inbounds:", markup)


def _render_notification_policy(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📨 Send after each verified migration", callback_data="bulk:policy:send"))
    markup.add(types.InlineKeyboardButton("🔕 Do not send", callback_data="bulk:policy:disabled"))
    markup.add(types.InlineKeyboardButton("⏳ Decide later", callback_data="bulk:policy:deferred"))
    _edit(call, "Choose customer-notification delivery for this migration:", markup)


def _spec_from_context(context, admin_id):
    return BulkUserTransferSpec(
        mode=context["mode"],
        source_server_id=str(context["source"]["id"]),
        destination_server_id=str(context["destination"]["id"]),
        inbound_ids=tuple(sorted(context.get("inbound_ids") or ())),
        requesting_admin=str(admin_id),
        notification_policy=context.get("notification_policy", "disabled"),
    )


def _preflight_text(context, result):
    rejections = result.get("rejections") or {}
    lines = [
        "Transfer preflight",
        "",
        f"Mode: {context['mode']}",
        f"Source: {_server_label(context['source'])}",
        f"Destination: {_server_label(context['destination'])}",
        f"Snapshot users: {result.get('total', 0)}",
        f"Eligible: {result.get('eligible', 0)}",
        f"Collisions: {result.get('collisions', 0)}",
    ]
    if context["mode"] == "migrate":
        lines.append(f"Notifications: {context.get('notification_policy', 'disabled')}")
    if rejections:
        lines.extend(("", "Skipped/rejected:"))
        lines.extend(f"• {reason}: {count}" for reason, count in rejections.items())
    lines.extend(("", "Users added after this snapshot will not be included."))
    return "\n".join(lines)


def _run_preflight(call):
    context = _context(call.from_user.id)
    if not context:
        _edit(call, "This setup expired. Open Mass Copy / Migrate again.")
        return
    _edit(call, "Checking both panels and building a fixed user snapshot…")
    result = preflight_transfer(_spec_from_context(context, call.from_user.id))
    if not result.get("ok"):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Start Over", callback_data="bulk:home"))
        _edit(call, f"Preflight failed: {result.get('error', 'unknown_error')}", markup)
        return
    context["preflight"] = result
    context["confirmation_stage"] = 0
    markup = types.InlineKeyboardMarkup(row_width=1)
    if result.get("eligible"):
        action = "Review Migration Warning" if context["mode"] == "migrate" else "Confirm Mass Copy"
        markup.add(types.InlineKeyboardButton(action, callback_data="bulk:confirm"))
    markup.add(types.InlineKeyboardButton("Cancel", callback_data="bulk:home"))
    _edit(call, _preflight_text(context, result), markup)


def _render_confirmation(call):
    context = _context(call.from_user.id)
    if not context or not context.get("preflight"):
        _edit(call, "This preflight expired. Start again.", _root_markup(bool(get_latest_job())))
        return
    if context["mode"] == "copy":
        _start_confirmed_job(call, context)
        return
    stage = int(context.get("confirmation_stage") or 0) + 1
    context["confirmation_stage"] = stage
    markup = types.InlineKeyboardMarkup(row_width=1)
    if stage == 1:
        markup.add(types.InlineKeyboardButton("I understand — continue", callback_data="bulk:confirm"))
        text = (
            "Migration warning (1/2)\n\n"
            "Each source account will be deleted only after its destination and local bot-record update are verified. "
            "Completed users remain migrated if another user fails."
        )
    else:
        markup.add(types.InlineKeyboardButton("🚚 Start Migration", callback_data="bulk:start"))
        text = (
            "Final migration confirmation (2/2)\n\n"
            f"Migrate {context['preflight'].get('eligible', 0)} eligible user(s) from "
            f"{_server_label(context['source'])} to {_server_label(context['destination'])}?"
        )
    markup.add(types.InlineKeyboardButton("Cancel", callback_data="bulk:home"))
    _edit(call, text, markup)


def _start_confirmed_job(call, context=None):
    context = context or _context(call.from_user.id)
    if not context or not context.get("preflight"):
        _edit(call, "This preflight expired. Start again.")
        return
    result = create_transfer_job(
        _spec_from_context(context, call.from_user.id), context["preflight"],
        status_chat_id=call.message.chat.id, status_message_id=call.message.message_id,
    )
    if not result.get("ok"):
        _edit(call, f"Could not start transfer: {result.get('error', 'unknown_error')}", _root_markup(True))
        return
    _drop_context(call.from_user.id)
    _render_status(call.message.chat.id, call.message.message_id, result["job_id"])
    start_transfer_worker()


def _status_text(job, counts, notices):
    lines = [
        f"Transfer {job['job_id'][:10]}",
        "",
        f"Mode: {job['mode']}",
        f"Status: {job['status']}",
        f"Source → destination: {job['source_server_id']} → {job['destination_server_id']}",
        f"Progress: {counts.get('processed', 0)}/{job['total_users']}",
        f"Completed: {counts.get('completed', 0)}",
        f"Skipped: {counts.get('skipped', 0)}",
        f"Failed: {counts.get('failed', 0)}",
        f"Manual review: {counts.get('manual_review', 0)}",
    ]
    if job["mode"] == "migrate":
        lines.append(f"Unmatched panel accounts: {counts.get('unmatched', 0)}")
        lines.append(f"Notification policy: {job['notification_policy']}")
        if notices:
            lines.append("Notifications: " + ", ".join(f"{key}={value}" for key, value in sorted(notices.items())))
    if job.get("last_error"):
        lines.append(f"Paused reason: {job['last_error']}")
    return "\n".join(lines)


def _status_markup(job, counts, notices):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if job["status"] in {"queued", "running"}:
        markup.add(types.InlineKeyboardButton("Cancel after current", callback_data=f"bulk:cancel:{job['job_id']}"))
    if job["status"] in {"cancelled", "failed"} and counts.get("remaining", 0):
        markup.add(types.InlineKeyboardButton("Resume", callback_data=f"bulk:resume:{job['job_id']}"))
    markup.add(
        types.InlineKeyboardButton("Refresh", callback_data=f"bulk:status:{job['job_id']}"),
        types.InlineKeyboardButton("CSV Report", callback_data=f"bulk:csv:{job['job_id']}"),
    )
    if (
        job["notification_policy"] == "deferred"
        and job["status"] not in {"queued", "running", "cancel_requested"}
        and not counts.get("remaining", 0)
        and notices.get("held", 0)
    ):
        markup.add(
            types.InlineKeyboardButton("Send Notifications", callback_data=f"bulk:notifpreview:{job['job_id']}"),
            types.InlineKeyboardButton("Discard Notifications", callback_data=f"bulk:discardpreview:{job['job_id']}"),
        )
    markup.add(types.InlineKeyboardButton("⬅️ Transfer Menu", callback_data="bulk:home"))
    return markup


def _render_status(chat_id, message_id, job_id):
    job = get_job(job_id)
    if job is None:
        return False
    counts = job_counts(job_id)
    notices = notification_counts(job_id)
    safe_edit_message_text(
        bot, _status_text(job, counts, notices), chat_id=chat_id, message_id=message_id,
        reply_markup=_status_markup(job, counts, notices),
    )
    return True


def _update_job_message(job_id):
    job = get_job(job_id)
    if not job or job.get("status_chat_id") is None or job.get("status_message_id") is None:
        return
    try:
        _render_status(int(job["status_chat_id"]), int(job["status_message_id"]), job_id)
    except Exception:
        return


set_progress_callback(_update_job_message)


def _notification_preview(call, job_id, discard=False):
    job = get_job(job_id)
    if job is None or str(job.get("requested_by")) != str(call.from_user.id):
        safe_answer_callback_query(bot, call.id, "Transfer not found.", show_alert=True)
        return
    preview = deferred_recipient_preview(job_id)
    lines = [
        f"{'Discard' if discard else 'Send'} deferred migration notifications?",
        "",
        f"Recipients: {preview['total']}",
    ]
    for recipient in preview["recipients"]:
        lines.append(f"• {recipient['route_scope']} / {recipient['recipient_id']}: {recipient['accounts']} account(s)")
    if preview["total"] > len(preview["recipients"]):
        lines.append(f"• … and {preview['total'] - len(preview['recipients'])} more")
    if discard:
        lines.append("\nDiscarding affects notices only; migrated VPN accounts and records stay unchanged.")
    action = "discard" if discard else "send"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        f"Confirm {'Discard' if discard else 'Send'}", callback_data=f"bulk:notifconfirm:{action}:{job_id}"
    ))
    markup.add(types.InlineKeyboardButton("Back", callback_data=f"bulk:status:{job_id}"))
    _edit(call, "\n".join(lines), markup)


@bot.callback_query_handler(func=lambda call: str(call.data or "").startswith("bulk:"))
def handle_bulk_transfer_callback(call):
    if not is_admin(call.from_user.id):
        safe_answer_callback_query(bot, call.id, "Unauthorized.", show_alert=True)
        return
    safe_answer_callback_query(bot, call.id)
    parts = str(call.data).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "home":
        _drop_context(call.from_user.id)
        text, markup = _root_text()
        _edit(call, text, markup)
    elif action == "new" and len(parts) == 3 and parts[2] in {"copy", "migrate"}:
        _render_sources(call, parts[2])
    elif action == "src" and len(parts) == 3 and parts[2].isdigit():
        _render_destinations(call, int(parts[2]))
    elif action == "sourceagain":
        context = _context(call.from_user.id)
        if context:
            source = context.get("source")
            index = next((i for i, item in enumerate(context.get("servers") or []) if item.get("id") == source.get("id")), 0)
            _render_destinations(call, index)
    elif action == "dst" and len(parts) == 3 and parts[2].isdigit():
        context = _context(call.from_user.id)
        index = int(parts[2])
        if not context or index >= len(context.get("destinations") or []):
            _edit(call, "This selection expired. Start again.")
            return
        context["destination"] = context["destinations"][index]
        if _destination_panel(context["destination"]) == THREE_X_UI_PANEL:
            _render_inbounds(call)
        elif context["mode"] == "migrate":
            _render_notification_policy(call)
        else:
            context["notification_policy"] = "disabled"
            _run_preflight(call)
    elif action == "inb" and len(parts) == 3 and parts[2].isdigit():
        context = _context(call.from_user.id)
        index = int(parts[2])
        if not context or index >= len(context.get("inbound_options") or []):
            _edit(call, "This inbound selection expired. Start again.")
            return
        inbound_id = int(context["inbound_options"][index]["id"])
        selected = context.setdefault("inbound_ids", set())
        selected.remove(inbound_id) if inbound_id in selected else selected.add(inbound_id)
        _render_inbounds(call)
    elif action == "inbdone":
        context = _context(call.from_user.id)
        if not context or not context.get("inbound_ids"):
            safe_answer_callback_query(bot, call.id, "Select at least one Hysteria inbound.", show_alert=True)
        elif context["mode"] == "migrate":
            _render_notification_policy(call)
        else:
            context["notification_policy"] = "disabled"
            _run_preflight(call)
    elif action == "policy" and len(parts) == 3 and parts[2] in {"send", "disabled", "deferred"}:
        context = _context(call.from_user.id)
        if context:
            context["notification_policy"] = parts[2]
            _run_preflight(call)
    elif action == "confirm":
        _render_confirmation(call)
    elif action == "start":
        _start_confirmed_job(call)
    elif action in {"latest", "status"}:
        job_id = parts[2] if action == "status" and len(parts) == 3 else (get_latest_job() or {}).get("job_id")
        if not job_id or not _render_status(call.message.chat.id, call.message.message_id, job_id):
            _edit(call, "No transfer job was found.", _root_markup(False))
    elif action == "cancel" and len(parts) == 3:
        request_cancel(parts[2], call.from_user.id)
        _render_status(call.message.chat.id, call.message.message_id, parts[2])
    elif action == "resume" and len(parts) == 3:
        if not resume_job(parts[2], call.from_user.id):
            safe_answer_callback_query(bot, call.id, "The job cannot be resumed while another transfer is active.", show_alert=True)
        _render_status(call.message.chat.id, call.message.message_id, parts[2])
    elif action == "csv" and len(parts) == 3:
        job = get_job(parts[2])
        if not job or str(job.get("requested_by")) != str(call.from_user.id):
            safe_answer_callback_query(bot, call.id, "Transfer not found.", show_alert=True)
            return
        content = export_job_csv(parts[2])
        document = io.BytesIO(content or b"")
        document.name = f"bulk_transfer_{parts[2][:10]}.csv"
        bot.send_document(call.message.chat.id, document, caption="Admin-only transfer report")
    elif action == "notifpreview" and len(parts) == 3:
        _notification_preview(call, parts[2], discard=False)
    elif action == "discardpreview" and len(parts) == 3:
        _notification_preview(call, parts[2], discard=True)
    elif action == "notifconfirm" and len(parts) == 4 and parts[2] in {"send", "discard"}:
        if not decide_deferred_notifications(parts[3], call.from_user.id, parts[2]):
            safe_answer_callback_query(bot, call.id, "Notification decision is no longer available.", show_alert=True)
        _render_status(call.message.chat.id, call.message.message_id, parts[3])
    else:
        safe_answer_callback_query(bot, call.id, "Invalid transfer action.", show_alert=True)
