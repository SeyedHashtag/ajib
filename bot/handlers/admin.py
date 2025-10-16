from __future__ import annotations

"""
Admin handlers (skeleton) for:
- Backup (create and restore)
- Broadcast (select audience and send)
- Plan management (list, add, edit)

Notes:
- This is a minimal skeleton to outline admin flows.
- Real integrations (DB, Blitz API, Heleket Webhooks) should be added in services.
- Blocking IO (tar/json) is kept simple; consider offloading to background workers if needed.
"""

import json
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiogram import Router, F
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)

from ..core.config import load_config, Config

__all__ = ["router"]

router = Router(name="admin_ext")


# ==============
# Helpers & i18n
# ==============

# In-memory language preference for admins (skeleton)
_admin_lang: Dict[int, str] = {}

TEXTS: Dict[str, Dict[str, str]] = {
    "en": {
        # generic
        "welcome_admin": "Admin panel. Choose an option.",
        "unknown_action": "Unknown action. Use the admin menu.",
        "back": "⬅️ Back",
        "cancel": "✖️ Cancel",
        # admin main
        "menu_backup": "🧰 Backup/Restore",
        "menu_broadcast": "📣 Broadcast",
        "menu_plans": "💲 Plans",
        "menu_close": "🔚 Close",
        # backup
        "backup_menu_title": "Backup/Restore",
        "backup_create": "🧩 Create Backup",
        "backup_restore": "🧿 Restore",
        "backup_created": "Backup created.",
        "backup_failed": "Backup failed.",
        "backup_sending": "Sending backup file...",
        "backup_send_failed": "Could not send backup file.",
        "restore_send_file": "Send a backup .tar.gz file to restore.",
        "restore_received": "Backup file received. Restore process is a stub in this skeleton.",
        "restore_failed": "Failed to accept or process the backup file.",
        # broadcast
        "broadcast_title": "Broadcast — choose audience:",
        "audience_active": "Active",
        "audience_expired": "Expired",
        "audience_test": "Test",
        "audience_all": "All",
        "broadcast_enter_text": "Send the message to broadcast to {audience} users.",
        "broadcast_sending": "Broadcast queued to {audience}. (skeleton)",
        "broadcast_cancelled": "Broadcast cancelled.",
        # plans
        "plans_title": "Plans — choose:",
        "plans_list": "📃 List Plans",
        "plans_add": "➕ Add Plan",
        "plans_edit": "✏️ Edit Plan",
        "plans_none": "No plans configured.",
        "plans_list_header": "Configured plans:",
        "plans_add_prompt": (
            "Send plan JSON, for example:\n"
            '{"id":"basic","name":"Basic","price":5.0,"duration_days":30,"data_gb":50,"backend_plan_id":"BASIC_30"}'
        ),
        "plans_add_ok": "Plan added.",
        "plans_add_err": "Invalid JSON or write error.",
        "plans_edit_prompt_id": "Send the plan 'id' you want to edit.",
        "plans_edit_prompt_json": "Send JSON to merge into the plan (partial fields allowed).",
        "plans_edit_ok": "Plan updated.",
        "plans_edit_err": "Plan not found or invalid JSON.",
        "plans_edit_cancel": "Edit cancelled.",
    },
    "fa": {
        # generic
        "welcome_admin": "پنل ادمین. یکی از گزینه‌ها را انتخاب کنید.",
        "unknown_action": "دستور نامشخص. از منوی ادمین استفاده کنید.",
        "back": "⬅️ بازگشت",
        "cancel": "✖️ لغو",
        # admin main
        "menu_backup": "🧰 بکاپ/بازیابی",
        "menu_broadcast": "📣 ارسال همگانی",
        "menu_plans": "💲 پلن‌ها",
        "menu_close": "🔚 خروج",
        # backup
        "backup_menu_title": "بکاپ/بازیابی",
        "backup_create": "🧩 ساخت بکاپ",
        "backup_restore": "🧿 بازیابی",
        "backup_created": "بکاپ ایجاد شد.",
        "backup_failed": "ایجاد بکاپ ناموفق بود.",
        "backup_sending": "در حال ارسال فایل بکاپ...",
        "backup_send_failed": "ارسال فایل بکاپ ممکن نشد.",
        "restore_send_file": "فایل بکاپ .tar.gz را برای بازیابی ارسال کنید.",
        "restore_received": "فایل بکاپ دریافت شد. فرآیند بازیابی در این نسخه آزمایشی است.",
        "restore_failed": "دریافت یا پردازش فایل بکاپ ناموفق بود.",
        # broadcast
        "broadcast_title": "ارسال همگانی — مخاطب را انتخاب کنید:",
        "audience_active": "فعال",
        "audience_expired": "منقضی",
        "audience_test": "تست",
        "audience_all": "همه",
        "broadcast_enter_text": "پیام خود را برای ارسال به کاربران {audience} ارسال کنید.",
        "broadcast_sending": "ارسال همگانی به {audience} در صف قرار گرفت. (آزمایشی)",
        "broadcast_cancelled": "ارسال همگانی لغو شد.",
        # plans
        "plans_title": "پلن‌ها — انتخاب کنید:",
        "plans_list": "📃 لیست پلن‌ها",
        "plans_add": "➕ افزودن پلن",
        "plans_edit": "✏️ ویرایش پلن",
        "plans_none": "هیچ پلنی تنظیم نشده است.",
        "plans_list_header": "پلن‌های تنظیم‌شده:",
        "plans_add_prompt": (
            "پلن را به صورت JSON ارسال کنید، مثلا:\n"
            '{"id":"basic","name":"Basic","price":5.0,"duration_days":30,"data_gb":50,"backend_plan_id":"BASIC_30"}'
        ),
        "plans_add_ok": "پلن اضافه شد.",
        "plans_add_err": "JSON نامعتبر یا خطا در ذخیره‌سازی.",
        "plans_edit_prompt_id": "شناسه (id) پلنی که می‌خواهید ویرایش کنید را ارسال کنید.",
        "plans_edit_prompt_json": "JSON را برای اعمال تغییرات ارسال کنید (ارسال بخشی از فیلدها مجاز است).",
        "plans_edit_ok": "پلن بروزرسانی شد.",
        "plans_edit_err": "پلن یافت نشد یا JSON نامعتبر.",
        "plans_edit_cancel": "ویرایش لغو شد.",
    },
}


