"""Formatting helpers for Telegram's legacy ``Markdown`` parse mode.

Only values originating outside a static translation should pass through these
helpers.  Raw values must remain available to API and QR-code callers.
"""


def escape_markdown_text(value) -> str:
    """Escape a dynamic value rendered as legacy-Markdown text."""
    text = str(value if value is not None else "")
    text = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]"):
        text = text.replace(character, f"\\{character}")
    return text


def escape_markdown_code(value) -> str:
    """Escape a dynamic value rendered inside legacy-Markdown backticks."""
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("`", "\\`")
