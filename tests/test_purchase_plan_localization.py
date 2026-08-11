import ast
import importlib.util
import re
from pathlib import Path
from string import Formatter


ROOT = Path(__file__).resolve().parents[1]
PURCHASE_PLAN_PATH = (
    ROOT / "core/scripts/telegrambot/utils/purchase_plan.py"
)
TRANSLATIONS_PATH = (
    ROOT / "core/scripts/telegrambot/utils/translations.py"
)

PUBLIC_CUSTOMER_FUNCTIONS = {
    "send_due_card_checkout_reminders",
    "maybe_send_checkout_reminder",
    "_process_customer_renewal_payment",
    "_fulfill_credit_funded_purchase",
    "handle_purchase_selection",
    "handle_purchase_support",
    "handle_cancel_purchase",
    "handle_crypto_payment",
    "_process_check_payment_job",
    "handle_check_payment",
    "process_payment_webhook",
    "check_pending_payments",
}

REQUIRED_KEYS = {
    "customer_reseller_only_plan",
    "payment_status_checking",
    "payment_status_check_in_progress",
    "payment_status_completed",
    "payment_status_processing",
    "payment_status_paid",
    "payment_status_pending_label",
    "payment_status_failed",
    "payment_status_expired",
    "payment_status_rejected",
    "payment_status_canceled",
    "payment_status_unknown",
    "settlement_credit_failed",
    "renewal_ipv4_line",
    "renewal_generic_unavailable_reason",
}

REMOVED_PUBLIC_LITERALS = {
    "This plan is for resellers only.",
    "Checking payment status...",
    "Payment status check is already in progress.",
    "Your renewal is paid and reserved.",
    "Your reserved renewal for `",
    'f"IPv4 URL:',
}


def _load_translations():
    spec = importlib.util.spec_from_file_location(
        "purchase_plan_translations_under_test",
        TRANSLATIONS_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _placeholders(template):
    return {
        field_name.split(".", 1)[0].split("[", 1)[0]
        for _literal, field_name, _format_spec, _conversion in Formatter().parse(template)
        if field_name
    }


def _callee_name(call):
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _direct_message_expressions(call):
    name = _callee_name(call)
    text_indexes = {
        "send_message": 1,
        "safe_send_message": 2,
        "reply_to": 1,
        "safe_reply_to": 2,
        "edit_message_text": 0,
        "answer_callback_query": 1,
        "safe_answer_callback_query": 2,
    }
    index = text_indexes.get(name)
    if index is not None and len(call.args) > index:
        yield call.args[index]
    for keyword in call.keywords:
        if keyword.arg in {"text", "caption"}:
            yield keyword.value


def _literal_fragments(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_fragments(node.left) + _literal_fragments(node.right)
    return ""


def test_purchase_plan_customer_catalog_keys_exist_with_matching_placeholders():
    catalogs = _load_translations().MESSAGE_TRANSLATIONS

    for language in ("en", "fa", "ru", "tk"):
        assert not REQUIRED_KEYS - catalogs[language].keys()
        for key in REQUIRED_KEYS:
            assert str(catalogs[language][key]).strip()
            assert _placeholders(catalogs[language][key]) == _placeholders(
                catalogs["en"][key]
            )


def test_customer_pricing_and_hysteria_catalog_copy_are_localized_by_currency_policy():
    catalogs = _load_translations().MESSAGE_TRANSLATIONS
    headings = {
        "en": "**Built for quality, not crowding.** Hysteria connects directly to the server and is sensitive to server load. We keep fewer users on each server for a fast, stable, uninterrupted connection.",
        "fa": "**کیفیت، نه شلوغی.** پروتکل Hysteria مستقیماً به سرور متصل می‌شود و به بار سرور حساس است. برای ارائهٔ اتصالی سریع، پایدار و بدون قطعی، تعداد کاربران هر سرور را محدود نگه می‌داریم.",
        "ru": "**Качество без перегрузки.** Протокол Hysteria подключается напрямую к серверу и чувствителен к его нагрузке. Мы ограничиваем число пользователей на каждом сервере, чтобы обеспечить быстрое, стабильное и бесперебойное соединение.",
        "tk": "**Hil üçin döredildi, aşa ýüklenme üçin däl.** Hysteria serwere gönüden-göni birigýän we onuň ýüküne duýgur protokoldyr. Çalt, durnukly we üznüksiz birikmäni üpjün etmek üçin her serwerdäki ulanyjylaryň sanyny az saklaýarys.",
    }
    removed_badges = {
        "quick_pick_cheapest",
        "quick_pick_balanced",
        "quick_pick_best_value",
    }

    for language, heading in headings.items():
        catalog = catalogs[language]
        assert catalog["all_plans_title"].split("\n\n", 1)[1] == heading
        assert catalog["all_plans_title"].startswith("● ")
        assert not removed_badges & catalog.keys()
        assert catalog["quick_pick_recommended"]
        assert _placeholders(catalog["plan_price_usd_only"]) == {"usd"}
        assert _placeholders(catalog["plan_payment_totals_crypto_only"]) == {
            "original_usd", "crypto_percent", "crypto_total"
        }
        assert _placeholders(catalog["renewal_payment_totals_crypto_only"]) == {
            "original_usd", "crypto_percent", "crypto_total"
        }

    for language in ("en", "fa"):
        totals = catalogs[language]["plan_payment_totals_usd_first"]
        renewals = catalogs[language]["renewal_payment_totals_usd_first"]
        assert totals.index("{crypto_total}") < totals.index("{card_total}")
        assert renewals.index("{crypto_total}") < renewals.index("{card_total}")
        assert "🏦" in totals and "🏦" in renewals
        assert "🇮🇷" not in totals + renewals


def test_public_purchase_handlers_have_no_direct_customer_copy_literals():
    source = PURCHASE_PLAN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    for literal in REMOVED_PUBLIC_LITERALS:
        assert literal not in source, f"old customer-facing literal remains: {literal}"

    for function_name in PUBLIC_CUSTOMER_FUNCTIONS:
        function = functions[function_name]
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            for expression in _direct_message_expressions(call):
                literal = _literal_fragments(expression)
                assert not re.search(r"[A-Za-z]{3}", literal), (
                    f"hardcoded customer copy in {function_name} at line "
                    f"{call.lineno}: {literal!r}"
                )


def test_public_payment_status_templates_receive_localized_status_values():
    tree = ast.parse(PURCHASE_PLAN_PATH.read_text(encoding="utf-8"))

    for function in (
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in PUBLIC_CUSTOMER_FUNCTIONS
    ):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "format":
                continue
            for keyword in call.keywords:
                if keyword.arg != "status":
                    continue
                assert not isinstance(keyword.value, ast.Constant), (
                    f"raw status passed to customer template in {function.name} "
                    f"at line {call.lineno}"
                )
