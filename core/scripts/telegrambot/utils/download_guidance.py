import logging

from telebot import types

from utils.translations import get_message_text


DOWNLOAD_CATALOG = {
    "ios": (
        {
            "id": "karing",
            "label_key": "download_karing_recommended",
            "url": "https://apps.apple.com/us/app/karing/id6472431552",
            "details_key": "download_karing_ios_tutorial",
        },
        {
            "id": "happ",
            "label_key": "download_happ",
            "url": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
            "details_key": "download_happ_ios_details",
        },
    ),
    "android": (
        {
            "id": "v2ray",
            "label": "v2rayNG",
            "url": "https://github.com/2dust/v2rayNG/releases/latest",
            "details_key": "download_v2rayng_android_details",
        },
    ),
    "windows": (
        {
            "id": "v2ray",
            "label": "v2rayN",
            "url": "https://github.com/2dust/v2rayN/releases/latest",
            "details_key": "download_v2rayn_windows_details",
        },
    ),
}

PLATFORM_LABELS = {
    "ios": "📱 iOS",
    "android": "📱 Android",
    "windows": "💻 Windows",
}


def _callback(prefix, *parts):
    return ":".join((prefix, *parts))


def _app_for(platform, app_id):
    return next(
        (app for app in DOWNLOAD_CATALOG.get(platform, ()) if app["id"] == app_id),
        None,
    )


def build_platform_markup(callback_prefix="download"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for platform in DOWNLOAD_CATALOG:
        markup.add(types.InlineKeyboardButton(
            PLATFORM_LABELS[platform],
            callback_data=_callback(callback_prefix, platform),
        ))
    return markup


def build_app_markup(language, platform, callback_prefix="download"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for app in DOWNLOAD_CATALOG.get(platform, ()):
        label = app.get("label") or get_message_text(language, app["label_key"])
        markup.add(types.InlineKeyboardButton(
            label,
            callback_data=_callback(callback_prefix, "app", app["id"], platform),
        ))
    markup.add(types.InlineKeyboardButton(
        get_message_text(language, "download_back_platforms"),
        callback_data=_callback(callback_prefix, "back"),
    ))
    return markup


def build_app_details_markup(language, platform, app_id, callback_prefix="download"):
    app = _app_for(platform, app_id)
    if app is None:
        return None
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        get_message_text(language, "download_open_link"),
        url=app["url"],
    ))
    markup.add(types.InlineKeyboardButton(
        get_message_text(language, "download_back_apps"),
        callback_data=_callback(callback_prefix, platform),
    ))
    return markup


def get_app_list_text(language, platform):
    key = f"download_{platform}_app_list"
    return get_message_text(language, key)


def get_app_details_text(language, platform, app_id):
    app = _app_for(platform, app_id)
    if app is None:
        return None
    return get_message_text(language, app["details_key"])


def parse_download_callback(data, callback_prefix="download"):
    prefix = f"{callback_prefix}:"
    if not isinstance(data, str) or not data.startswith(prefix):
        return None
    parts = data[len(prefix):].split(":")
    if parts == ["back"]:
        return {"action": "back"}
    if len(parts) == 1 and parts[0] in DOWNLOAD_CATALOG:
        return {"action": "platform", "platform": parts[0]}
    if len(parts) == 3 and parts[0] == "app":
        app_id, platform = parts[1], parts[2]
        if _app_for(platform, app_id) is not None:
            return {"action": "app", "platform": platform, "app_id": app_id}
    return None


def send_download_prompt(bot, chat_id, language, callback_prefix="download", reply_to=None):
    kwargs = {
        "reply_markup": build_platform_markup(callback_prefix),
    }
    text = get_message_text(language, "select_platform")
    if reply_to is not None:
        return bot.reply_to(reply_to, text, **kwargs)
    return bot.send_message(chat_id, text, **kwargs)


def send_download_prompt_safely(bot, chat_id, language, callback_prefix="download"):
    try:
        return send_download_prompt(bot, chat_id, language, callback_prefix=callback_prefix)
    except Exception as error:
        logging.getLogger("ajib.downloads").warning(
            "Could not send download guidance to chat %s: %s",
            chat_id,
            type(error).__name__,
        )
        return None


def render_download_callback(bot, call, language, callback_prefix="download"):
    selection = parse_download_callback(call.data, callback_prefix)
    if selection is None:
        bot.answer_callback_query(
            call.id,
            text=get_message_text(language, "download_invalid_selection"),
            show_alert=True,
        )
        return False

    bot.answer_callback_query(call.id)
    common = {
        "chat_id": call.message.chat.id,
        "message_id": call.message.message_id,
    }
    if selection["action"] == "back":
        bot.edit_message_text(
            get_message_text(language, "select_platform"),
            reply_markup=build_platform_markup(callback_prefix),
            **common,
        )
        return True

    platform = selection["platform"]
    if selection["action"] == "platform":
        bot.edit_message_text(
            get_app_list_text(language, platform),
            reply_markup=build_app_markup(language, platform, callback_prefix),
            parse_mode="Markdown",
            **common,
        )
        return True

    app_id = selection["app_id"]
    bot.edit_message_text(
        get_app_details_text(language, platform, app_id),
        reply_markup=build_app_details_markup(language, platform, app_id, callback_prefix),
        parse_mode="Markdown",
        disable_web_page_preview=True,
        **common,
    )
    return True
