import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_UTILS = ROOT / "core" / "scripts" / "telegrambot" / "utils"
PRIVATE_IDENTIFIER = "".join(chr(code) for code in (97, 106, 105, 98))


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, BOT_UTILS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_catalog_is_brand_neutral(catalog_name, catalog):
    for language, messages in catalog.items():
        for key, value in messages.items():
            assert PRIVATE_IDENTIFIER not in str(value).casefold(), (
                f"{catalog_name}.{language}.{key} exposes the private project identifier"
            )


def test_every_customer_visible_catalog_is_brand_neutral():
    translations = _load_module("brand_private_main_translations", "translations.py")
    hosted = _load_module("brand_private_hosted_translations", "hosted_translations.py")

    _assert_catalog_is_brand_neutral("buttons", translations.BUTTON_TRANSLATIONS)
    _assert_catalog_is_brand_neutral("messages", translations.MESSAGE_TRANSLATIONS)
    _assert_catalog_is_brand_neutral("hosted", hosted.HOSTED_TRANSLATIONS)
