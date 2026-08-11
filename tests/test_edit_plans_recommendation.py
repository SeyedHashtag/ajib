import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDIT_PLANS_PATH = ROOT / "core/scripts/telegrambot/utils/edit_plans.py"


class Markup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)

    def row(self, *buttons):
        self.buttons.extend(buttons)


class Button:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class Bot:
    def __init__(self):
        self.answers = []
        self.edits = []

    def message_handler(self, *args, **kwargs):
        return lambda function: function

    def callback_query_handler(self, *args, **kwargs):
        return lambda function: function

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    def send_message(self, *args, **kwargs):
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=2)

    def reply_to(self, *args, **kwargs):
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=2)

    def register_next_step_handler(self, *args, **kwargs):
        return None


def load_edit_plans(monkeypatch, tmp_path):
    bot = Bot()
    telebot = types.ModuleType("telebot")
    telebot.types = types.SimpleNamespace(
        InlineKeyboardMarkup=Markup,
        InlineKeyboardButton=Button,
    )
    command = types.ModuleType("utils.command")
    command.bot = bot
    command.is_admin = lambda _user_id: True
    common = types.ModuleType("utils.common")
    common.admin_action_text = lambda key: key
    common.create_main_markup = lambda **_kwargs: Markup()
    monkeypatch.setitem(sys.modules, "telebot", telebot)
    monkeypatch.setitem(sys.modules, "utils.command", command)
    monkeypatch.setitem(sys.modules, "utils.common", common)

    spec = importlib.util.spec_from_file_location("edit_plans_recommendation_test", EDIT_PLANS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PLANS_FILE = str(tmp_path / "plans.json")
    return module, bot


def call(data):
    return types.SimpleNamespace(
        data=data,
        id="callback",
        from_user=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=2),
    )


def test_main_admin_recommendation_is_unique_and_customer_only(monkeypatch, tmp_path):
    module, bot = load_edit_plans(monkeypatch, tmp_path)
    module.save_plans({
        "10": {"price": 2, "days": 30, "recommended": True},
        "20": {"price": 3, "days": 30, "recommended": True},
        "100": {"price": 4, "days": 30, "target": "reseller"},
    })

    markup, text, _plans = module.create_plans_markup()
    assert text.count("⭐ Recommended") == 1
    assert module.get_recommended_customer_plan_id(module.load_plans()) == "10"

    module.handle_recommend_customer_plan(call("recommend_customer_plan:20"))
    saved = module.load_plans()
    assert saved["20"]["recommended"] is True
    assert "recommended" not in saved["10"]
    assert sum(details.get("recommended") is True for details in saved.values()) == 1

    module.handle_recommend_customer_plan(call("recommend_customer_plan:100"))
    assert bot.answers[-1][1]["show_alert"] is True
    assert module.load_plans()["20"]["recommended"] is True
    assert any(button.callback_data == "select_plan:0" for button in markup.buttons)


def test_target_change_and_delete_remove_main_recommendation(monkeypatch, tmp_path):
    module, _bot = load_edit_plans(monkeypatch, tmp_path)
    module.save_plans({
        "10": {"price": 2, "days": 30, "recommended": True},
        "20": {"price": 3, "days": 30},
    })

    module.handle_update_target(call("update_target:reseller:10"))
    assert module.get_recommended_customer_plan_id(module.load_plans()) is None

    plans = module.load_plans()
    plans["20"]["recommended"] = True
    module.save_plans(plans)
    module.handle_plan_delete(call("delete_plan:20"))
    saved = json.loads(Path(module.PLANS_FILE).read_text(encoding="utf-8"))
    assert "20" not in saved
    assert module.get_recommended_customer_plan_id(saved) is None
