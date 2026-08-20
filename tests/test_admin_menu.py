import importlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest


COMMON_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "common.py"
)


def load_common():
    existing_telebot = sys.modules.pop("telebot", None)
    try:
        importlib.import_module("telebot")
        spec = importlib.util.spec_from_file_location("admin_menu_common_under_test", COMMON_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop("telebot", None)
        if existing_telebot is not None:
            sys.modules["telebot"] = existing_telebot


common = load_common()

EXPECTED_ROOT_ROWS = (
    ("✅ Confirmations", "📊 Server Info"),
    ("👥 Users", "💳 Sales"),
    ("💼 Resellers", "⚙️ System"),
    ("📊 Reports", "📣 Messaging"),
)

EXPECTED_GROUP_ROWS = {
    "users": (
        ("➕ Add User", "👤 Show User"),
        ("❌ Delete User", "🧪 Manage Test Accounts"),
        ("🔁 Mass Copy / Migrate",),
        ("🧹 Expired Cleanup",),
        ("🏠 Admin Menu",),
    ),
    "sales": (
        ("✅ Confirmations", "💳 Payment Settings"),
        ("📝 Edit Plans", "💰 Referral Payouts"),
        ("🏠 Admin Menu",),
    ),
    "resellers": (
        ("💼 Manage Resellers", "🤖 Hosted Bots"),
        ("🏠 Admin Menu",),
    ),
    "system": (
        ("⚖️ VPN Servers", "💾 Backup Bot"),
        ("📄 Bot Logs",),
        ("🏠 Admin Menu",),
    ),
    "reports": (
        ("📊 Server Info", "📈 Growth Funnel"),
        ("🏠 Admin Menu",),
    ),
    "messaging": (
        ("📢 Broadcast Message", "📞 Edit Support"),
        ("🔄 Update Keyboards",),
        ("🏠 Admin Menu",),
    ),
}

EXPECTED_ACTION_KEYS = {
    "add_user",
    "show_user",
    "delete_user",
    "server_info",
    "backup_bot",
    "payment_settings",
    "edit_plans",
    "broadcast_message",
    "edit_support",
    "update_keyboards",
    "manage_resellers",
    "manage_test_accounts",
    "referral_payouts",
    "vpn_servers",
    "confirmations",
    "expired_cleanup",
    "bulk_transfer",
    "bot_logs",
    "hosted_bots",
    "growth_funnel",
}


def serialized_keyboard(view):
    return json.loads(common.create_admin_markup(view).to_json())


def keyboard_text_rows(view):
    keyboard = serialized_keyboard(view)["keyboard"]
    return tuple(tuple(button["text"] for button in row) for row in keyboard)


def test_admin_menu_row_contract_and_main_alias():
    assert common.ADMIN_ROOT_MENU_ROWS == EXPECTED_ROOT_ROWS
    assert common.ADMIN_MAIN_MENU_ROWS is common.ADMIN_ROOT_MENU_ROWS
    assert common.ADMIN_GROUP_MENU_ROWS == EXPECTED_GROUP_ROWS

    assert keyboard_text_rows("root") == EXPECTED_ROOT_ROWS
    for view, expected_rows in EXPECTED_GROUP_ROWS.items():
        assert keyboard_text_rows(view) == expected_rows

    assert serialized_keyboard("root")["resize_keyboard"] is True
    with pytest.raises(ValueError, match="Unknown admin menu view"):
        common.create_admin_markup("unknown")


def test_admin_menu_serializes_native_semantic_styles():
    expected_styles = {
        "✅ Confirmations": "success",
        "📊 Server Info": "primary",
        "➕ Add User": "success",
        "❌ Delete User": "danger",
        "🧹 Expired Cleanup": "danger",
        "🔁 Mass Copy / Migrate": "primary",
        "📈 Growth Funnel": "primary",
        "🏠 Admin Menu": "primary",
        **{category["text"]: "primary" for category in common.ADMIN_CATEGORIES.values()},
    }

    serialized_buttons = []
    for view in ("root", *EXPECTED_GROUP_ROWS):
        serialized_buttons.extend(
            button
            for row in serialized_keyboard(view)["keyboard"]
            for button in row
        )

    for button in serialized_buttons:
        assert button["text"]
        expected_style = expected_styles.get(button["text"])
        if expected_style is None:
            assert "style" not in button
        else:
            assert button["style"] == expected_style


def test_action_catalog_and_group_coverage_are_complete():
    assert set(common.ADMIN_ACTIONS) == EXPECTED_ACTION_KEYS
    assert all(set(action) == {"text", "style", "group"} for action in common.ADMIN_ACTIONS.values())
    assert {
        common.admin_action_text(key) for key in EXPECTED_ACTION_KEYS
    } == common.ADMIN_ACTION_BUTTONS

    canonical_buttons = [
        button
        for rows in common.ADMIN_GROUP_MENU_ROWS.values()
        for row in rows
        for button in row
        if button != common.ADMIN_HOME_BUTTON_TEXT
    ]
    assert Counter(canonical_buttons) == Counter({button: 1 for button in common.ADMIN_ACTION_BUTTONS})
    for view, rows in common.ADMIN_GROUP_MENU_ROWS.items():
        action_buttons = {
            button
            for row in rows
            for button in row
            if button != common.ADMIN_HOME_BUTTON_TEXT
        }
        assert action_buttons == {
            action["text"]
            for action in common.ADMIN_ACTIONS.values()
            if action["group"] == view
        }

    root_action_buttons = {
        button
        for row in common.ADMIN_ROOT_MENU_ROWS
        for button in row
        if button in common.ADMIN_ACTION_BUTTONS
    }
    assert root_action_buttons == {"✅ Confirmations", "📊 Server Info"}
    assert common.ADMIN_NAVIGATION_BUTTONS == {
        "👥 Users",
        "💳 Sales",
        "💼 Resellers",
        "⚙️ System",
        "📊 Reports",
        "📣 Messaging",
        "🏠 Admin Menu",
    }
    assert common.ADMIN_MAIN_MENU_BUTTONS == (
        common.ADMIN_ACTION_BUTTONS | common.ADMIN_NAVIGATION_BUTTONS
    )
    assert common.GROWTH_FUNNEL_BUTTON_TEXT == common.admin_action_text("growth_funnel")


def test_navigation_resolution_and_main_markup_delegation(monkeypatch):
    for view, category in common.ADMIN_CATEGORIES.items():
        assert common.resolve_admin_menu_view(category["text"]) == view
    assert common.resolve_admin_menu_view(common.ADMIN_HOME_BUTTON_TEXT) == "root"
    assert common.resolve_admin_menu_view("✅ Confirmations") is None
    assert common.resolve_admin_menu_view("not a menu button") is None
    assert common.resolve_admin_menu_view(None) is None

    sentinel = object()
    calls = []

    def fake_admin_markup(view="root"):
        calls.append(view)
        return sentinel

    monkeypatch.setattr(common, "create_admin_markup", fake_admin_markup)
    assert common.create_main_markup(is_admin=True, user_id=123) is sentinel
    assert common.create_main_markup_with_language({}, is_admin=True, user_id=123) is sentinel
    assert calls == ["root", "root"]


def test_non_admin_keyboard_is_unchanged():
    translations = {
        "my_configs": "configs",
        "purchase_plan": "purchase",
        "downloads": "downloads",
        "test_config": "test",
        "referral": "referral",
        "reseller_panel": "reseller",
        "support": "support",
        "language": "language",
    }
    markup = common.create_main_markup_with_language(translations)
    serialized = json.loads(markup.to_json())

    assert serialized["keyboard"] == [
        [{"text": "configs"}, {"text": "purchase"}],
        [{"text": "downloads"}, {"text": "test"}],
        [{"text": "referral"}, {"text": "reseller"}],
        [{"text": "support"}, {"text": "language"}],
    ]
    assert serialized["resize_keyboard"] is True
