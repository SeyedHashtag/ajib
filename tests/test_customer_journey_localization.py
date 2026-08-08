import importlib.util
from pathlib import Path
from string import Formatter


ROOT = Path(__file__).resolve().parents[1]
TRANSLATIONS_PATH = ROOT / "core/scripts/telegrambot/utils/translations.py"

JOURNEY_KEYS = {
    "referral_stats",
    "referral_share_text",
    "referral_share_button",
    "recruitment_claim_cash",
    "recruitment_claim_credit",
    "recruitment_reward_claimed_cash",
    "recruitment_reward_claimed_credit",
    "recruitment_reward_unavailable",
    "recruitment_progress_summary",
    "referral_invalid_reward_action",
    "customer_reseller_only_plan",
    "payment_status_checking",
    "payment_status_check_in_progress",
    "payment_status_paid",
    "payment_status_pending_label",
    "payment_status_failed",
    "payment_status_expired",
    "payment_status_rejected",
    "payment_status_canceled",
    "payment_status_unknown",
    "settlement_credit_failed",
    "referral_wallet_invalid",
    "referral_wallet_required",
    "referral_withdraw_minimum",
    "referral_withdraw_request_sent",
    "referral_withdraw_request_error",
    "account_credit_unavailable",
    "value_not_available",
    "value_unknown",
    "value_unlimited",
    "refresh_action",
    "payment_status_completed",
    "payment_status_processing",
    "renewal_state_summary",
    "renewal_generic_unavailable_reason",
    "renewal_ipv4_line",
    "reseller_request_failed",
    "reseller_program_preview",
    "reseller_program_preview_no_plan",
    "reseller_program_apply",
    "reseller_program_see_plans",
    "reseller_eligibility_checklist",
    "reseller_launch_storefront",
    "reseller_already_approved",
    "reseller_eligibility_checking",
    "reseller_request_check_in_progress",
    "reseller_access_required",
    "reseller_customer_only_plan",
    "reseller_config_created",
    "reseller_config_accounting_cancelled",
    "reseller_config_creation_failed",
    "reseller_access_inactive",
    "reseller_config_creating",
    "reseller_config_start_failed",
    "reseller_config_in_progress",
    "reseller_invalid_customer_category",
    "reseller_invalid_request",
    "reseller_removed_during_cleanup",
    "reseller_customer_entry",
    "reseller_config_data_unavailable",
    "reseller_traffic_no_data",
    "reseller_traffic_data",
    "reseller_config_status_blocked",
    "reseller_config_status_active",
    "reseller_config_live_details",
    "reseller_config_expired",
    "reseller_subscription_unavailable",
    "reseller_config_subscription_caption",
    "reseller_renewal_processing",
    "reseller_renewal_in_progress",
}


def _load_translations():
    spec = importlib.util.spec_from_file_location(
        "customer_journey_translations_under_test",
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


def test_customer_journey_catalog_is_complete_in_all_supported_languages():
    translations = _load_translations()
    catalogs = translations.MESSAGE_TRANSLATIONS

    for language in ("en", "fa", "ru", "tk"):
        missing = JOURNEY_KEYS - catalogs[language].keys()
        assert not missing, f"{language} is missing: {sorted(missing)}"
        for key in JOURNEY_KEYS:
            assert str(catalogs[language][key]).strip(), f"{language}.{key} is empty"


def test_invite_and_earn_heading_uses_money_bag_emoji_in_every_language():
    catalogs = _load_translations().MESSAGE_TRANSLATIONS

    for language in ("en", "fa", "ru", "tk"):
        assert catalogs[language]["referral_stats"].startswith("💰 ")


def test_customer_journey_placeholders_match_english_catalog():
    translations = _load_translations()
    catalogs = translations.MESSAGE_TRANSLATIONS

    for key in JOURNEY_KEYS:
        expected = _placeholders(catalogs["en"][key])
        for language in ("fa", "ru", "tk"):
            assert _placeholders(catalogs[language][key]) == expected, (
                f"{language}.{key} placeholders do not match English"
            )


def test_reseller_config_delivery_omits_price_in_all_supported_languages():
    catalogs = _load_translations().MESSAGE_TRANSLATIONS
    expected_placeholders = {"username", "plan_gb", "days", "ipv4_info", "sub_url"}

    for language in ("en", "fa", "ru", "tk"):
        template = catalogs[language]["reseller_config_created"]
        assert _placeholders(template) == expected_placeholders
        assert "{price}" not in template
        assert "$" not in template


def test_changed_customer_journey_copy_is_not_hardcoded_in_handlers():
    source_paths = [
        ROOT / "core/scripts/telegrambot/utils/referral_handlers.py",
        ROOT / "core/scripts/telegrambot/utils/reseller_handlers.py",
        ROOT / "core/scripts/telegrambot/utils/renewal.py",
        ROOT / "core/scripts/telegrambot/utils/expired_cleanup.py",
        ROOT / "core/scripts/telegrambot/utils/traffic_monitor.py",
    ]
    banned_literals = (
        "Invalid reward action.",
        "Use my invitation link and save",
        "Share Invitation",
        "Reseller access required.",
        "Checking reseller eligibility...",
        "Reseller request is already being checked.",
        "Failed to submit request. Please try again.",
        "This plan is for customers only.",
        "Creating config. I will send it here when it is ready.",
        "Configuration expired/blocked",
        "Could not generate subscription URL",
        "Reserve renewal",
        "Processing renewal...",
        "Renewal is already in progress.",
        "Days remaining: unknown",
        "renewal is unavailable",
        'or "Renew Plan"',
    )

    for path in source_paths:
        source = path.read_text(encoding="utf-8")
        for literal in banned_literals:
            assert literal not in source, f"hardcoded customer copy in {path.name}: {literal}"