def _get_cfg(message: Message) -> Config:
    """Retrieve Config instance, cached on bot object for reuse."""
    if hasattr(message.bot, "__dict__") and "cfg" in message.bot.__dict__:
        return message.bot.__dict__["cfg"]
    cfg = load_config()
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
    uid = message.from_user.id if message.from_user else 0
    if uid in _admin_lang:
        return _admin_lang[uid]
    if cfg is None:
        cfg = _get_cfg(message)
    return cfg.default_locale if cfg.default_locale in ("en", "fa") else "en"


# ==============
# Admin filtering
# ==============


class AdminOnlyFilter(BaseFilter):
    """Allow only configured admins to interact."""

    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        cfg = _get_cfg(message)
        return message.from_user.id in cfg.admin_ids


router.message.filter(AdminOnlyFilter())


# ============
# Keyboards
# ============


def _kb_admin_main(lang: str) -> ReplyKeyboardMarkup:
    L = lambda k: _t(lang, k)
    keyboard = [
        [
            KeyboardButton(text=L("menu_backup")),
            KeyboardButton(text=L("menu_broadcast")),
        ],
        [KeyboardButton(text=L("menu_plans")), KeyboardButton(text=L("menu_close"))],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _kb_backup(lang: str) -> ReplyKeyboardMarkup:
    L = lambda k: _t(lang, k)
    keyboard = [
        [
            KeyboardButton(text=L("backup_create")),
            KeyboardButton(text=L("backup_restore")),
        ],
        [KeyboardButton(text=L("back"))],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _kb_broadcast_audience(lang: str) -> ReplyKeyboardMarkup:
    L = lambda k: _t(lang, k)
    keyboard = [
        [
            KeyboardButton(text=L("audience_active")),
            KeyboardButton(text=L("audience_expired")),
        ],
        [
            KeyboardButton(text=L("audience_test")),
            KeyboardButton(text=L("audience_all")),
        ],
        [KeyboardButton(text=L("cancel"))],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _kb_plans_menu(lang: str) -> ReplyKeyboardMarkup:
    L = lambda k: _t(lang, k)
    keyboard = [
        [KeyboardButton(text=L("plans_list")), KeyboardButton(text=L("plans_add"))],
        [KeyboardButton(text=L("plans_edit"))],
        [KeyboardButton(text=L("back"))],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# ============
# States (FSM)
# ============


class BroadcastStates(StatesGroup):
    choosing_audience = State()
    entering_message = State()


class RestoreStates(StatesGroup):
    awaiting_file = State()


class PlansStates(StatesGroup):
    adding_json = State()
    editing_id = State()
    editing_json = State()


# =====================
# Admin main entrypoint
# =====================


@router.message(Command("admin"))
async def admin_menu_cmd(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "welcome_admin"), reply_markup=_kb_admin_main(lang))


# =========
# Backup UI
# =========


@router.message(F.text.in_({_t("en", "menu_backup"), _t("fa", "menu_backup")}))
async def backup_menu(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "backup_menu_title"), reply_markup=_kb_backup(lang))


@router.message(F.text.in_({_t("en", "backup_create"), _t("fa", "backup_create")}))
async def backup_create(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)

    # Prepare backup directory
    backups_dir = Path(cfg.data_dir) / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_path = backups_dir / f"ajib-backup-{ts}.tar.gz"

    # Compose backup members
    members: List[Tuple[Path, str]] = []
    if cfg.env_path and Path(cfg.env_path).is_file():
        members.append((Path(cfg.env_path), "env/.env"))
    if Path(cfg.sqlite_path).is_file():
        members.append((Path(cfg.sqlite_path), "db/ajib.sqlite3"))

    try:
        with tarfile.open(backup_path, mode="w:gz") as tf:
            for src, arc in members:
                tf.add(str(src), arcname=arc)
        await message.answer(_t(lang, "backup_created"))
        await message.answer(_t(lang, "backup_sending"))
        try:
            await message.answer_document(
                FSInputFile(str(backup_path)),
                caption=str(backup_path),
            )
        except Exception:
            await message.answer(_t(lang, "backup_send_failed"))
    except Exception:
        await message.answer(_t(lang, "backup_failed"))


@router.message(F.text.in_({_t("en", "backup_restore"), _t("fa", "backup_restore")}))
async def restore_prompt(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.set_state(RestoreStates.awaiting_file)
    await message.answer(_t(lang, "restore_send_file"))


@router.message(RestoreStates.awaiting_file, F.document)
async def restore_receive_file(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    try:
        backups_dir = Path(cfg.data_dir) / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        dest = backups_dir / "restore-uploaded.tar.gz"

        # Download the file (best-effort; API may differ by aiogram version)
        # Attempt via Bot API helper:
        try:
            file = await message.bot.get_file(message.document.file_id)
            await message.bot.download(file, destination=dest)
        except Exception:
            # Fallback: try direct method if available
            try:
                await message.document.download(destination=dest)  # type: ignore[attr-defined]
            except Exception:
                # Give up but still acknowledge receipt in skeleton
                pass

        await message.answer(_t(lang, "restore_received"))
    except Exception:
        await message.answer(_t(lang, "restore_failed"))
    finally:
        await state.clear()


# =============
# Broadcast UI
# =============


def _audience_labels() -> set[str]:
    return {
        _t("en", "audience_active"),
        _t("fa", "audience_active"),
        _t("en", "audience_expired"),
        _t("fa", "audience_expired"),
        _t("en", "audience_test"),
        _t("fa", "audience_test"),
        _t("en", "audience_all"),
        _t("fa", "audience_all"),
    }


@router.message(F.text.in_({_t("en", "menu_broadcast"), _t("fa", "menu_broadcast")}))
async def broadcast_start(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.set_state(BroadcastStates.choosing_audience)
    await message.answer(_t(lang, "broadcast_title"), reply_markup=_kb_broadcast_audience(lang))


@router.message(BroadcastStates.choosing_audience, F.text.in_(_audience_labels()))
async def broadcast_audience_selected(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    audience = message.text.strip()
    await state.update_data(audience=audience)
    await state.set_state(BroadcastStates.entering_message)
    await message.answer(_t(lang, "broadcast_enter_text", audience=audience))


@router.message(
    BroadcastStates.choosing_audience,
    F.text.in_({_t("en", "cancel"), _t("fa", "cancel")}),
)
@router.message(
    BroadcastStates.entering_message,
    F.text.in_({_t("en", "cancel"), _t("fa", "cancel")}),
)
async def broadcast_cancel(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.clear()
    await message.answer(_t(lang, "broadcast_cancelled"), reply_markup=_kb_admin_main(lang))


@router.message(BroadcastStates.entering_message, F.text)
async def broadcast_enter_message(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    data = await state.get_data()
    audience = data.get("audience", _t(lang, "audience_all"))
    text_to_send = message.text

    # Skeleton: here we would query DB to get user segments and send messages in batches.
    # For now, we only acknowledge.
    _ = text_to_send  # placeholder to satisfy linters
    await message.answer(_t(lang, "broadcast_sending", audience=audience))

    # Clear state and go back to admin menu
    await state.clear()
    await message.answer(_t(lang, "welcome_admin"), reply_markup=_kb_admin_main(lang))


# ==============
# Plans management
# ==============


@dataclass
class Plan:
    id: str
    name: str
    price: float
    duration_days: int
    data_gb: int
    backend_plan_id: str


def _plans_path(cfg: Config) -> Path:
    plans_dir = Path(cfg.data_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir / "plans.json"


def _load_plans(cfg: Config) -> List[Dict]:
    p = _plans_path(cfg)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_plans(cfg: Config, plans: List[Dict]) -> bool:
    p = _plans_path(cfg)
    try:
        p.write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


@router.message(F.text.in_({_t("en", "menu_plans"), _t("fa", "menu_plans")}))
async def plans_menu(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "plans_title"), reply_markup=_kb_plans_menu(lang))


@router.message(F.text.in_({_t("en", "plans_list"), _t("fa", "plans_list")}))
async def plans_list(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    plans = _load_plans(cfg)
    if not plans:
        await message.answer(_t(lang, "plans_none"), reply_markup=_kb_plans_menu(lang))
        return
    lines = [_t(lang, "plans_list_header")]
    for pl in plans:
        lines.append(
            f"- id={pl.get('id')} name={pl.get('name')} price={pl.get('price')} "
            f"duration_days={pl.get('duration_days')} data_gb={pl.get('data_gb')} "
            f"backend_plan_id={pl.get('backend_plan_id')}"
        )
    await message.answer("\n".join(lines), reply_markup=_kb_plans_menu(lang))


@router.message(F.text.in_({_t("en", "plans_add"), _t("fa", "plans_add")}))
async def plans_add_start(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.set_state(PlansStates.adding_json)
    await message.answer(
        _t(lang, "plans_add_prompt"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=_t(lang, "cancel"))]], resize_keyboard=True
        ),
    )


@router.message(PlansStates.adding_json, F.text.in_({_t("en", "cancel"), _t("fa", "cancel")}))
async def plans_add_cancel(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.clear()
    await message.answer(_t(lang, "plans_edit_cancel"), reply_markup=_kb_plans_menu(lang))


@router.message(PlansStates.adding_json, F.text)
async def plans_add_receive_json(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    try:
        obj = json.loads(message.text)
        required = {
            "id",
            "name",
            "price",
            "duration_days",
            "data_gb",
            "backend_plan_id",
        }
        if not required.issubset(set(obj.keys())):
            raise ValueError("missing fields")
        plans = _load_plans(cfg)
        # Replace if exists with same id; else append
        plans = [p for p in plans if p.get("id") != obj["id"]]
        plans.append(obj)
        if not _save_plans(cfg, plans):
            raise RuntimeError("write failed")
        await message.answer(_t(lang, "plans_add_ok"), reply_markup=_kb_plans_menu(lang))
    except Exception:
        await message.answer(_t(lang, "plans_add_err"), reply_markup=_kb_plans_menu(lang))
    finally:
        await state.clear()


@router.message(F.text.in_({_t("en", "plans_edit"), _t("fa", "plans_edit")}))
async def plans_edit_start(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.set_state(PlansStates.editing_id)
    await message.answer(
        _t(lang, "plans_edit_prompt_id"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=_t(lang, "cancel"))]], resize_keyboard=True
        ),
    )


@router.message(PlansStates.editing_id, F.text.in_({_t("en", "cancel"), _t("fa", "cancel")}))
async def plans_edit_cancel(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.clear()
    await message.answer(_t(lang, "plans_edit_cancel"), reply_markup=_kb_plans_menu(lang))


@router.message(PlansStates.editing_id, F.text)
async def plans_edit_receive_id(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    plan_id = message.text.strip()
    plans = _load_plans(cfg)
    target = next((p for p in plans if p.get("id") == plan_id), None)
    if not target:
        await state.clear()
        await message.answer(_t(lang, "plans_edit_err"), reply_markup=_kb_plans_menu(lang))
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(PlansStates.editing_json)
    await message.answer(
        _t(lang, "plans_edit_prompt_json"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=_t(lang, "cancel"))]], resize_keyboard=True
        ),
    )


@router.message(PlansStates.editing_json, F.text.in_({_t("en", "cancel"), _t("fa", "cancel")}))
async def plans_edit_json_cancel(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await state.clear()
    await message.answer(_t(lang, "plans_edit_cancel"), reply_markup=_kb_plans_menu(lang))


@router.message(PlansStates.editing_json, F.text)
async def plans_edit_receive_json(message: Message, state: FSMContext) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    try:
        patch = json.loads(message.text)
        data = await state.get_data()
        plan_id = data.get("plan_id")
        if not plan_id:
            raise ValueError("missing plan id")

        plans = _load_plans(cfg)
        updated = False
        for p in plans:
            if p.get("id") == plan_id:
                p.update(patch)
                updated = True
                break

        if not updated or not _save_plans(cfg, plans):
            raise RuntimeError("update failed")

        await message.answer(_t(lang, "plans_edit_ok"), reply_markup=_kb_plans_menu(lang))
    except Exception:
        await message.answer(_t(lang, "plans_edit_err"), reply_markup=_kb_plans_menu(lang))
    finally:
        await state.clear()


# =========================
# Navigation and fallbacks
# =========================


@router.message(F.text.in_({_t("en", "back"), _t("fa", "back")}))
async def back_to_admin_main(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "welcome_admin"), reply_markup=_kb_admin_main(lang))


@router.message(F.text.in_({_t("en", "menu_close"), _t("fa", "menu_close")}))
async def admin_close(message: Message) -> None:
    # Minimal close action: just acknowledge
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "menu_close"))


@router.message(F.text)
async def admin_fallback(message: Message) -> None:
    cfg = _get_cfg(message)
    lang = _get_lang(message, cfg)
    await message.answer(_t(lang, "unknown_action"), reply_markup=_kb_admin_main(lang))
