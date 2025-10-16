from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional, Set, Any, Tuple

from .. import read_env, DEFAULT_LOCALE, SUPPORTED_LOCALES

# Service identity
SERVICE_NAME = "ajib-bot"

# Default locations for runtime data (first existing/usable one will be used)
DEFAULT_DATA_DIRS = (
    Path("/opt/ajib"),
    Path.home() / ".local" / "share" / "ajib",
)
DEFAULT_DB_FILENAME = "ajib.sqlite3"

# Internal flag to avoid configuring logging multiple times
_LOGGING_CONFIGURED = False


def env_bool(value: Optional[str], default: bool = False) -> bool:
    """
    Parse a boolean-like environment value.

    Truthy: "1", "true", "yes", "y", "on"
    Falsey: "0", "false", "no", "n", "off"

    Any other value -> default
    """
    if value is None:
        return default
    val = value.strip().lower()
    if val in {"1", "true", "yes", "y", "on"}:
        return True
    if val in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_level(level: Optional[str]) -> int:
    """Normalize string level to logging level constant."""
    if not level:
        return logging.INFO
    try:
        return getattr(logging, str(level).upper())
    except AttributeError:
        return logging.INFO


def resolve_data_dir(env: Optional[Dict[str, str]] = None) -> Path:
    """
    Determine the data directory without causing side-effects (no creation here).

    Order:
    1) DATA_DIR in .env (if absolute path)
    2) First existing from DEFAULT_DATA_DIRS
    3) Fallback to first from DEFAULT_DATA_DIRS (even if not existing yet)

    Note: Creation of the directory is deferred to `ensure_paths`.
    """
    env = env or {}
    custom = env.get("DATA_DIR", "").strip()
    if custom:
        p = Path(custom).expanduser()
        if p.is_absolute():
            return p

    for candidate in DEFAULT_DATA_DIRS:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            # Skip problematic paths
            continue

    # Fallback to the first choice
    return DEFAULT_DATA_DIRS[0]


def resolve_database_paths(
    env: Dict[str, str], data_dir: Path
) -> Tuple[Optional[str], Path]:
    """
    Resolve database connection and file path.

    - DATABASE_URL (optional) may be provided for non-sqlite backends.
    - If not provided or is a sqlite-like URL, default to a local sqlite file.

    Returns:
        (database_url, sqlite_path)
    """
    database_url = env.get("DATABASE_URL", "").strip() or None
    # Default sqlite path regardless of DATABASE_URL (for local backup/ops)
    sqlite_path = data_dir / DEFAULT_DB_FILENAME

    if not database_url:
        return None, sqlite_path

    # If a sqlite URL is provided, keep sqlite_path aligned with the local file
    if database_url.lower().startswith(("sqlite://", "sqlite3://")):
        return database_url, sqlite_path

    # Non-sqlite database URL is in use
    return database_url, sqlite_path


def _collect_client_downloads(env: Dict[str, str]) -> Dict[str, str]:
    """
    Collect client download links by OS code.
    Expected keys:
      - CLIENT_DL_URL_ANDROID
      - CLIENT_DL_URL_IOS
      - CLIENT_DL_URL_WINDOWS
      - CLIENT_DL_URL_MACOS
      - CLIENT_DL_URL_LINUX
    """
    keys = [
        ("android", "CLIENT_DL_URL_ANDROID"),
        ("ios", "CLIENT_DL_URL_IOS"),
        ("windows", "CLIENT_DL_URL_WINDOWS"),
        ("macos", "CLIENT_DL_URL_MACOS"),
        ("linux", "CLIENT_DL_URL_LINUX"),
    ]
    out: Dict[str, str] = {}
    for os_key, env_key in keys:
        v = env.get(env_key, "").strip()
        if v:
            out[os_key] = v
    return out


@dataclass(frozen=True)
class Config:
    # Core bot
    bot_token: str
    admin_ids: Set[int]

    # Backend API (Blitz-compatible)
    blitz_api_base: Optional[str] = None
    blitz_api_key: Optional[str] = None

    # Payments (Heleket)
    heleket_api_key: Optional[str] = None
    heleket_network: str = "mainnet"  # e.g., mainnet or testnet
    heleket_webhook_secret: Optional[str] = None

    # Runtime paths
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIRS[0])
    database_url: Optional[str] = None
    sqlite_path: Path = field(
        default_factory=lambda: DEFAULT_DATA_DIRS[0] / DEFAULT_DB_FILENAME
    )

    # Internationalization
    default_locale: str = DEFAULT_LOCALE

    # Client download URLs
    client_dl_urls: Dict[str, str] = field(default_factory=dict)

    # Support contact (username, URL, or email)
    support_contact: Optional[str] = None

    # Logging config
    log_level: str = "INFO"
    log_json: bool = False

    # Path to the .env used to load this config (if discovered)
    env_path: Optional[Path] = None

    def redacted_dict(self) -> Dict[str, Any]:
        """Dictionary representation with secrets redacted."""
        d = asdict(self)
        for key in list(d.keys()):
            if any(s in key.lower() for s in ("token", "key", "secret")) and isinstance(
                d[key], str
            ):
                if d[key]:
                    d[key] = "***REDACTED***"
        # Paths to strings for clean logging
        if isinstance(d.get("data_dir"), Path):
            d["data_dir"] = str(d["data_dir"])
        if isinstance(d.get("sqlite_path"), Path):
            d["sqlite_path"] = str(d["sqlite_path"])
        if isinstance(d.get("env_path"), Path):
            d["env_path"] = str(d["env_path"])
        return d


