import ast
import importlib.util
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
TRANSLATIONS_PATH = BOT_DIR / "utils" / "hosted_translations.py"
SPEC = importlib.util.spec_from_file_location("hosted_translations", TRANSLATIONS_PATH)
TRANSLATIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSLATIONS)
HOSTED_TRANSLATIONS = TRANSLATIONS.HOSTED_TRANSLATIONS
hosted_text = TRANSLATIONS.hosted_text
USERNAME_UTILS_PATH = BOT_DIR / "utils" / "username_utils.py"
USERNAME_SPEC = importlib.util.spec_from_file_location("username_utils", USERNAME_UTILS_PATH)
USERNAME_UTILS = importlib.util.module_from_spec(USERNAME_SPEC)
USERNAME_SPEC.loader.exec_module(USERNAME_UTILS)
WORKER_SOURCE = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")
WORKER_TREE = ast.parse(WORKER_SOURCE)


def _worker_function(name):
    return next(node for node in WORKER_TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _worker_constant(name):
    for node in WORKER_TREE.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing worker constant: {name}")


def _button_translations():
    source = (BOT_DIR / "utils" / "translations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BUTTON_TRANSLATIONS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("Missing BUTTON_TRANSLATIONS")


def _hosted_user_creator(
    existing_usernames,
    add_user_results=None,
    recorded_usernames=None,
    history_error=None,
):
    calls = []
    note_texts = []
    results = list(add_user_results or [{"ok": True}])

    class FakeClient:
        server_id = "server-1"

        def add_user(self, username, traffic_limit, expiration_days, **kwargs):
            calls.append((username, traffic_limit, expiration_days, kwargs))
            return results.pop(0) if results else {"ok": True}

    client = FakeClient()

    class FakeMultiServerAPI:
        def create_user_with_retry(
            self,
            allocator,
            creator,
            on_username_allocated=None,
            reuse_username_on_retry=False,
        ):
            result = None
            username = None
            for _attempt in range(2):
                if username is None or not reuse_username_on_retry:
                    username = allocator(existing_usernames)
                if on_username_allocated is not None:
                    on_username_allocated(username, client)
                result = creator(client, username)
                if result is not None:
                    break
            return username, result, client

    def fake_build_user_note(username, traffic_limit, expiration_days, **kwargs):
        note_texts.append(kwargs.get("note_text"))
        return f"note:{kwargs.get('note_text')}"

    def load_recorded_usernames(**kwargs):
        if history_error is not None:
            raise USERNAME_UTILS.RecordedUsernameLoadError(history_error)
        return set(recorded_usernames or set())

    namespace = {
        "MultiServerAPI": FakeMultiServerAPI,
        "OWNER_ID": 5956844665,
        "allocate_username": USERNAME_UTILS.allocate_username,
        "build_user_note": fake_build_user_note,
        "load_recorded_usernames": load_recorded_usernames,
        "RecordedUsernameLoadError": USERNAME_UTILS.RecordedUsernameLoadError,
        "tenant_file": lambda owner_id, filename: f"/tenants/{owner_id}/{filename}",
        "logging": logging,
    }
    module = ast.Module(body=[_worker_function("_create_user")], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)
    return namespace["_create_user"], calls, note_texts


def _pricing_preview(settings, visible_plans, all_plans=None):
    all_plans = dict(all_plans or visible_plans)

    def quote(plan, current_settings, referral_margin_percent=0, referred=False):
        catalog = round(float(plan["price"]), 2)
        wholesale = round(float(plan["wholesale"]), 2)
        retail = round(catalog * (1 + float(current_settings["markup_percent"]) / 100), 2)
        crypto_collected = round(retail * 0.95, 2)
        card_margin = round(retail - wholesale, 2)
        crypto_margin = round(crypto_collected - wholesale, 2)
        referral_rate = float(referral_margin_percent) if referred else 0
        card_referral = round(max(0, card_margin) * referral_rate / 100, 2)
        crypto_referral = round(max(0, crypto_margin) * referral_rate / 100, 2)
        return {
            "wholesale": wholesale,
            "retail_base": catalog,
            "retail": retail,
            "crypto_collected": crypto_collected,
            "crypto_margin": crypto_margin,
            "card_margin": card_margin,
            "crypto_referral_reward": crypto_referral,
            "card_referral_reward": card_referral,
            "crypto_supported": crypto_collected >= wholesale,
        }

    namespace = {
        "OWNER_ID": 7,
        "TELEGRAM_SAFE_TEXT_LIMIT": 3800,
        "get_settings": lambda owner_id: dict(settings),
        "_sellable_plans": lambda: dict(visible_plans),
        "_load_plans": lambda: dict(all_plans),
        "_owner_plan_ids": lambda: set(all_plans),
        "_hosted_plan_quote": quote,
        "_hosted_message": lambda owner_id, key, **values: hosted_text("en", key, **values),
        "format_usd_amount": lambda value: f"{float(value):.2f}",
    }
    module = ast.Module(
        body=[
            _worker_function("_pricing_plan_items"),
            _worker_function("_split_message_blocks"),
            _worker_function("_pricing_overview"),
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)
    return namespace


class HostedStorefrontTranslationTests(unittest.TestCase):
    def test_every_supported_language_has_the_complete_hosted_catalog(self):
        expected = set(HOSTED_TRANSLATIONS["en"])

        for language, catalog in HOSTED_TRANSLATIONS.items():
            with self.subTest(language=language):
                self.assertEqual(set(catalog), expected)
                self.assertTrue(all(str(value).strip() for value in catalog.values()))

    def test_customer_catalog_positioning_and_currency_templates_are_symmetric(self):
        headings = {
            "en": "*Built for quality, not crowding.* Hysteria connects directly to the server and is sensitive to server load. We keep fewer users on each server for a fast, stable, uninterrupted connection.",
            "fa": "*کیفیت، نه شلوغی.* پروتکل Hysteria مستقیماً به سرور متصل می‌شود و به بار سرور حساس است. برای ارائهٔ اتصالی سریع، پایدار و بدون قطعی، تعداد کاربران هر سرور را محدود نگه می‌داریم.",
            "ru": "*Качество без перегрузки.* Протокол Hysteria подключается напрямую к серверу и чувствителен к его нагрузке. Мы ограничиваем число пользователей на каждом сервере, чтобы обеспечить быстрое, стабильное и бесперебойное соединение.",
            "tk": "*Hil üçin döredildi, aşa ýüklenme üçin däl.* Hysteria serwere gönüden-göni birigýän we onuň ýüküne duýgur protokoldyr. Çalt, durnukly we üznüksiz birikmäni üpjün etmek üçin her serwerdäki ulanyjylaryň sanyny az saklaýarys.",
        }
        removed_badges = {"pick_cheapest", "pick_balanced", "pick_best_value"}

        for language, heading in headings.items():
            catalog = HOSTED_TRANSLATIONS[language]
            self.assertEqual(catalog["all_plans_title"].split("\n\n", 1)[1], heading)
            self.assertTrue(catalog["all_plans_title"].startswith("1️⃣ "))
            self.assertFalse(removed_badges & catalog.keys())
            self.assertTrue(catalog["pick_recommended"])
            self.assertIn("${usd}", catalog["plan_button_usd_only"])
            self.assertNotIn("{toman}", catalog["plan_button_usd_only"])
            self.assertIn("${usd}", catalog["crypto_total_usd_only"])
            self.assertNotIn("{toman}", catalog["crypto_total_usd_only"])

        for language in ("en", "fa"):
            catalog = HOSTED_TRANSLATIONS[language]
            self.assertLess(
                catalog["plan_button_usd_first"].index("${usd}"),
                catalog["plan_button_usd_first"].index("{toman}"),
            )
            self.assertTrue(catalog["card_total_toman"].startswith("🏦"))
            self.assertNotIn("🇮🇷", catalog["card_total_toman"])

    def test_invite_and_earn_buttons_use_money_bag_emoji_in_every_language(self):
        button_translations = _button_translations()

        for language in ("en", "fa", "ru", "tk"):
            with self.subTest(language=language):
                self.assertTrue(button_translations[language]["referral"].startswith("💰 "))
                self.assertTrue(HOSTED_TRANSLATIONS[language]["invite_and_earn_button"].startswith("💰 "))

    def test_dynamic_owner_messages_format_in_every_language(self):
        for language in HOSTED_TRANSLATIONS:
            with self.subTest(language=language):
                summary = hosted_text(
                    language, "owner_summary", markup=25, crypto="enabled", card="1234", referral=20
                )
                receipt = hosted_text(
                    language, "receipt_owner_caption", user_id=7, plan_gb=30, days=30,
                    toman_price="1,250,000",
                )
                setup = hosted_text(
                    language,
                    "setup_dashboard",
                    completed=2,
                    total=3,
                    pricing="✅",
                    payments="✅",
                    plans="⬜",
                    next_step=hosted_text(language, "setup_next_plans"),
                )
                pricing = hosted_text(
                    language,
                    "pricing_current",
                    markup=20,
                    referral=20,
                )
                crypto = hosted_text(
                    language,
                    "pricing_crypto",
                    crypto_price="114.00",
                    crypto_profit="34.00",
                    crypto_referral="6.80",
                    crypto_net="27.20",
                )
                plan = hosted_text(
                    language,
                    "pricing_plan",
                    plan_gb=30,
                    days=30,
                    catalog="100.00",
                    cost="80.00",
                    card_price="120.00",
                    card_profit="40.00",
                    card_referral="8.00",
                    card_net="32.00",
                    crypto=crypto,
                )
                connection = hosted_text(language, "connected_success", username="shopbot")
                self.assertIn("25", summary)
                self.assertIn("1,250,000", receipt)
                self.assertIn("2", setup)
                self.assertIn("3", setup)
                self.assertIn("20", pricing)
                self.assertIn("100.00", plan)
                self.assertIn("80.00", plan)
                self.assertIn("120.00", plan)
                self.assertIn("114.00", plan)
                self.assertIn("32.00", plan)
                self.assertIn("27.20", plan)
                self.assertIn("shopbot", connection)

    def test_non_english_owner_guides_are_translated(self):
        english = HOSTED_TRANSLATIONS["en"]["owner_guide"]

        for language in ("fa", "ru", "tk"):
            with self.subTest(language=language):
                self.assertNotEqual(HOSTED_TRANSLATIONS[language]["owner_guide"], english)

    def test_customer_copy_never_exposes_the_reseller_relationship(self):
        customer_keys = {
            "purchase_unavailable",
            "credit_unavailable",
            "crypto_disabled",
            "payment_processing",
            "paid_needs_attention",
            "receipt_rejected",
            "config_ready",
            "config_no_url",
            "support_default",
            "referral_withdrawal_result",
        }
        forbidden_terms = {
            "en": ("reseller", "wholesale", "operator"),
            "fa": ("نماینده", "عمده", "اپراتور"),
            "ru": ("реселлер", "оптов", "оператор"),
            "tk": ("satyjy", "lomaý", "operator"),
        }

        for language, terms in forbidden_terms.items():
            customer_copy = "\n".join(
                HOSTED_TRANSLATIONS[language][key] for key in customer_keys
            ).casefold()
            for term in terms:
                with self.subTest(language=language, term=term):
                    self.assertNotIn(term.casefold(), customer_copy)


class HostedStorefrontParityTests(unittest.TestCase):
    def test_hosted_paid_usernames_are_reseller_scoped_and_audit_data_moves_to_note(self):
        base = "hs5956844665"
        create_user, calls, note_texts = _hosted_user_creator({base, f"{base}a"})
        allocations = []

        username, result, _client = create_user(
            {"gb": 30, "days": 30},
            "",
            customer_id=124041600,
            operation_id="550e8400-e29b-41d4-a716-446655440000",
            username_prefix="hs",
            on_username_allocated=lambda allocated, client: allocations.append(
                (allocated, client.server_id)
            ),
        )

        self.assertEqual(username, f"{base}b")
        self.assertNotIn("124041600", username)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(note_texts, ["customer=u124041600; order=550e8400e29b"])
        self.assertEqual(allocations, [(f"{base}b", "server-1")])
        self.assertEqual(calls[0][0], f"{base}b")
        self.assertEqual(
            calls[0][3]["note"],
            "note:customer=u124041600; order=550e8400e29b",
        )

        suffixes = [""] + [chr(ord("a") + index) for index in range(26)]
        existing = {f"{base}{suffix}" for suffix in suffixes}
        self.assertEqual(USERNAME_UTILS.allocate_username("hs", 5956844665, existing), f"{base}aa")

    def test_hosted_test_usernames_use_test_reseller_prefix_and_concise_note(self):
        base = "ht5956844665"
        create_user, calls, note_texts = _hosted_user_creator({base})

        username, result, _client = create_user(
            {"gb": 1, "days": 30},
            "",
            customer_id=124041600,
            username_prefix="ht",
        )

        self.assertEqual(username, f"{base}a")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(note_texts, ["customer=u124041600"])
        self.assertEqual(calls[0][3]["note"], "note:customer=u124041600")

    def test_hosted_creation_skips_a_username_kept_in_local_records(self):
        base = "hs5956844665"
        create_user, calls, _note_texts = _hosted_user_creator(
            set(),
            recorded_usernames={base},
        )

        username, result, _client = create_user(
            {"gb": 30, "days": 30},
            "",
            customer_id=124041600,
            username_prefix="hs",
        )

        self.assertEqual(username, f"{base}a")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0][0], f"{base}a")

    def test_hosted_retry_reuses_its_previously_persisted_username(self):
        create_user, calls, _note_texts = _hosted_user_creator(
            {"hs5956844665"},
            recorded_usernames={"hs5956844665"},
        )

        username, result, _client = create_user(
            {"gb": 30, "days": 30},
            "",
            customer_id=124041600,
            operation_id="550e8400-e29b-41d4-a716-446655440000",
            username_prefix="hs",
            preferred_username="hs5956844665",
        )

        self.assertEqual(username, "hs5956844665")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0][0], "hs5956844665")

    def test_hosted_history_failure_stops_before_vpn_creation(self):
        create_user, calls, _note_texts = _hosted_user_creator(
            set(),
            history_error="damaged tenant payments",
        )

        result = create_user(
            {"gb": 30, "days": 30},
            "",
            customer_id=124041600,
            username_prefix="hs",
        )

        self.assertEqual(result, (None, None, None))
        self.assertEqual(calls, [])

    def test_hosted_notes_keep_the_admin_edit_field_empty(self):
        note = USERNAME_UTILS.build_user_note(
            username="hs5956844665",
            traffic_limit=30,
            expiration_days=30,
            note_text="customer=u124041600; order=550e8400e29b",
            timestamp="2026-07-18 12:00",
        )

        self.assertEqual(
            note,
            "📅 2026-07-18 12:00 | 📝 customer=u124041600; "
            "order=550e8400e29b | ✏️ ",
        )

    def test_manual_hosted_user_keeps_label_without_customer_id(self):
        create_user, _calls, note_texts = _hosted_user_creator(set())

        username, result, _client = create_user(
            {"gb": 10, "days": 30},
            "manual customer",
        )

        self.assertEqual(username, "h5956844665")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(note_texts, ["manual customer"])
        self.assertNotIn("Telegram user ID", note_texts[0])

    def test_hosted_user_creation_does_not_drop_note_after_a_failure(self):
        create_user, calls, note_texts = _hosted_user_creator(
            set(),
            add_user_results=[None, {"ok": True}],
        )

        username, result, _client = create_user(
            {"gb": 1, "days": 30},
            "",
            customer_id=124041600,
            username_prefix="ht",
        )

        self.assertEqual(username, "ht5956844665")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            note_texts,
            ["customer=u124041600", "customer=u124041600"],
        )
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("note" in call[3] for call in calls))

    def test_hosted_customer_ids_are_passed_only_for_telegram_customer_flows(self):
        provision = ast.get_source_segment(WORKER_SOURCE, _worker_function("_provision_payment"))
        free_test = ast.get_source_segment(WORKER_SOURCE, _worker_function("free_test"))
        manual = ast.get_source_segment(WORKER_SOURCE, _worker_function("owner_generate_input"))

        self.assertIn("customer_id=customer_id", provision)
        self.assertIn('username_prefix="hs"', provision)
        self.assertIn("operation_id=payment_id", provision)
        self.assertIn("preferred_username=provisioned_username", provision)
        self.assertIn("customer_id=customer.id", free_test)
        self.assertIn('username_prefix="ht"', free_test)
        self.assertIn("preferred_username=pending_username", free_test)
        self.assertNotIn("operation_id=", free_test)
        self.assertNotIn("customer_id=", manual)

    def test_main_bot_username_prefixes_are_unchanged(self):
        purchase_source = (BOT_DIR / "utils" / "purchase_plan.py").read_text(encoding="utf-8")
        test_source = (BOT_DIR / "utils" / "test_config.py").read_text(encoding="utf-8")

        self.assertIn('allocate_username("s", user_id', purchase_source)
        self.assertIn('allocate_username("t", user_id', test_source)

    def test_checkout_keeps_navigation_and_uses_the_localized_progress_context(self):
        source = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")

        for behavior in (
            'callback_data="hb:plans"',
            'callback_data=f"hb:cancel:{order_id}"',
            '"checkout_source"',
            '"receipt_prompt_message_id"',
            '"card_checkout"',
            '"crypto_checkout"',
            '"risk_disclosure"',
            "qrcode.make(url)",
            "format_toman_amount",
        ):
            with self.subTest(behavior=behavior):
                self.assertIn(behavior, source)

        self.assertNotIn("Another checkout is already open", source)
        self.assertNotIn('callback_data=f"hb:receipt:', source)
        payment_method = ast.get_source_segment(WORKER_SOURCE, _worker_function("payment_method"))
        self.assertNotIn('"purchase_connection_warning"', payment_method)

    def test_customer_handlers_do_not_use_old_reseller_disclosures(self):
        source = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")

        self.assertNotIn("Contact the reseller for support", source)
        self.assertNotIn("by the reseller", source)

    def test_owner_pricing_preview_explains_cost_prices_profit_and_referrals(self):
        settings = {
            "markup_percent": 20,
            "referral_margin_percent": 20,
            "plan_selection_configured": True,
        }
        plans = {
            "30": {"price": 100, "wholesale": 80, "gb": 30, "days": 30},
        }
        namespace = _pricing_preview(settings, plans)

        chunks = namespace["_pricing_overview"]()
        text = "\n".join(chunks)

        for value in (
            "$100.00",  # Catalog price.
            "$80.00",   # Level-discounted owner cost.
            "$120.00",  # Card customer price.
            "$40.00",   # Card gross profit.
            "$8.00",    # Card referral payout.
            "$32.00",   # Card owner net.
            "$114.00",  # Crypto customer payment after 5% discount.
            "$34.00",   # Crypto gross profit.
            "$6.80",    # Crypto referral payout.
            "$27.20",   # Crypto owner net.
        ):
            with self.subTest(value=value):
                self.assertIn(value, text)
        self.assertIn("Card payments reach your card", text)
        self.assertIn("credit gross profit to Earnings", text)
        self.assertIn("separate liability", text)

    def test_owner_pricing_preview_handles_zero_increase_and_plan_fallbacks(self):
        settings = {
            "markup_percent": 0,
            "referral_margin_percent": 0,
            "plan_selection_configured": False,
        }
        fallback_plans = {
            "10": {"price": 100, "wholesale": 100, "gb": 10, "days": 30},
        }
        namespace = _pricing_preview(settings, {}, all_plans=fallback_plans)

        text = "\n".join(namespace["_pricing_overview"]())

        self.assertIn("Customer price increase: *0%*", text)
        self.assertIn("*10 GB · 30 days*", text)
        self.assertIn("Card: customer pays *$100.00* · Gross profit *$0.00*", text)
        self.assertIn("Crypto: *unavailable at this price*", text)
        self.assertIn("*$95.00*", text)
        self.assertIn("*$100.00* cost", text)

        configured = dict(settings, plan_selection_configured=True)
        empty_namespace = _pricing_preview(configured, {}, all_plans=fallback_plans)
        empty_text = "\n".join(empty_namespace["_pricing_overview"]())
        self.assertIn("No eligible plans are available", empty_text)
        self.assertNotIn("*10 GB · 30 days*", empty_text)

    def test_owner_pricing_preview_splits_large_plan_catalogs_safely(self):
        settings = {
            "markup_percent": 20,
            "referral_margin_percent": 20,
            "plan_selection_configured": True,
        }
        plans = {
            str(plan_id): {
                "price": plan_id * 10,
                "wholesale": plan_id * 8,
                "gb": plan_id,
                "days": 30,
            }
            for plan_id in range(1, 41)
        }
        namespace = _pricing_preview(settings, plans)

        chunks = namespace["_pricing_overview"](max_length=500)
        text = "\n".join(chunks)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertIn("*1 GB · 30 days*", text)
        self.assertIn("*40 GB · 30 days*", text)
        self.assertIn("*Where the money goes*", text)

    def test_owner_pricing_prompt_and_keyboard_are_only_on_the_final_chunk(self):
        sent = []
        states = []

        class DummyMarkup:
            def __init__(self):
                self.buttons = []

            def add(self, button):
                self.buttons.append(button)

        namespace = {
            "OWNER_ID": 7,
            "TELEGRAM_SAFE_TEXT_LIMIT": 3800,
            "_set_input_state": lambda owner_id, state: states.append((owner_id, state)),
            "_hosted_message": lambda owner_id, key: {
                "pricing_keep": "Keep",
                "prompt_markup": "Send the customer price increase.",
            }.get(key, key),
            "_pricing_overview": lambda max_length: ["first chunk", "last chunk"],
            "types": SimpleNamespace(
                InlineKeyboardMarkup=DummyMarkup,
                InlineKeyboardButton=lambda text, callback_data: (text, callback_data),
            ),
            "bot": SimpleNamespace(
                send_message=lambda *args, **kwargs: sent.append((args, kwargs)),
            ),
        }
        module = ast.Module(body=[_worker_function("_begin_owner_setting")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)

        namespace["_begin_owner_setting"](99, "markup")

        self.assertEqual(states, [(7, {"kind": "owner_setting", "field": "markup"})])
        self.assertEqual(len(sent), 2)
        self.assertNotIn("reply_markup", sent[0][1])
        self.assertEqual(sent[0][0][1], "first chunk")
        self.assertIn("last chunk\n\nSend the customer price increase.\n\n/cancel", sent[1][0][1])
        self.assertEqual(sent[1][1]["reply_markup"].buttons, [("Keep", "hb:setup:keeppricing")])

    def test_owner_panel_uses_reply_keyboard_and_keeps_dynamic_controls_inline(self):
        owner_markup = ast.get_source_segment(WORKER_SOURCE, _worker_function("_owner_markup"))
        owner_menu_text = ast.get_source_segment(WORKER_SOURCE, _worker_function("_owner_menu_text"))

        self.assertIn("types.ReplyKeyboardMarkup(resize_keyboard=True)", owner_markup)
        self.assertNotIn("types.InlineKeyboardMarkup", owner_markup)
        self.assertIn("for row in rows", owner_markup)
        self.assertIn('_button(user_id, "back")', owner_menu_text)
        for callback in (
            "hb:ogen:", "hb:plantoggle:", "hb:plansdone", "hb:payment:",
            "hb:messages:", "hb:earn:", "hb:refresolve:",
        ):
            with self.subTest(callback=callback):
                self.assertIn(callback, WORKER_SOURCE)

    def test_owner_reply_commands_map_every_localized_label(self):
        menu_rows = _worker_constant("OWNER_MENU_ROWS")
        setup_rows = _worker_constant("OWNER_SETUP_ROWS")
        customer_rows = _worker_constant("OWNER_CUSTOMER_ROWS")
        money_rows = _worker_constant("OWNER_MONEY_ROWS")
        legacy_rows = _worker_constant("LEGACY_OWNER_MENU_ROWS")
        setting_keys = _worker_constant("OWNER_SETTING_KEYS")
        group_keys = _worker_constant("OWNER_GROUP_KEYS")
        legacy_labels = _worker_constant("LEGACY_OWNER_LABELS")
        button_translations = _button_translations()
        namespace = {
            "HOSTED_TRANSLATIONS": HOSTED_TRANSLATIONS,
            "OWNER_MENU_ROWS": menu_rows,
            "OWNER_SETUP_ROWS": setup_rows,
            "OWNER_CUSTOMER_ROWS": customer_rows,
            "OWNER_MONEY_ROWS": money_rows,
            "LEGACY_OWNER_MENU_ROWS": legacy_rows,
            "OWNER_SETTING_KEYS": setting_keys,
            "OWNER_GROUP_KEYS": group_keys,
            "LEGACY_OWNER_LABELS": legacy_labels,
            "_all_button_values": lambda key, fallback=None: {
                catalog.get(key, fallback or button_translations["en"].get(key, key))
                for catalog in button_translations.values()
            },
        }
        module = ast.Module(body=[_worker_function("_owner_menu_command")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)
        command_for = namespace["_owner_menu_command"]

        self.assertEqual(
            menu_rows,
            (
                ("owner_setup", "owner_customers"),
                ("owner_money", "customer_view"),
            ),
        )
        self.assertTrue(all(len(row) == 2 for row in menu_rows))
        self.assertEqual(
            set(setting_keys),
            {"markup", "card", "support", "welcome", "refpercent"},
        )
        for language, catalog in HOSTED_TRANSLATIONS.items():
            self.assertNotIn("rate", catalog)
            self.assertNotIn("prompt_rate", catalog)
            for key in {"generate", "customers", "debt", "payment_methods", "plans", "messages", "earnings", "referrals", "crypto"}:
                with self.subTest(language=language, action=key):
                    self.assertEqual(command_for(catalog[key]), ("action", key))
            for key in setting_keys:
                with self.subTest(language=language, setting=key):
                    self.assertEqual(command_for(catalog[key]), ("setting", key))
            for key in group_keys:
                with self.subTest(language=language, group=key):
                    self.assertEqual(command_for(catalog[key]), ("group", key))
            self.assertEqual(command_for(catalog["owner_home"]), ("home", None))
            self.assertEqual(command_for(catalog["customer_view"]), ("customer_view", None))
            with self.subTest(language=language, action="back"):
                self.assertEqual(command_for(button_translations[language]["back"]), ("customer_view", None))
        for label in legacy_labels["markup"]:
            with self.subTest(legacy_markup_label=label):
                self.assertEqual(command_for(label), ("setting", "markup"))

    def test_owner_setup_deep_link_is_reserved_from_referrals(self):
        start = ast.get_source_segment(WORKER_SOURCE, _worker_function("start"))

        self.assertIn('start_payload == "owner_setup"', start)
        self.assertIn("message.from_user.id == OWNER_ID", start)
        self.assertIn('start_payload != "owner_setup"', start)
        self.assertIn("_show_owner_dashboard", start)

    def test_main_bot_connection_hands_management_to_the_hosted_bot(self):
        source = (BOT_DIR / "utils" / "hosted_bot_handlers.py").read_text(encoding="utf-8")

        self.assertIn('?start=owner_setup', source)
        self.assertIn('from utils.hosted_translations import hosted_text', source)
        self.assertIn('callback_data="hosted:disconnect:confirm"', source)
        self.assertIn('callback_data="hosted:disconnect:cancel"', source)
        self.assertNotIn("Configure markup, payments, support, referrals, and earnings", source)

    def test_hosted_checkout_uses_the_shared_main_exchange_rate(self):
        purchase_options = ast.get_source_segment(WORKER_SOURCE, _worker_function("_purchase_options"))
        payment_method = ast.get_source_segment(WORKER_SOURCE, _worker_function("payment_method"))

        self.assertIn("from utils.exchange_rate import get_exchange_rate", WORKER_SOURCE)
        self.assertIn("exchange_rate = get_exchange_rate()", purchase_options)
        self.assertIn("exchange_rate = get_exchange_rate()", payment_method)
        self.assertNotIn('settings.get("exchange_rate"', WORKER_SOURCE)

    def test_owner_back_restores_main_menu_and_legacy_callbacks_remain(self):
        owner_menu = ast.get_source_segment(WORKER_SOURCE, _worker_function("owner_menu_action"))

        self.assertIn("_pop_input_state(OWNER_ID)", owner_menu)
        self.assertIn("reply_markup=_main_markup(OWNER_ID)", owner_menu)
        self.assertIn('c.data.startswith("hb:owner:")', WORKER_SOURCE)
        self.assertIn('c.data.startswith("hb:setting:")', WORKER_SOURCE)


if __name__ == "__main__":
    unittest.main()
