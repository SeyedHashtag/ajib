"""
Core package for AJIB Telegram Bot.

This subpackage contains:
- Configuration and logging helpers (config.py)
- Bot runner and dispatcher setup (bot.py)

It intentionally avoids importing submodules at package import time to keep
side effects minimal and improve startup performance for tools.
"""

from __future__ import annotations

__all__: list[str] = []