def load_config(env_path: Optional[Path] = None) -> Config:
    """
    Load configuration from .env and environment variables.

    Only the Telegram BOT token is strictly required at runtime.
    The installer is expected to provide BOT token and primary admin ID, while
    other credentials can be set later via the CLI.
    """
    env = read_env(env_path)

    bot_token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required but not set in environment.")

    # Admin IDs
    admin_ids: Set[int] = set()
    # Prefer TELEGRAM_ADMIN_IDS, fallback to TELEGRAM_ADMIN_ID
    if env.get("TELEGRAM_ADMIN_IDS"):
        for part in env["TELEGRAM_ADMIN_IDS"].split(","):
            part = part.strip()
            if not part:
                continue
            try:
                admin_ids.add(int(part))
            except ValueError:
                continue
    elif env.get("TELEGRAM_ADMIN_ID"):
        try:
            admin_ids.add(int(env["TELEGRAM_ADMIN_ID"].strip()))
        except ValueError:
            pass

    # Runtime directories
    data_dir = resolve_data_dir(env)
    database_url, sqlite_path = resolve_database_paths(env, data_dir)

    # Locale
    default_locale = (env.get("LOCALE_DEFAULT") or DEFAULT_LOCALE).strip().lower()
    if default_locale not in SUPPORTED_LOCALES:
        default_locale = DEFAULT_LOCALE

    # Logging
    log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    log_json = env_bool(env.get("LOG_JSON"), default=False)

    cfg = Config(
        bot_token=bot_token,
        admin_ids=admin_ids,
        blitz_api_base=(env.get("BLITZ_API_BASE") or "").strip() or None,
        blitz_api_key=(env.get("BLITZ_API_KEY") or "").strip() or None,
        heleket_api_key=(env.get("HELEKET_API_KEY") or "").strip() or None,
        heleket_network=(env.get("HELEKET_NETWORK") or "mainnet").strip() or "mainnet",
        heleket_webhook_secret=(env.get("HELEKET_WEBHOOK_SECRET") or "").strip()
        or None,
        data_dir=data_dir,
        database_url=database_url,
        sqlite_path=sqlite_path,
        default_locale=default_locale,
        client_dl_urls=_collect_client_downloads(env),
        support_contact=(env.get("SUPPORT_CONTACT") or "").strip() or None,
        log_level=log_level,
        log_json=log_json,
        env_path=_discover_env_path(env_path),
    )

    return cfg


def ensure_paths(cfg: Config) -> None:
    """
    Ensure required directories/files exist.
    Safe to call multiple times; no effect if already present.
    """
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Avoid hard failure on path issues; calling code can handle/log this.
        pass

    # For sqlite use-case, ensure parent exists (file itself is created by DB layer)
    try:
        cfg.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def setup_logging(
    level: Optional[str] = None,
    json: Optional[bool] = None,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """
    Configure root logger once. Subsequent calls are no-ops.

    Priority for level/json:
      1) Explicit parameters
      2) Environment variables (LOG_LEVEL, LOG_JSON)
      3) Defaults (INFO, False)
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    env = env or read_env()
    resolved_level = _normalize_level(level or env.get("LOG_LEVEL", "INFO"))
    use_json = (
        json if json is not None else env_bool(env.get("LOG_JSON"), default=False)
    )

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Avoid duplicate handlers if something added one before
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        if use_json:
            # Simple JSON-like formatting without external libs
            formatter = logging.Formatter(
                fmt='{"ts":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}',
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        else:
            formatter = logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        handler.setFormatter(formatter)
        root.addHandler(handler)

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """
    Obtain a logger with module-wide configuration ensured.
    """
    if not _LOGGING_CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def _discover_env_path(explicit: Optional[Path]) -> Optional[Path]:
    """
    Attempt to determine which .env was used.
    """
    if explicit:
        return explicit

    candidates = [
        Path("/opt/ajib/.env"),
        Path.cwd() / ".env",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None
