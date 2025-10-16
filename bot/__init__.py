"""
AJIB Telegram Bot package.

This module provides minimal, dependency-free utilities and metadata to treat
`ajib.bot` as a Python package and to expose small, generic entrypoints that
other submodules can import without causing circular or heavy imports.

It intentionally avoids importing any optional or large dependencies so that:
- Packaging tools can import `ajib.bot` safely (e.g., during setup or CLI calls)
- Simple environment/config checks can happen without side effects
"""

from pathlib import Path
from typing import Dict, Optional, Set

__all__ = [
    "__version__",
    "PACKAGE_ROOT",
    "LOCALES_DIR",
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "read_env",
    "get_admin_ids",
    "is_admin",
]

# Semantic version of the package segment.
__version__ = "0.1.0"

# Basic paths used across the project without heavy imports
PACKAGE_ROOT: Path = Path(__file__).resolve().parent
LOCALES_DIR: Path = PACKAGE_ROOT / "locales"

# i18n defaults
DEFAULT_LOCALE: str = "en"
SUPPORTED_LOCALES = ("en", "fa")


def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    """
    Read a simple .env file into a dict without external dependencies.
    - Supports lines of the form KEY=VALUE
    - Strips quotes around values if present
    - Ignores blank lines and comments starting with '#'

    Args:
        path: Optional path to the .env file. If None, attempts to read
              from /opt/ajib/.env, falling back to project root .env.

    Returns:
        Dict of environment key/value pairs.
    """
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    else:
        # Common installation locations; installer/CLI can standardize this.
        candidates.extend(
            [
                Path("/opt/ajib/.env"),
                PACKAGE_ROOT.parent.parent / ".env",  # repo root fallback
            ]
        )

    env: Dict[str, str] = {}
    for candidate in candidates:
        try:
            if candidate.is_file():
                for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip("'").strip('"')
                    if key:
                        env[key] = value
                # Stop at the first .env found
                break
        except Exception:
            # Silently continue; caller can decide how to handle missing/invalid .env
            continue

    return env


def get_admin_ids(env: Optional[Dict[str, str]] = None) -> Set[int]:
    """
    Compute the set of admin user IDs from environment mapping.

    Accepted keys:
    - TELEGRAM_ADMIN_IDS: Comma-separated numeric IDs
    - TELEGRAM_ADMIN_ID: Single numeric ID (fallback)

    Args:
        env: Existing environment mapping (e.g., from read_env()).
             If None, reads with read_env().

    Returns:
        Set of integer admin user IDs.
    """
    if env is None:
        env = read_env()

    ids: Set[int] = set()

    raw_many = env.get("TELEGRAM_ADMIN_IDS", "")
    if raw_many:
        for part in raw_many.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                # Ignore non-numeric entries
                continue

    raw_single = env.get("TELEGRAM_ADMIN_ID", "")
    if raw_single:
        try:
            ids.add(int(raw_single.strip()))
        except ValueError:
            pass

    return ids


def is_admin(user_id: int, env: Optional[Dict[str, str]] = None) -> bool:
    """
    Check if the given Telegram user_id is an admin.

    Args:
        user_id: Telegram numeric user ID
        env: Optional environment mapping. If not provided, read_env() is used.

    Returns:
        True if user_id appears in TELEGRAM_ADMIN_IDS/TELEGRAM_ADMIN_ID; else False.
    """
    return user_id in get_admin_ids(env)
