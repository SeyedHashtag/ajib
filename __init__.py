"""
AJIB — Modular Telegram Bot for VPN customers, resellers, and admins.

This top-level package marker makes `ajib` a Python package and exposes minimal
metadata without importing heavy submodules.
"""

from __future__ import annotations

# Keep in sync with ajib.bot.__version__ to avoid importing subpackages here.
__version__ = "0.1.0"

__all__ = ["__version__"]
