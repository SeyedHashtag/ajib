from __future__ import annotations

"""
Internationalization (i18n) module for AJIB bot.

Provides:
- Loading YAML locale files (en.yml, fa.yml)
- Translation function with placeholder interpolation
- Language preference management

Example:

    from ajib.bot.core.i18n import load_locales, translate

    locales = load_locales()
    text = translate(locales, "en", "welcome", name="John")
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .. import LOCALES_DIR, DEFAULT_LOCALE, SUPPORTED_LOCALES

__all__ = [
    "load_locales",
    "translate",
    "get_locale_text",
]


def load_locales(locales_dir: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """
    Load all locale files from locales directory.

    Returns:
        Dict mapping locale code (e.g., "en", "fa") to translation dict.

    Example:
        locales = load_locales()
        # locales == {"en": {"welcome": "Welcome"}, "fa": {"welcome": "خوش آمدید"}}
    """
    if locales_dir is None:
        locales_dir = LOCALES_DIR

    result: Dict[str, Dict[str, str]] = {}

    for locale_code in SUPPORTED_LOCALES:
        locale_file = locales_dir / f"{locale_code}.yml"
        if not locale_file.exists():
            continue

        try:
            with locale_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict):
                    result[locale_code] = data
        except Exception:
            # Silently skip problematic locale files
            continue

    return result


def translate(
    locales: Dict[str, Dict[str, str]],
    lang: str,
    key: str,
    **kwargs: Any,
) -> str:
    """
    Translate a key in the given language with optional placeholder interpolation.

    Args:
        locales: Loaded locales dictionary
        lang: Target language code (e.g., "en", "fa")
        key: Translation key (e.g., "welcome")
        **kwargs: Placeholder values for format string interpolation

    Returns:
        Translated string with placeholders replaced, or the key itself if not found.

    Example:
        >>> locales = {"en": {"greeting": "Hello, {name}!"}}
        >>> translate(locales, "en", "greeting", name="Alice")
        "Hello, Alice!"
    """
    # Get locale dict for language
    locale = locales.get(lang)
    if locale is None:
        # Fallback to default locale
        locale = locales.get(DEFAULT_LOCALE, {})

    text = locale.get(key)
    if text is None:
        # Try default locale if not found
        if lang != DEFAULT_LOCALE:
            text = locales.get(DEFAULT_LOCALE, {}).get(key)
        # Still not found? Return key
        if text is None:
            return key

    # Interpolate placeholders
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            # Format error, return as-is
            return text

    return text


def get_locale_text(
    locales: Dict[str, Dict[str, str]],
    lang: str,
    key: str,
) -> str:
    """
    Get locale text without placeholder interpolation.

    Args:
        locales: Loaded locales dictionary
        lang: Target language code
        key: Translation key

    Returns:
        Raw translated string or key if not found.
    """
    return translate(locales, lang, key)


# ------------
# Helper class for convenience
# ------------


class I18n:
    """
    I18n helper class that loads locales once and provides translate method.

    Example:
        i18n = I18n()
        text = i18n.t("en", "welcome", name="John")
    """

    def __init__(self, locales_dir: Optional[Path] = None) -> None:
        self.locales = load_locales(locales_dir)

    def t(self, lang: str, key: str, **kwargs: Any) -> str:
        """Translate key in given language with optional placeholders."""
        return translate(self.locales, lang, key, **kwargs)

    def get(self, lang: str, key: str) -> str:
        """Get raw text without interpolation."""
        return get_locale_text(self.locales, lang, key)

    def reload(self, locales_dir: Optional[Path] = None) -> None:
        """Reload locales from disk."""
        self.locales = load_locales(locales_dir)
