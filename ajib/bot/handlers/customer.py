from __future__ import annotations

"""
Customer handlers and minimal admin stubs (skeletons).

This module defines:
- `router`: customer-facing router with a reply keyboard-driven UX.
  - View active configs
  - Purchase
  - Get 1GB test config
  - Client download links
  - Contact support
  - Language switch (English / Persian)
- Minimal admin broadcast/backup placeholders are described but administered in a dedicated admin module.

Notes:
- This is a minimal skeleton: it returns placeholder messages and uses simple in-memory language preferences.
- Integration with Blitz backend and Heleket crypto must be added in services modules.
- i18n here is a compact dictionary for core flows; prefer using proper locale files as the project grows.
"""

from typing import Dict, Optional, Set

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from ..core.config import load_config, Config

__all__ = ["router"]

router = Router(name="customer")

# In-memory language preference (per user). For production, persist this.
_user_lang: Dict[int, str] = {}

# Simple i18n dictionary (skeleton). Replace with proper locale files later.
TEXTS: Dict[str, Dict[str, str]] = {
    "en": {
        "menu_title": "Main Menu",
        "btn_configs": "🧾 My Configs",
        "btn_purchase": "🛒 Purchase",
        "btn_test": "🎁 1GB Test",
        "btn_downloads": "💻 Client Downloads",
        "btn_support": "🆘 Support",
        "btn_language": "🌐 Language",
        "btn_back": "⬅️ Back",
        "lang_title": "Select your language",
        "lang_en": "English",
        "lang_fa": "فارسی",
        "welcome": "Choose an option from the menu below.",
        "configs_loading": "Fetching your active configs...",
        "configs_empty": "You have no active configs yet.",
        "purchase_intro": "Purchase flow is under construction.",
        "test_intro": "A 1GB test config will be provided here. (Coming soon)",
        "downloads_title": "Client download links:",
        "downloads_item": "• {os}: {url}",
        "downloads_empty": "No client download links have been configured yet.",
        "support_title": "Support",
        "support_contact": "Contact support: {contact}",
        "support_unset": "Support contact has not been configured.",
        "lang_switched": "Language set to English.",
        "lang_switched_fa": "زبان به فارسی تغییر کرد.",
        "unknown_action": "Unknown action. Use the menu.",
    },
    "fa": {
        "menu_title": "منوی اصلی",
        "btn_configs": "🧾 کانفیگ‌های من",
        "btn_purchase": "🛒 خرید",
        "btn_test": "🎁 تست ۱ گیگ",
        "btn_downloads": "💻 دانلود کلاینت",
        "btn_support": "🆘 پشتیبانی",
        "btn_language": "🌐 زبان",
        "btn_back": "⬅️ بازگشت",
        "lang_title": "زبان خود را انتخاب کنید",
        "lang_en": "English",
        "lang_fa": "فارسی",
        "welcome": "یکی از گزینه‌های زیر را انتخاب کنید.",
        "configs_loading": "در حال دریافت کانفیگ‌های فعال شما...",
        "configs_empty": "شما در حال حاضر کانفیگ فعالی ندارید.",
        "purchase_intro": "فرایند خرید به زودی اضافه می‌شود.",
        "test_intro": "کانفیگ تست ۱ گیگ به زودی ارائه می‌شود.",
        "downloads_title": "لینک‌های دانلود کلاینت:",
        "downloads_item": "• {os}: {url}",
        "downloads_empty": "لینک‌های دانلود کلاینت پیکربندی نشده‌اند.",
        "support_title": "پشتیبانی",
        "support_contact": "ارتباط با پشتیبانی: {contact}",
        "support_unset": "راه ارتباط با پشتیبانی تنظیم نشده است.",
        "lang_switched": "Language set to English.",
        "lang_switched_fa": "زبان به فارسی تغییر کرد.",
        "unknown_action": "دستور نامشخص. از منو استفاده کنید.",
    },
}


def _get_cfg(message: Message) -> Config:
    """
    Retrieve Config instance.
    If not injected in bot context, lazily load and attach for reuse.
    """
    # Bot context can be used to store shared data
    if hasattr(message.bot, "__dict__") and "cfg" in message.bot.__dict__:
        return message.bot.__dict__["cfg"]
    cfg = load_config()
    # Cache on bot instance for subsequent handlers
    message.bot.__dict__["cfg"] = cfg
    return cfg


def _t(lang: str, key: str, **kwargs) -> str:
    """Translate helper with fallback to English."""
    base = TEXTS.get(lang) or TEXTS["en"]
    text = base.get(key) or TEXTS["en"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


def _get_lang(message: Message, cfg: Optional[Config] = None) -> str:
    """Get user-selected language or default."""
    uid = message.from_user.id if message.from_user else 0
    if uid in _user_lang:
        return _user_lang[uid]
    if cfg is None:
        cfg = _get_cfg(message)
    return cfg.default_locale if cfg.default_locale in ("en", "fa") else "en"


def _set_lang(message: Message, lang: str) -> None:
    """Set user language in memory."""
    if not message.from_user:
        return
    if lang not in ("en", "fa"):
        return
    _user_lang[message.from_user.id] = lang


def _main_menu_kb(lang: str) -> ReplyKeyboardMarkup:
    """Build main reply keyboard."""
    L = lambda k: _t(lang, k)
    # Two rows + language row
    keyboard = [
        [KeyboardButton(text=L("btn_configs")), KeyboardButton(text=L("btn_purchase"))],
        [KeyboardButton(text=L("btn_test")), KeyboardButton(text=L("btn_downloads"))],
        [KeyboardButton(text=L("btn_support")), KeyboardButton(text=L("btn_language"))],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False
    )


def _language_kb(lang: str) -> ReplyKeyboardMarkup:
    """Language selection keyboard."""
    L = lambda k: _t(lang, k)
    keyboard = [
        [KeyboardButton(text=L("lang_en")), KeyboardButton(text=L("lang_fa"))],
        [KeyboardButton(text=L("btn_back"))],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False
    )


def _is_action_text(text: Optional[str], action_keys: Set[str]) -> bool:
    """Check if message text matches one of the given action labels."""
    if not text:
        return False
    normalized = text.strip().lower()
    return normalized in {a.strip().lower() for a in action_keys}


def _labels_for(action: str) -> Set[str]:
    """Return a set of labels across locales for the given action key."""
    mapping = {
        "configs": {"en": "btn_configs", "fa": "btn_configs"},
        "purchase": {"en": "btn_purchase", "fa": "btn_purchase"},
        "test": {"en": "btn_test", "fa": "btn_test"},
        "downloads": {"en": "btn_downloads", "fa": "btn_downloads"},
        "support": {"en": "btn_support", "fa": "btn_support"},
        "language": {"en": "btn_language", "fa": "btn_language"},
        "back": {"en": "btn_back", "fa": "btn_back"},
        "lang_en": {"en": "lang_en", "fa": "lang_en"},
        "lang_fa": {"en": "lang_fa", "fa": "lang_fa"},
    }
    labels: Set[str] = set()
    if action not in mapping:
        return labels
    for loc, key in mapping[action].items():
        labels.add(TEXTS[loc][key])
    return labels


@router.message(Command("menu"))
async def show_menu(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "welcome"), reply_markup=_main_menu_kb(lang=lang))


# My Configs
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("configs"))))
@router.message(Command("configs"))
async def handle_configs(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "configs_loading"))
    # TODO: integrate with Blitz API to fetch configs for this user.
    # For now, a placeholder:
    await message.answer(_t(lang, "configs_empty"), reply_markup=_main_menu_kb(lang))


# Purchase
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("purchase"))))
@router.message(Command("purchase"))
async def handle_purchase(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    # TODO: Show plans (fetch from backend), create Heleket invoice, poll status, then provision.
    await message.answer(_t(lang, "purchase_intro"), reply_markup=_main_menu_kb(lang))


# 1GB Test
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("test"))))
@router.message(Command("test"))
async def handle_test(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    # TODO: Enforce eligibility, request test from backend, deliver config.
    await message.answer(_t(lang, "test_intro"), reply_markup=_main_menu_kb(lang))


# Client Downloads
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("downloads"))))
@router.message(Command("downloads"))
async def handle_downloads(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)

    if not cfg.client_dl_urls:
        await message.answer(
            _t(lang, "downloads_empty"), reply_markup=_main_menu_kb(lang)
        )
        return

    lines = [_t(lang, "downloads_title")]
    # Format e.g., • android: https://...
    for os_key, url in cfg.client_dl_urls.items():
        lines.append(_t(lang, "downloads_item", os=os_key.capitalize(), url=url))

    await message.answer("\n".join(lines), reply_markup=_main_menu_kb(lang))


# Support
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("support"))))
@router.message(Command("support"))
async def handle_support(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)

    if cfg.support_contact:
        await message.answer(
            _t(lang, "support_contact", contact=cfg.support_contact),
            reply_markup=_main_menu_kb(lang),
        )
    else:
        await message.answer(
            _t(lang, "support_unset"), reply_markup=_main_menu_kb(lang)
        )


# Language menu
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("language"))))
@router.message(Command("language"))
async def handle_language(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "lang_title"), reply_markup=_language_kb(lang))


# Language selection
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("lang_en"))))
async def set_lang_en(message: Message) -> None:
    _set_lang(message, "en")
    lang = "en"
    await message.answer(_t(lang, "lang_switched"), reply_markup=_main_menu_kb(lang))


@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("lang_fa"))))
async def set_lang_fa(message: Message) -> None:
    _set_lang(message, "fa")
    lang = "fa"
    await message.answer(_t(lang, "lang_switched_fa"), reply_markup=_main_menu_kb(lang))


# Back from language menu
@router.message(F.text.func(lambda t: _is_action_text(t, _labels_for("back"))))
async def back_to_menu(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "welcome"), reply_markup=_main_menu_kb(lang))


# Fallback for unknown text (optional, keep low priority by placing last)
@router.message(F.text)
async def fallback_unknown(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "unknown_action"), reply_markup=_main_menu_kb(lang))
