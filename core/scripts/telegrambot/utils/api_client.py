"""
Unified API client for all ajib REST API interactions.

This is the single source of truth for all HTTP communication with the ajib
backend. Import ``APIClient`` from here instead of from individual handler
modules (adduser, edituser, deleteuser, …).

Legacy public methods return parsed JSON data (dict / list / str) on success,
or ``None`` on failure. Structured lookup/reset methods additionally expose
whether the target was missing or its assigned server was unavailable.
"""

import json
import math
import os
import re
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
try:
    from utils.account_state import PanelState, inspect_account, panel_deadline, parse_timestamp
    from utils.time_utils import format_utc_timestamp, utc_now
except ModuleNotFoundError:  # Standalone diagnostics/tests.
    from account_state import PanelState, inspect_account, panel_deadline, parse_timestamp
    from time_utils import format_utc_timestamp, utc_now
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv


TELEGRAM_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
_HTTP_POOL_CONNECTIONS = 8
_HTTP_POOL_MAXSIZE = 16
GIB = 1024 ** 3
MILLISECONDS_PER_DAY = 86_400_000
THREE_X_UI_PANEL = "3x-ui"
BLITZ_PANEL = "blitz"
_thread_local = threading.local()


def _destination_transfer_password(
    username: str,
    source_password: str | None,
    source_panel: str,
    destination_panel: str,
) -> str | None:
    if (
        source_panel == BLITZ_PANEL
        and destination_panel == THREE_X_UI_PANEL
        and isinstance(source_password, str)
    ):
        return f"{username}:{source_password}"
    return source_password


@dataclass(frozen=True)
class UserRef:
    server_id: str
    username: str
    panel_type: str = BLITZ_PANEL


@dataclass
class UserProvisionSpec:
    username: str
    traffic_limit_bytes: int
    expiration_days: int
    password: str | None = None
    creation_date: str | None = None
    absolute_expiry: datetime | None = None
    delayed_start: bool = True
    upload_bytes: int = 0
    download_bytes: int = 0
    blocked: bool = False
    unlimited_ip: bool = False
    note: str | None = None
    inbound_ids: list[int] = field(default_factory=list)
    limit_ip: int | None = None

    def __post_init__(self):
        if (
            isinstance(self.expiration_days, bool)
            or not isinstance(self.expiration_days, int)
            or self.expiration_days < 0
        ):
            raise ValueError("expiration_days must be a non-negative integer")


@dataclass(frozen=True)
class UserCopySpec:
    source: UserRef
    destination_server_id: str
    inbound_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BulkUserTransferSpec:
    """Persistent server-to-server copy or migration request.

    Only stable identifiers are carried here.  Live credentials and links are
    deliberately fetched from the panels while an item is processed.
    """

    mode: str
    source_server_id: str
    destination_server_id: str
    inbound_ids: tuple[int, ...] = ()
    requesting_admin: str = ""
    notification_policy: str = "disabled"


@dataclass(frozen=True)
class _UserTransferSnapshot:
    username: str
    source_panel_type: str
    password: str
    total_bytes: int
    upload_bytes: int
    download_bytes: int
    duration_days: int
    blocked: bool
    delayed_start: bool
    absolute_expiry: datetime | None
    account_creation_date: str | None
    note: str | None
    unlimited_access: bool


def _canonical_note(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _ceil_utc_day(value: datetime) -> datetime:
    current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if current == midnight else midnight + timedelta(days=1)


def _float_env(name, default, minimum=0.1):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)
    return value if value >= minimum else float(default)


def get_api_read_timeout_seconds() -> float:
    return _float_env("AJIB_API_READ_TIMEOUT_SECONDS", 6)


def get_api_write_timeout_seconds() -> float:
    return _float_env("AJIB_API_WRITE_TIMEOUT_SECONDS", 10)


def _get_thread_session(session_key) -> requests.Session:
    """Return a per-thread pooled HTTP session for a server/auth pair."""
    sessions = getattr(_thread_local, "api_sessions", None)
    if sessions is None:
        sessions = {}
        _thread_local.api_sessions = sessions

    session = sessions.get(session_key)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=_HTTP_POOL_CONNECTIONS, pool_maxsize=_HTTP_POOL_MAXSIZE)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        sessions[session_key] = session
    return session


def _safe_server_id(value: str, fallback: str = "primary") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(value or "").strip())
    return cleaned or fallback


def _safe_weight(value) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(weight) or weight < 0:
        return 1.0
    return 0.0 if weight == 0 else weight


def server_placement_status(
    server: dict,
    *,
    healthy: bool,
    creation_ready: bool,
) -> tuple[bool, str]:
    """Return whether a server accepts automatic placement and why."""
    if not server.get("enabled", True):
        return False, "disabled"
    if _safe_weight(server.get("weight", 1)) == 0:
        return False, "weight_zero"
    if not healthy:
        return False, "unhealthy"
    if not creation_ready:
        return False, "creation_not_ready"
    return True, "ready"


def _safe_bool(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "enabled", "on"}:
            return True
        if normalized in {"0", "false", "no", "disabled", "off"}:
            return False
    return bool(default)


def _normalise_panel_type(value) -> str:
    normalized = str(value or BLITZ_PANEL).strip().lower().replace("_", "-")
    if normalized in {"3x", "3xui", "3x-ui", "x-ui", "xui"}:
        return THREE_X_UI_PANEL
    return BLITZ_PANEL


def _safe_inbound_ids(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        values = re.split(r"[|,\s]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    result = []
    for item in values:
        if item in (None, ""):
            continue
        try:
            inbound_id = int(item)
        except (TypeError, ValueError):
            continue
        if inbound_id > 0 and inbound_id not in result:
            result.append(inbound_id)
    return result


def _safe_nonnegative_int(value, default=0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return parsed if parsed >= 0 else int(default)


def _renewal_postconditions(user, traffic_limit_gb, expiration_days, unlimited_ip) -> bool:
    """Return whether a live user reflects a completed renewal snapshot."""
    if not isinstance(user, dict):
        return False
    try:
        expected_bytes = int(traffic_limit_gb) * GIB
        expected_days = int(expiration_days)
    except (TypeError, ValueError, OverflowError):
        return False
    live_unlimited_ip = user.get("unlimited_ip")
    if live_unlimited_ip is None:
        live_unlimited_ip = user.get("unlimited_user")
    if live_unlimited_ip is None or any(
        user.get(field) is None for field in ("upload_bytes", "download_bytes")
    ):
        return False
    used_bytes = _safe_nonnegative_int(user.get("upload_bytes"), 0) + _safe_nonnegative_int(
        user.get("download_bytes"), 0
    )
    return bool(
        expected_bytes > 0
        and expected_days > 0
        and _safe_nonnegative_int(user.get("max_download_bytes"), -1) == expected_bytes
        and _safe_nonnegative_int(user.get("expiration_days"), -1) == expected_days
        and _safe_bool(live_unlimited_ip, False) == bool(unlimited_ip)
        and not _safe_bool(user.get("blocked"), True)
        and used_bytes == 0
    )


def _renewal_failure(outcome, stage: str) -> dict:
    result = dict(outcome) if isinstance(outcome, dict) else {}
    result["status"] = "unavailable" if result.get("status") == "unavailable" else "failed"
    result.setdefault("data", None)
    result.setdefault("http_status", None)
    result.setdefault("error", f"{stage}_failed")
    result["stage"] = stage
    return result


def _normalise_server_config(config: dict, index: int = 0) -> dict | None:
    if not isinstance(config, dict):
        return None
    url = str(config.get("url") or config.get("URL") or "").strip()
    token = str(config.get("token") or config.get("TOKEN") or "").strip()
    if not url or not token:
        return None
    server_id = _safe_server_id(config.get("id") or config.get("name") or f"server{index + 1}")
    panel_type = _normalise_panel_type(
        config.get("panel") or config.get("panel_type") or config.get("type")
    )
    return {
        "id": server_id,
        "name": str(config.get("name") or server_id),
        "url": url,
        "token": token,
        "panel": panel_type,
        "enabled": _safe_bool(config.get("enabled", True)),
        "weight": _safe_weight(config.get("weight", 1)),
        "default_inbound_ids": _safe_inbound_ids(
            config.get("default_inbound_ids") or config.get("inbound_ids")
        ),
        "default_limit_ip": _safe_nonnegative_int(
            config.get("default_limit_ip", config.get("limit_ip", 0)), 0
        ),
    }


def get_server_configs() -> list[dict]:
    """Load configured VPN API servers.

    ``SERVERS_JSON`` is the multi-server source of truth. Legacy ``URL`` and
    ``TOKEN`` remain supported as a single primary server fallback.
    """
    load_dotenv(TELEGRAM_ENV_PATH)
    raw_servers = os.getenv("SERVERS_JSON", "").strip()
    servers: list[dict] = []
    if raw_servers:
        try:
            parsed = json.loads(raw_servers)
            if isinstance(parsed, list):
                for index, item in enumerate(parsed):
                    normalized = _normalise_server_config(item, index)
                    if normalized:
                        servers.append(normalized)
        except json.JSONDecodeError as e:
            print(f"Warning: invalid SERVERS_JSON: {e}")

    if servers:
        return servers

    base_url = os.getenv('URL', '')
    token = os.getenv('TOKEN', '')
    fallback = _normalise_server_config(
        {"id": "primary", "name": "Primary", "url": base_url, "token": token, "enabled": True, "weight": 1},
        0,
    )
    return [fallback] if fallback else []


def save_server_configs(servers: list[dict]) -> bool:
    """Persist server configs to the Telegram bot .env file."""
    normalized = []
    for index, item in enumerate(servers):
        config = _normalise_server_config(item, index)
        if config:
            normalized.append(config)
    if not normalized:
        return False

    core_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if core_dir not in sys.path:
        sys.path.insert(0, core_dir)
    try:
        import ajib_operator
        from pathlib import Path
        fingerprint = ajib_operator.replace_servers(normalized, path=Path(TELEGRAM_ENV_PATH))
    except Exception as error:
        print(f"Failed to save server configuration atomically: {type(error).__name__}")
        return False

    updates = {
        "SERVERS_JSON": json.dumps(normalized, separators=(",", ":")),
        "URL": normalized[0]["url"],
        "TOKEN": normalized[0]["token"],
    }
    os.environ.update(updates)
    os.environ["AJIB_CONFIG_FINGERPRINT"] = fingerprint
    if os.getenv("AJIB_BOT_ROLE") == "main":
        ajib_operator.refresh_ready_fingerprint(fingerprint)
    multi_server_api = globals().get("MultiServerAPI")
    if multi_server_api is not None:
        multi_server_api.invalidate_all_caches()
    return True


def update_server_config(server_id: str, **changes) -> bool:
    """Atomically edit one server without overwriting concurrent changes."""
    core_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if core_dir not in sys.path:
        sys.path.insert(0, core_dir)
    try:
        import ajib_operator
        from pathlib import Path
        normalized, fingerprint = ajib_operator.update_server(
            server_id, changes, path=Path(TELEGRAM_ENV_PATH)
        )
    except Exception as error:
        print(f"Failed to update server configuration atomically: {type(error).__name__}")
        return False

    os.environ.update({
        "SERVERS_JSON": json.dumps(normalized, separators=(",", ":")),
        "URL": normalized[0]["url"],
        "TOKEN": normalized[0]["token"],
        "AJIB_CONFIG_FINGERPRINT": fingerprint,
    })
    if os.getenv("AJIB_BOT_ROLE") == "main":
        ajib_operator.refresh_ready_fingerprint(fingerprint)
    multi_server_api = globals().get("MultiServerAPI")
    if multi_server_api is not None:
        multi_server_api.invalidate_all_caches()
    return True


class APIClient:
    """HTTP client for the Blitz REST API.

    The public name is retained for compatibility.  New multi-panel code uses
    :func:`create_panel_client`, which returns this class for Blitz servers.
    """

    def __init__(self, server_config: dict | None = None):
        load_dotenv(TELEGRAM_ENV_PATH)

        server_config = _normalise_server_config(server_config or {}, 0) if server_config else None
        base_url: str = server_config["url"] if server_config else os.getenv('URL', '')
        self.token: str = server_config["token"] if server_config else os.getenv('TOKEN', '')
        self.server_id: str = server_config["id"] if server_config else "primary"
        self.server_name: str = server_config["name"] if server_config else "Primary"
        self.panel_type: str = BLITZ_PANEL
        self.server_config = server_config or {
            "id": self.server_id,
            "name": self.server_name,
            "url": base_url,
            "token": self.token,
            "panel": BLITZ_PANEL,
            "enabled": True,
            "weight": 1.0,
            "default_inbound_ids": [],
            "default_limit_ip": 0,
        }

        if not base_url or not self.token:
            print("Warning: API URL or TOKEN not found in environment variables.")

        # Normalise: ensure exactly one trailing slash
        self.base_url = base_url.rstrip('/') + '/'
        self.users_endpoint = f"{self.base_url}api/v1/users/"

        self.headers = {
            'accept': 'application/json',
            'Authorization': self.token,
        }
        self._session_key = (self.base_url, self.token)

    # ------------------------------------------------------------------ #
    # Private HTTP helpers                                                  #
    # ------------------------------------------------------------------ #

    @property
    def session(self) -> requests.Session:
        return _get_thread_session(self._session_key)

    def _request(self, method: str, url: str, *, data: dict | None = None, headers: dict | None = None, timeout: float | None = None):
        request_headers = {**self.headers}
        if headers:
            request_headers.update(headers)
        timeout = timeout if timeout is not None else get_api_read_timeout_seconds()
        return self.session.request(method, url, headers=request_headers, json=data, timeout=timeout)

    def _get(self, url: str):
        try:
            response = self._request("GET", url, timeout=get_api_read_timeout_seconds())
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[APIClient] GET {url} failed: {e}")
            return None

    @staticmethod
    def _http_status(response) -> int | None:
        try:
            return int(getattr(response, "status_code", None))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _request_error_code(error) -> str:
        if isinstance(error, requests.exceptions.Timeout):
            return "timeout"
        if isinstance(error, requests.exceptions.ConnectionError):
            return "connection_error"
        return "request_error"

    @staticmethod
    def _http_error_code(status_code: int | None) -> str:
        if status_code == 429:
            return "rate_limited"
        if status_code is not None and status_code >= 500:
            return "server_error"
        if status_code == 408:
            return "timeout"
        return "http_error"

    def _post(self, url: str, data: dict):
        try:
            response = self._request("POST", url, data=data, headers={'Content-Type': 'application/json'}, timeout=get_api_write_timeout_seconds())
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return response.text or True
        except requests.exceptions.RequestException as e:
            print(f"[APIClient] POST {url} failed: {e}")
            return None

    def _patch(self, url: str, data: dict):
        try:
            response = self._request("PATCH", url, data=data, headers={'Content-Type': 'application/json'}, timeout=get_api_write_timeout_seconds())
            response.raise_for_status()
            MultiServerAPI.invalidate_all_caches()
            try:
                return response.json()
            except ValueError:
                return {"message": "Updated successfully."}
        except requests.exceptions.RequestException as e:
            print(f"[APIClient] PATCH {url} failed: {e}")
            return None

    def _delete(self, url: str):
        try:
            response = self._request("DELETE", url, timeout=get_api_write_timeout_seconds())
            response.raise_for_status()
            MultiServerAPI.invalidate_all_caches()
            try:
                return response.json()
            except ValueError:
                return {"message": "Deleted successfully."}
        except requests.exceptions.RequestException as e:
            print(f"[APIClient] DELETE {url} failed: {e}")
            return None

    # ------------------------------------------------------------------ #
    # User operations                                                       #
    # ------------------------------------------------------------------ #

    def _normalise_user_record(self, user, username: str | None = None):
        if not isinstance(user, dict):
            return user
        normalized = dict(user)
        if username and not normalized.get("username"):
            normalized["username"] = username
        normalized.setdefault("panel_type", BLITZ_PANEL)
        normalized.setdefault("server_id", self.server_id)
        status = " ".join(
            str(normalized.get("status") or "").strip().lower()
            .replace("-", " ").replace("_", " ").split()
        )
        try:
            duration_days = int(normalized.get("expiration_days"))
        except (TypeError, ValueError, OverflowError):
            duration_days = None
        unlimited_duration = duration_days == 0
        delayed_start = False if unlimited_duration else (
            status == "on hold" and not normalized.get("account_creation_date")
        )
        if unlimited_duration:
            normalized["delayed_start"] = False
            normalized["timer_started"] = True
        else:
            normalized.setdefault("delayed_start", delayed_start)
            normalized.setdefault("timer_started", not delayed_start and bool(normalized.get("account_creation_date")))
        # Blitz reports null traffic before first connection. On-hold proves
        # the account has never started, so those counters are safely zero.
        # Active accounts with missing counters remain malformed/fail-closed.
        if delayed_start:
            if normalized.get("upload_bytes") is None:
                normalized["upload_bytes"] = 0
            if normalized.get("download_bytes") is None:
                normalized["download_bytes"] = 0
        deadline = panel_deadline(normalized)
        if deadline is not None:
            normalized.setdefault("account_expiration_date", format_utc_timestamp(deadline))
            normalized.setdefault("absolute_expiry", format_utc_timestamp(deadline))
        normalized.setdefault("credential_metadata", {
            "panel": BLITZ_PANEL,
            "fields_present": ["password"] if normalized.get("password") else [],
        })
        return normalized

    def get_users(self):
        """Return list or dict of all users, or ``None`` on failure."""
        users = self._get(self.users_endpoint)
        if isinstance(users, dict):
            return {
                username: self._normalise_user_record(user, str(username))
                for username, user in users.items()
            }
        if isinstance(users, list):
            return [self._normalise_user_record(user) for user in users]
        return users

    def get_user(self, username: str):
        """Return a single user's detail dict, or ``None`` if not found / on failure."""
        result = self.get_user_result(username)
        return result.get("data") if result.get("status") == "found" else None

    def get_user_result(self, username: str) -> dict:
        """Return a structured user lookup result without conflating 404 and outages."""
        url = f"{self.users_endpoint}{username}"
        try:
            response = self._request("GET", url, timeout=get_api_read_timeout_seconds())
        except requests.exceptions.RequestException as error:
            print(f"[APIClient] GET {url} failed: {error}")
            return {
                "status": "unavailable",
                "data": None,
                "http_status": None,
                "error": self._request_error_code(error),
            }

        status_code = self._http_status(response)
        if status_code == 404:
            return {
                "status": "missing",
                "data": None,
                "http_status": status_code,
                "error": "not_found",
            }
        if status_code is not None and status_code >= 500:
            # Blitz 2.5.x can wrap its command-line "user not found" result
            # in HTTP 500. Trust it only when it names this exact user, then
            # confirm absence against the live full list. Other 5xx responses
            # remain unavailable and therefore fail closed.
            try:
                error_payload = response.json()
            except (TypeError, ValueError):
                error_payload = None
            detail = (
                str(error_payload.get("detail") or "")
                if isinstance(error_payload, dict)
                else ""
            )
            detail_folded = detail.casefold()
            if (
                str(username).casefold() in detail_folded
                and "not found in the database" in detail_folded
            ):
                users = self.get_users()
                target = str(username).casefold()
                listed_user = None
                if isinstance(users, dict):
                    listed_user = next(
                        (
                            user for name, user in users.items()
                            if str(name).casefold() == target and isinstance(user, dict)
                        ),
                        None,
                    )
                elif isinstance(users, list):
                    listed_user = next(
                        (
                            user for user in users
                            if isinstance(user, dict)
                            and str(user.get("username") or "").casefold() == target
                        ),
                        None,
                    )
                if users is not None and listed_user is None:
                    return {
                        "status": "missing",
                        "data": None,
                        "http_status": status_code,
                        "error": "not_found",
                        "source": "confirmed_list_fallback",
                    }
                if listed_user is not None:
                    return {
                        "status": "found",
                        "data": self._normalise_user_record(listed_user, username),
                        "http_status": status_code,
                        "error": None,
                        "source": "confirmed_list_fallback",
                    }
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as error:
            print(f"[APIClient] GET {url} failed: {error}")
            return {
                "status": "unavailable",
                "data": None,
                "http_status": status_code,
                "error": self._http_error_code(status_code),
            }
        try:
            data = response.json()
        except (TypeError, ValueError):
            return {
                "status": "unavailable",
                "data": None,
                "http_status": status_code,
                "error": "invalid_response",
            }
        if not isinstance(data, dict):
            return {
                "status": "unavailable",
                "data": None,
                "http_status": status_code,
                "error": "invalid_response",
            }
        return {
            "status": "found",
            "data": self._normalise_user_record(data, username),
            "http_status": status_code,
            "error": None,
        }

    def add_user(
        self,
        username: str,
        traffic_limit: int,
        expiration_days: int,
        unlimited: bool = False,
        note: str | None = None,
        password: str | None = None,
        creation_date: str | None = None,
        blocked: bool = False,
        inbound_ids: list[int] | None = None,
    ):
        """Create a new user. Returns response data or ``None`` on failure."""
        payload = {
            "username": username,
            "traffic_limit": traffic_limit,
            "expiration_days": expiration_days,
            "unlimited": unlimited,
        }
        if note is not None:
            payload["note"] = note
        if password:
            payload["password"] = password
        if creation_date:
            payload["creation_date"] = creation_date
        result = self._post(self.users_endpoint, payload)
        if result is not None:
            MultiServerAPI.record_created_user(self.server_id, username)
            if blocked and self.update_user(username, {"blocked": True}) is None:
                return None
        return result

    def is_creation_ready(self, verify_remote: bool = False) -> tuple[bool, str | None]:
        return True, None

    def get_inbound_options(self) -> list[dict] | None:
        return []

    def provision_user(self, spec: UserProvisionSpec):
        traffic_limit_gib = int(math.ceil(spec.traffic_limit_bytes / GIB))
        return self.add_user(
            spec.username,
            traffic_limit_gib,
            spec.expiration_days,
            unlimited=spec.unlimited_ip,
            note=spec.note,
            password=spec.password,
            creation_date=spec.creation_date,
            blocked=spec.blocked,
        )

    def update_user(self, username: str, data: dict):
        """Patch one or more fields of an existing user.

        Returns the API response dict, or ``None`` on failure.
        """
        return self._patch(f"{self.users_endpoint}{username}", data)

    def reset_user(self, username: str):
        """Reset a user through the panel reset endpoint."""
        result = self.reset_user_result(username)
        return result.get("data") if result.get("status") == "succeeded" else None

    def reset_user_result(self, username: str) -> dict:
        """Reset a user and classify transient API unavailability separately."""
        url = f"{self.users_endpoint}{username}/reset"
        try:
            response = self._request("GET", url, timeout=get_api_write_timeout_seconds())
        except requests.exceptions.RequestException as error:
            print(f"[APIClient] GET {url} failed: {error}")
            return {
                "status": "unavailable",
                "data": None,
                "http_status": None,
                "error": self._request_error_code(error),
            }

        status_code = self._http_status(response)
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as error:
            print(f"[APIClient] GET {url} failed: {error}")
            unavailable = status_code in {408, 429} or (status_code is not None and status_code >= 500)
            return {
                "status": "unavailable" if unavailable else "failed",
                "data": None,
                "http_status": status_code,
                "error": self._http_error_code(status_code),
            }

        MultiServerAPI.invalidate_all_caches()
        try:
            data = response.json()
        except ValueError:
            data = {"message": "Reset successfully."}
        return {
            "status": "succeeded",
            "data": data,
            "http_status": status_code,
            "error": None,
        }

    def renew_user_result(
        self,
        username: str,
        traffic_limit_gb: int,
        expiration_days: int,
        unlimited_ip: bool = False,
    ) -> dict:
        """Apply a plan snapshot to an existing Blitz user and reset its cycle."""
        try:
            traffic_limit_gb = int(traffic_limit_gb)
            expiration_days = int(expiration_days)
        except (TypeError, ValueError, OverflowError):
            return {
                "status": "failed", "data": None, "http_status": None,
                "error": "invalid_plan", "stage": "reconfigure",
            }
        if traffic_limit_gb <= 0 or expiration_days <= 0:
            return {
                "status": "failed", "data": None, "http_status": None,
                "error": "invalid_plan", "stage": "reconfigure",
            }

        url = f"{self.users_endpoint}{username}"
        payload = {
            "new_traffic_limit": traffic_limit_gb,
            "new_expiration_days": expiration_days,
            "unlimited_ip": bool(unlimited_ip),
        }
        try:
            response = self._request(
                "PATCH",
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=get_api_write_timeout_seconds(),
            )
        except requests.exceptions.RequestException as error:
            return {
                "status": "unavailable", "data": None, "http_status": None,
                "error": self._request_error_code(error), "stage": "reconfigure",
            }
        status_code = self._http_status(response)
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException:
            unavailable = status_code in {408, 429} or (status_code is not None and status_code >= 500)
            return {
                "status": "unavailable" if unavailable else "failed",
                "data": None,
                "http_status": status_code,
                "error": self._http_error_code(status_code),
                "stage": "reconfigure",
            }
        MultiServerAPI.invalidate_all_caches()

        reset = self.reset_user_result(username)
        if reset.get("status") != "succeeded":
            lookup = self.get_user_result(username)
            if lookup.get("status") == "found" and _renewal_postconditions(
                lookup.get("data"), traffic_limit_gb, expiration_days, unlimited_ip
            ):
                return {
                    "status": "succeeded",
                    "data": reset.get("data"),
                    "user": lookup.get("data"),
                    "http_status": lookup.get("http_status"),
                    "error": None,
                    "stage": "verify",
                }
            return _renewal_failure(reset, "reset")
        lookup = self.get_user_result(username)
        if lookup.get("status") != "found":
            return _renewal_failure(lookup, "verify")
        if not _renewal_postconditions(
            lookup.get("data"), traffic_limit_gb, expiration_days, unlimited_ip
        ):
            return {
                "status": "failed", "data": lookup.get("data"),
                "http_status": lookup.get("http_status"),
                "error": "verification_failed", "stage": "verify",
            }
        return {
            "status": "succeeded",
            "data": reset.get("data"),
            "user": lookup.get("data"),
            "http_status": reset.get("http_status"),
            "error": None,
            "stage": "verify",
        }

    def delete_user(self, username: str):
        """Delete a user. Returns response data or ``None`` on failure."""
        return self._delete(f"{self.users_endpoint}{username}")

    def get_user_uri(self, username: str):
        """Return subscription URI data dict, or ``None`` on failure."""
        return self._get(f"{self.base_url}api/v1/users/{username}/uri")


class ThreeXUIAPIClient(APIClient):
    """Adapter for the current 3x-ui v3 Bearer-token clients API."""

    _DURATION_RE = re.compile(r"(?:^|\s)\[ajib-duration:(\d+)d\](?:\s|$)")

    def __init__(self, server_config: dict):
        config = _normalise_server_config(server_config or {}, 0)
        if not config:
            raise ValueError("3x-ui server requires a URL and API token")
        self.server_config = config
        self.token = config["token"]
        self.server_id = config["id"]
        self.server_name = config["name"]
        self.panel_type = THREE_X_UI_PANEL
        self.default_inbound_ids = list(config.get("default_inbound_ids") or [])
        self.default_limit_ip = _safe_nonnegative_int(config.get("default_limit_ip"), 0)
        root = config["url"].rstrip("/")
        for suffix in ("/panel/api", "/panel"):
            if root.lower().endswith(suffix):
                root = root[:-len(suffix)]
                break
        self.base_url = root.rstrip("/") + "/"
        self.api_base = f"{self.base_url}panel/api/"
        bearer = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        self.headers = {
            "accept": "application/json",
            "Authorization": bearer,
        }
        self._session_key = (self.api_base, bearer)
        self._readiness_cache = None

    @staticmethod
    def _envelope(response) -> dict:
        status_code = APIClient._http_status(response)
        if status_code == 404:
            return {"status": "missing", "data": None, "http_status": status_code, "error": "not_found"}
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException:
            return {
                "status": "unavailable" if status_code in {408, 429} or (status_code or 0) >= 500 else "failed",
                "data": None,
                "http_status": status_code,
                "error": APIClient._http_error_code(status_code),
            }
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return {"status": "unavailable", "data": None, "http_status": status_code, "error": "invalid_response"}
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            return {"status": "unavailable", "data": None, "http_status": status_code, "error": "invalid_envelope"}
        if not payload["success"]:
            message = str(payload.get("msg") or "").lower()
            missing = any(word in message for word in ("not found", "does not exist", "no client"))
            return {
                "status": "missing" if missing else "failed",
                "data": None,
                "http_status": status_code,
                "error": "not_found" if missing else "panel_rejected",
                "message": payload.get("msg"),
            }
        return {
            "status": "succeeded",
            "data": payload.get("obj"),
            "http_status": status_code,
            "error": None,
            "message": payload.get("msg"),
        }

    def _xui_result(self, method: str, path: str, data: dict | list | None = None) -> dict:
        url = f"{self.api_base}{path.lstrip('/')}"
        try:
            response = self._request(
                method,
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                timeout=get_api_write_timeout_seconds() if method.upper() != "GET" else get_api_read_timeout_seconds(),
            )
        except requests.exceptions.RequestException as error:
            return {
                "status": "unavailable",
                "data": None,
                "http_status": None,
                "error": self._request_error_code(error),
            }
        return self._envelope(response)

    @classmethod
    def _duration_days(cls, comment) -> int | None:
        match = cls._DURATION_RE.search(str(comment or ""))
        if not match:
            return None
        days = int(match.group(1))
        return days if days > 0 else None

    @classmethod
    def _comment_with_duration(cls, comment, days: int) -> str:
        clean = cls._DURATION_RE.sub(" ", str(comment or "")).strip()
        marker = f"[ajib-duration:{int(days)}d]"
        return f"{clean} {marker}".strip()

    @classmethod
    def _comment_without_marker(cls, comment) -> str | None:
        clean = cls._DURATION_RE.sub(" ", str(comment or "")).strip()
        return clean or None

    def _limit_ip_for_plan(self, unlimited_ip: bool) -> int:
        if unlimited_ip:
            return 0
        return self.default_limit_ip if self.default_limit_ip > 0 else 1

    @staticmethod
    def _record_parts(item) -> tuple[dict, list[int], dict]:
        if not isinstance(item, dict):
            return {}, [], {}
        client = item.get("client") if isinstance(item.get("client"), dict) else item
        inbound_ids = _safe_inbound_ids(item.get("inboundIds", client.get("inboundIds")))
        traffic = item.get("traffic") if isinstance(item.get("traffic"), dict) else client.get("traffic")
        if not isinstance(traffic, dict):
            traffic = {}
        return dict(client), inbound_ids, dict(traffic)

    @staticmethod
    def _client_update_payload(client: dict) -> dict:
        """Sanitize a full 3x-ui read record for the replacement endpoint."""
        payload = dict(client or {})
        if "id" in payload and not isinstance(payload.get("id"), str):
            payload.pop("id", None)
        # 3x-ui v3 serializes the database value as a string on reads even
        # though its Go update model requires []string. Preserve configured
        # values while making the empty/default representation writable.
        allowed_ips = payload.get("allowedIPs")
        if isinstance(allowed_ips, str):
            normalized = allowed_ips.strip()
            if not normalized:
                payload["allowedIPs"] = []
            else:
                try:
                    parsed = json.loads(normalized)
                except (TypeError, ValueError):
                    parsed = [item.strip() for item in normalized.split(",") if item.strip()]
                payload["allowedIPs"] = parsed if isinstance(parsed, list) else []
        return payload

    def _normalise_user(self, item, online_emails: set[str] | None = None, traffic_override=None) -> dict | None:
        client, inbound_ids, traffic = self._record_parts(item)
        if isinstance(traffic_override, dict):
            traffic.update(traffic_override)
        username = str(client.get("email") or "").strip()
        if not username:
            return None
        total_bytes = _safe_nonnegative_int(client.get("totalGB"), 0)
        upload = _safe_nonnegative_int(traffic.get("up", traffic.get("upload")), 0)
        download = _safe_nonnegative_int(traffic.get("down", traffic.get("download")), 0)
        raw_expiry_ms = client.get("expiryTime")
        expiry_ms = None
        if raw_expiry_ms is not None:
            try:
                expiry_ms = int(raw_expiry_ms)
            except (TypeError, ValueError, OverflowError):
                pass
        duration_days = self._duration_days(client.get("comment"))
        delayed_start = expiry_ms is not None and expiry_ms < 0
        absolute_expiry = None
        creation_date = None
        if expiry_ms == 0:
            duration_days = 0
        elif expiry_ms is not None and expiry_ms > 0:
            expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
            absolute_expiry = format_utc_timestamp(expiry_dt)
            if duration_days:
                creation_date = format_utc_timestamp(expiry_dt - timedelta(days=duration_days))
        elif delayed_start and not duration_days:
            duration_days = max(1, int(math.ceil(abs(expiry_ms) / MILLISECONDS_PER_DAY)))
        created_at = client.get("createdAt")
        if creation_date is None and created_at and not delayed_start:
            try:
                creation_date = format_utc_timestamp(
                    datetime.fromtimestamp(int(created_at) / 1000, tz=timezone.utc)
                )
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        blocked = not _safe_bool(client.get("enable", True))
        online = username in (online_emails or set())
        status = "Disabled" if blocked else ("On-hold" if delayed_start else ("Online" if online else "Offline"))
        credential_fields = [name for name in ("auth", "password", "uuid", "id") if client.get(name)]
        selected_credential = next(
            (name for name in ("auth", "password", "uuid", "id") if isinstance(client.get(name), str) and client.get(name)),
            None,
        )
        return {
            "username": username,
            "panel_type": THREE_X_UI_PANEL,
            "server_id": self.server_id,
            "status": status,
            "blocked": blocked,
            "max_download_bytes": total_bytes,
            "upload_bytes": upload,
            "download_bytes": download,
            "used_traffic": upload + download,
            "expiration_days": duration_days,
            "configured_duration_days": duration_days,
            "account_creation_date": creation_date,
            "account_expiration_date": absolute_expiry,
            "absolute_expiry": absolute_expiry,
            "delayed_start": delayed_start,
            "timer_started": not delayed_start,
            "unlimited_user": total_bytes == 0,
            "unlimited_ip": _safe_nonnegative_int(client.get("limitIp"), 0) == 0,
            "limit_ip": _safe_nonnegative_int(client.get("limitIp"), 0),
            "note": self._comment_without_marker(client.get("comment")),
            "inbound_ids": inbound_ids,
            "sub_id": client.get("subId"),
            "password": client.get(selected_credential) if selected_credential else None,
            "credential_metadata": {
                "panel": THREE_X_UI_PANEL,
                "fields_present": credential_fields,
                "selected_field": selected_credential,
            },
        }

    def get_users(self):
        listing = self._xui_result("GET", "clients/list")
        if listing.get("status") != "succeeded" or not isinstance(listing.get("data"), list):
            return None
        online_result = self._xui_result("POST", "clients/onlines", {})
        online_emails = set(online_result.get("data") or []) if online_result.get("status") == "succeeded" else set()
        users = []
        for item in listing["data"]:
            user = self._normalise_user(item, online_emails=online_emails)
            if user:
                users.append(user)
        return users

    def _get_raw_result(self, username: str) -> dict:
        result = self._xui_result("GET", f"clients/get/{quote(str(username), safe='')}")
        if result.get("status") == "succeeded" and not isinstance(result.get("data"), dict):
            return {**result, "status": "unavailable", "data": None, "error": "invalid_response"}
        return result

    def get_user_result(self, username: str) -> dict:
        result = self._get_raw_result(username)
        if result.get("status") != "succeeded":
            return result
        traffic_result = self._xui_result("GET", f"clients/traffic/{quote(str(username), safe='')}")
        traffic = traffic_result.get("data") if traffic_result.get("status") == "succeeded" else None
        online_result = self._xui_result("POST", "clients/onlines", {})
        online = set(online_result.get("data") or []) if online_result.get("status") == "succeeded" else set()
        user = self._normalise_user(result["data"], online_emails=online, traffic_override=traffic)
        if user is None:
            return {**result, "status": "unavailable", "data": None, "error": "invalid_response"}
        return {**result, "status": "found", "data": user}

    def add_user(
        self,
        username: str,
        traffic_limit: int,
        expiration_days: int,
        unlimited: bool = False,
        note: str | None = None,
        password: str | None = None,
        creation_date: str | None = None,
        blocked: bool = False,
        inbound_ids: list[int] | None = None,
    ):
        ids = _safe_inbound_ids(inbound_ids) or list(self.default_inbound_ids)
        if not ids:
            return None
        try:
            days = int(expiration_days)
            limit_bytes = int(traffic_limit) * GIB
        except (TypeError, ValueError, OverflowError):
            return None
        if days < 0 or limit_bytes < 0:
            return None
        expiry_time = 0 if days == 0 else -days * MILLISECONDS_PER_DAY
        comment = (
            self._comment_without_marker(note)
            if days == 0
            else self._comment_with_duration(note, days)
        )
        client = {
            "email": username,
            "subId": secrets.token_urlsafe(12),
            "totalGB": limit_bytes,
            "expiryTime": expiry_time,
            "tgId": 0,
            "limitIp": self._limit_ip_for_plan(bool(unlimited)),
            "enable": not blocked,
            "comment": comment,
        }
        if password:
            client["auth"] = password
        result = self._xui_result("POST", "clients/add", {"client": client, "inboundIds": ids})
        if result.get("status") != "succeeded":
            return None
        MultiServerAPI.record_created_user(self.server_id, username)
        return {"message": result.get("message") or "Client added", "obj": result.get("data")}

    def create_from_spec(self, spec: UserProvisionSpec):
        inbound_ids = _safe_inbound_ids(spec.inbound_ids) or list(self.default_inbound_ids)
        if not inbound_ids or not spec.password:
            return None
        days = int(spec.expiration_days)
        if days < 0 or int(spec.traffic_limit_bytes) < 0:
            return None
        if days == 0:
            expiry_time = 0
        elif spec.delayed_start:
            expiry_time = -days * MILLISECONDS_PER_DAY
        elif spec.absolute_expiry is not None:
            expiry = spec.absolute_expiry
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            expiry_time = int(expiry.timestamp() * 1000)
        else:
            expiry_time = int((utc_now() + timedelta(days=days)).timestamp() * 1000)
        client = {
            "email": spec.username,
            "subId": secrets.token_urlsafe(12),
            "auth": spec.password,
            "totalGB": int(spec.traffic_limit_bytes),
            "expiryTime": expiry_time,
            "tgId": 0,
            "limitIp": (
                self._limit_ip_for_plan(spec.unlimited_ip)
                if spec.limit_ip is None
                else _safe_nonnegative_int(spec.limit_ip)
            ),
            "enable": not spec.blocked,
            "comment": (
                self._comment_without_marker(spec.note)
                if days == 0
                else self._comment_with_duration(spec.note, days)
            ),
        }
        result = self._xui_result("POST", "clients/add", {"client": client, "inboundIds": inbound_ids})
        if result.get("status") != "succeeded":
            return None
        MultiServerAPI.record_created_user(self.server_id, spec.username)
        return {"message": result.get("message") or "Client added", "obj": result.get("data")}

    def provision_user(self, spec: UserProvisionSpec):
        return self.create_from_spec(spec)

    def update_traffic(self, username: str, upload_bytes: int, download_bytes: int):
        result = self._xui_result(
            "POST",
            f"clients/updateTraffic/{quote(str(username), safe='')}",
            {"upload": _safe_nonnegative_int(upload_bytes), "download": _safe_nonnegative_int(download_bytes)},
        )
        if result.get("status") == "succeeded":
            MultiServerAPI.invalidate_all_caches()
            return {"message": result.get("message") or "Traffic updated"}
        return None

    def update_user(self, username: str, data: dict):
        raw_result = self._get_raw_result(username)
        if raw_result.get("status") != "succeeded":
            return None
        client, _, _ = self._record_parts(raw_result["data"])
        if not client:
            return None
        duration = self._duration_days(client.get("comment"))
        if "new_username" in data:
            client["email"] = str(data["new_username"]).strip()
        if "new_traffic_limit" in data:
            client["totalGB"] = int(data["new_traffic_limit"]) * GIB
        if "traffic_limit" in data:
            client["totalGB"] = int(data["traffic_limit"]) * GIB
        if "new_expiration_days" in data or "expiration_days" in data:
            days = int(data.get("new_expiration_days", data.get("expiration_days")))
            if days < 0:
                return None
            client["expiryTime"] = 0 if days == 0 else -days * MILLISECONDS_PER_DAY
            client["comment"] = (
                self._comment_without_marker(client.get("comment"))
                if days == 0
                else self._comment_with_duration(client.get("comment"), days)
            )
            duration = days
        if "blocked" in data:
            client["enable"] = not _safe_bool(data["blocked"], False)
        if "unlimited_ip" in data:
            client["limitIp"] = self._limit_ip_for_plan(
                _safe_bool(data["unlimited_ip"], False)
            )
        if "note" in data:
            client["comment"] = (
                self._comment_with_duration(data.get("note"), duration)
                if duration and duration > 0
                else self._comment_without_marker(data.get("note")) or ""
            )
        if data.get("renew_password"):
            if client.get("auth") is not None:
                client["auth"] = secrets.token_urlsafe(18)
            elif client.get("password") is not None:
                client["password"] = secrets.token_urlsafe(18)
            elif client.get("uuid") is not None:
                client["uuid"] = str(uuid.uuid4())
            elif isinstance(client.get("id"), str):
                client["id"] = str(uuid.uuid4())
            else:
                return None
        if data.get("renew_creation_date"):
            if not duration:
                return None
            client["expiryTime"] = int((utc_now() + timedelta(days=duration)).timestamp() * 1000)
        result = self._xui_result(
            "POST",
            f"clients/update/{quote(str(username), safe='')}",
            self._client_update_payload(client),
        )
        if result.get("status") == "succeeded":
            MultiServerAPI.invalidate_all_caches()
            return {"message": result.get("message") or "Client updated"}
        return None

    def reset_user_result(self, username: str) -> dict:
        raw_result = self._get_raw_result(username)
        if raw_result.get("status") != "succeeded":
            return raw_result
        client, _, _ = self._record_parts(raw_result["data"])
        duration = self._duration_days(client.get("comment"))
        raw_expiry_time = client.get("expiryTime")
        try:
            expiry_time = int(raw_expiry_time) if raw_expiry_time is not None else None
        except (TypeError, ValueError, OverflowError):
            expiry_time = None
        unlimited_duration = expiry_time == 0
        if not duration and not unlimited_duration:
            return {"status": "failed", "data": None, "http_status": None, "error": "duration_unknown"}
        reset = self._xui_result("POST", f"clients/resetTraffic/{quote(str(username), safe='')}", {})
        if reset.get("status") != "succeeded":
            return reset
        client["expiryTime"] = 0 if unlimited_duration else -duration * MILLISECONDS_PER_DAY
        if unlimited_duration:
            client["comment"] = self._comment_without_marker(client.get("comment")) or ""
        client["enable"] = True
        updated = self._xui_result(
            "POST",
            f"clients/update/{quote(str(username), safe='')}",
            self._client_update_payload(client),
        )
        if updated.get("status") != "succeeded":
            return updated
        MultiServerAPI.invalidate_all_caches()
        return {"status": "succeeded", "data": {"message": "Client reset"}, "http_status": 200, "error": None}

    def _verify_renewed_user_result(
        self,
        username: str,
        traffic_limit_gb: int,
        expiration_days: int,
        unlimited_ip: bool,
    ) -> dict:
        lookup = self._get_raw_result(username)
        if lookup.get("status") != "succeeded":
            return _renewal_failure(lookup, "verify")
        traffic_lookup = self._xui_result(
            "GET", f"clients/traffic/{quote(str(username), safe='')}"
        )
        if traffic_lookup.get("status") != "succeeded":
            return _renewal_failure(traffic_lookup, "verify")
        verified_user = self._normalise_user(
            lookup.get("data"), traffic_override=traffic_lookup.get("data")
        )
        if not _renewal_postconditions(
            verified_user, traffic_limit_gb, expiration_days, unlimited_ip
        ):
            return {
                "status": "failed", "data": verified_user,
                "http_status": lookup.get("http_status"),
                "error": "verification_failed", "stage": "verify",
            }
        return {
            "status": "succeeded", "data": {"message": "Client renewed"},
            "user": verified_user, "http_status": 200, "error": None,
            "stage": "verify",
        }

    def renew_user_result(
        self,
        username: str,
        traffic_limit_gb: int,
        expiration_days: int,
        unlimited_ip: bool = False,
    ) -> dict:
        """Replace 3x-ui plan fields, reset traffic, and verify the result."""
        try:
            traffic_limit_gb = int(traffic_limit_gb)
            expiration_days = int(expiration_days)
        except (TypeError, ValueError, OverflowError):
            return {
                "status": "failed", "data": None, "http_status": None,
                "error": "invalid_plan", "stage": "reconfigure",
            }
        if traffic_limit_gb <= 0 or expiration_days <= 0:
            return {
                "status": "failed", "data": None, "http_status": None,
                "error": "invalid_plan", "stage": "reconfigure",
            }

        raw_result = self._get_raw_result(username)
        if raw_result.get("status") != "succeeded":
            return _renewal_failure(raw_result, "reconfigure")
        client, _, _ = self._record_parts(raw_result.get("data"))
        if not client:
            return {
                "status": "failed", "data": None,
                "http_status": raw_result.get("http_status"),
                "error": "invalid_response", "stage": "reconfigure",
            }
        client["totalGB"] = traffic_limit_gb * GIB
        client["expiryTime"] = -expiration_days * MILLISECONDS_PER_DAY
        client["limitIp"] = self._limit_ip_for_plan(bool(unlimited_ip))
        client["comment"] = self._comment_with_duration(client.get("comment"), expiration_days)
        client["enable"] = True

        updated = self._xui_result(
            "POST",
            f"clients/update/{quote(str(username), safe='')}",
            self._client_update_payload(client),
        )
        if updated.get("status") != "succeeded":
            return _renewal_failure(updated, "reconfigure")
        reset = self._xui_result(
            "POST", f"clients/resetTraffic/{quote(str(username), safe='')}", {}
        )
        if reset.get("status") != "succeeded":
            verified = self._verify_renewed_user_result(
                username, traffic_limit_gb, expiration_days, bool(unlimited_ip)
            )
            if verified.get("status") == "succeeded":
                MultiServerAPI.invalidate_all_caches()
                return verified
            return _renewal_failure(reset, "reset")
        MultiServerAPI.invalidate_all_caches()
        return self._verify_renewed_user_result(
            username, traffic_limit_gb, expiration_days, bool(unlimited_ip)
        )

    def delete_user(self, username: str):
        result = self._xui_result("POST", f"clients/del/{quote(str(username), safe='')}?keepTraffic=0", {})
        if result.get("status") == "succeeded":
            MultiServerAPI.invalidate_all_caches()
            return {"message": result.get("message") or "Client deleted"}
        return None

    @staticmethod
    def _settings_dict(data) -> dict:
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            result = {}
            for item in data:
                if isinstance(item, dict) and item.get("key") is not None:
                    result[str(item["key"])] = item.get("value")
            return result
        return {}

    def get_settings(self) -> dict | None:
        result = self._xui_result("POST", "setting/all", {})
        return self._settings_dict(result.get("data")) if result.get("status") == "succeeded" else None

    def get_inbound_options(self) -> list[dict] | None:
        result = self._xui_result("GET", "inbounds/options")
        if result.get("status") != "succeeded" or not isinstance(result.get("data"), list):
            return None
        options = []
        for item in result["data"]:
            if not isinstance(item, dict):
                continue
            inbound_id = item.get("id")
            try:
                inbound_id = int(inbound_id)
            except (TypeError, ValueError):
                continue
            options.append({
                **item,
                "id": inbound_id,
                "remark": item.get("remark") or item.get("tag") or f"Inbound {inbound_id}",
                "protocol": str(item.get("protocol") or "").lower(),
            })
        return options

    def is_creation_ready(self, verify_remote: bool = False) -> tuple[bool, str | None]:
        if not self.default_inbound_ids:
            return False, "default_inbounds_missing"
        if not verify_remote:
            return True, None
        options = self.get_inbound_options()
        if options is None:
            return False, "inbounds_unavailable"
        available = {item["id"] for item in options}
        if any(inbound_id not in available for inbound_id in self.default_inbound_ids):
            return False, "default_inbounds_invalid"
        settings = self.get_settings()
        if settings is None:
            return False, "settings_unavailable"
        sub_uri = str(settings.get("subURI") or "").strip()
        if not sub_uri or not _safe_bool(settings.get("subEnable", True), True):
            return False, "public_subscription_missing"
        return True, None

    def get_user_uri(self, username: str):
        raw_result = self._get_raw_result(username)
        if raw_result.get("status") != "succeeded":
            return None
        client, _, _ = self._record_parts(raw_result["data"])
        sub_id = str(client.get("subId") or "").strip()
        settings = self.get_settings()
        sub_uri = str((settings or {}).get("subURI") or "").strip()
        if sub_uri and sub_id and _safe_bool((settings or {}).get("subEnable", True), True):
            return {"normal_sub": f"{sub_uri}{sub_id}", "ipv4": "", "direct": False}
        links = self._xui_result("GET", f"clients/links/{quote(str(username), safe='')}")
        direct_links = links.get("data") if links.get("status") == "succeeded" else None
        if isinstance(direct_links, list) and direct_links:
            return {"normal_sub": str(direct_links[0]), "ipv4": "", "direct": True, "links": direct_links}
        return None


def create_panel_client(server_config: dict | None = None):
    """Construct the correct adapter while preserving legacy APIClient use."""
    if server_config and _normalise_panel_type(server_config.get("panel") or server_config.get("panel_type")) == THREE_X_UI_PANEL:
        return ThreeXUIAPIClient(server_config)
    return APIClient(server_config)


class MultiServerAPI:
    """Coordinates API operations across configured VPN servers."""

    _creation_cache_lock = threading.RLock()
    _creation_write_lock = threading.RLock()
    _creation_refresh_lock = threading.Lock()
    _user_snapshot_refresh_lock = threading.Lock()
    _creation_cache = None
    _user_snapshot_cache = {}

    def __init__(self):
        self.servers = get_server_configs()

    @staticmethod
    def server_placement_status(
        server: dict,
        *,
        healthy: bool,
        creation_ready: bool,
    ) -> tuple[bool, str]:
        return server_placement_status(
            server,
            healthy=healthy,
            creation_ready=creation_ready,
        )

    def get_client(self, server_id: str | None = None) -> APIClient | ThreeXUIAPIClient | None:
        if not self.servers:
            return None
        if server_id:
            for server in self.servers:
                if server["id"] == server_id:
                    return create_panel_client(server)
        return create_panel_client(self.servers[0])

    def iter_clients(self, include_disabled: bool = False):
        for server in self.servers:
            if include_disabled or server.get("enabled", True):
                yield server, create_panel_client(server)

    @staticmethod
    def _max_parallel_workers(server_count: int) -> int:
        try:
            configured = int(os.getenv("SERVER_FETCH_WORKERS", "8"))
        except (TypeError, ValueError):
            configured = 8
        return max(1, min(server_count, configured))

    def _client_entries(self, include_disabled: bool = False) -> list[dict]:
        entries = []
        for server in self.servers:
            if include_disabled or server.get("enabled", True):
                entries.append({
                    "server": server,
                    "client": create_panel_client(server),
                    "index": len(entries),
                })
        return entries

    @staticmethod
    def _client_creation_readiness(client, healthy: bool) -> tuple[bool, str | None]:
        if not healthy:
            return False, "server_unavailable"
        checker = getattr(client, "is_creation_ready", None)
        return checker(verify_remote=True) if callable(checker) else (True, None)

    def _fetch_users_for_servers(self, include_disabled: bool = False) -> list[dict]:
        entries = self._client_entries(include_disabled=include_disabled)
        if len(entries) <= 1 or self._max_parallel_workers(len(entries)) == 1:
            for entry in entries:
                entry["users"] = entry["client"].get_users()
            return entries

        results = [None] * len(entries)

        def fetch_users(entry):
            return {**entry, "users": entry["client"].get_users()}

        with ThreadPoolExecutor(max_workers=self._max_parallel_workers(len(entries)), thread_name_prefix="ajib-api") as executor:
            future_to_index = {executor.submit(fetch_users, entry): index for index, entry in enumerate(entries)}
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as e:
                    entry = entries[index]
                    print(f"[MultiServerAPI] Fetch users for {entry['server'].get('id')} failed: {e}")
                    results[index] = {**entry, "users": None}

        return results

    def _build_user_snapshot(self, include_disabled: bool = False) -> dict:
        return {
            "created_at": time.monotonic(),
            "signature": (bool(include_disabled), self._servers_signature(self.servers)),
            "include_disabled": bool(include_disabled),
            "entries": self._fetch_users_for_servers(include_disabled=include_disabled),
        }

    def _get_user_snapshot(
        self,
        include_disabled: bool = False,
        force_refresh: bool = False,
        cache_ttl_seconds: float | None = None,
    ) -> dict:
        signature = (bool(include_disabled), self._servers_signature(self.servers))
        ttl = self._user_snapshot_cache_ttl_seconds(cache_ttl_seconds)
        now = time.monotonic()
        cache_key = bool(include_disabled)

        with self._creation_cache_lock:
            cached = self.__class__._user_snapshot_cache.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and cached.get("signature") == signature
                and ttl > 0
                and now - cached.get("created_at", 0) < ttl
            ):
                self.last_user_snapshot_cache_hit = True
                return cached

            has_matching_cached = cached is not None and cached.get("signature") == signature

        refresh_acquired = self.__class__._user_snapshot_refresh_lock.acquire(blocking=not has_matching_cached or force_refresh)
        if not refresh_acquired:
            self.last_user_snapshot_cache_hit = True
            return cached

        try:
            now = time.monotonic()
            with self._creation_cache_lock:
                cached = self.__class__._user_snapshot_cache.get(cache_key)
                if (
                    not force_refresh
                    and cached is not None
                    and cached.get("signature") == signature
                    and ttl > 0
                    and now - cached.get("created_at", 0) < ttl
                ):
                    self.last_user_snapshot_cache_hit = True
                    return cached

            snapshot = self._build_user_snapshot(include_disabled=include_disabled)
            with self._creation_cache_lock:
                self.__class__._user_snapshot_cache[cache_key] = snapshot
            self.last_user_snapshot_cache_hit = False
            return snapshot
        finally:
            self.__class__._user_snapshot_refresh_lock.release()

    @classmethod
    def invalidate_read_snapshot_cache(cls):
        with cls._creation_cache_lock:
            cls._user_snapshot_cache = {}

    @classmethod
    def invalidate_all_caches(cls):
        with cls._creation_cache_lock:
            cls._creation_cache = None
            cls._user_snapshot_cache = {}

    def invalidate_user_snapshot_cache(self):
        self.__class__.invalidate_read_snapshot_cache()

    @staticmethod
    def allocated_user_count(users) -> int:
        """Placement load: every unblocked account, including On-hold."""
        count = 0
        if isinstance(users, dict):
            iterable = users.values()
        elif isinstance(users, list):
            iterable = users
        else:
            return 0
        for user in iterable:
            if isinstance(user, dict) and not bool(user.get("blocked", False)):
                count += 1
        return count

    @staticmethod
    def active_user_count(users) -> int:
        """Backward-compatible alias for the placement allocation count."""
        return MultiServerAPI.allocated_user_count(users)

    @staticmethod
    def account_state_counts(users) -> dict:
        counts = {
            "allocated": 0,
            "started": 0,
            "online": 0,
            "offline": 0,
            "hold": 0,
            "blocked": 0,
            "unknown": 0,
            # Compatibility alias: state snapshots historically called every
            # started (Online or Offline) account "active".
            "active": 0,
        }
        if isinstance(users, dict):
            iterable = users.values()
        elif isinstance(users, list):
            iterable = users
        else:
            return counts
        for user in iterable:
            snapshot = inspect_account(user if isinstance(user, dict) else None)
            if snapshot.blocked is False:
                counts["allocated"] += 1
            if snapshot.panel_state == PanelState.CONNECTED:
                counts["started"] += 1
                counts["active"] += 1
                if snapshot.normalized_status == "online":
                    counts["online"] += 1
                elif snapshot.normalized_status == "offline":
                    counts["offline"] += 1
            elif snapshot.panel_state == PanelState.HOLD:
                counts["hold"] += 1
            elif snapshot.panel_state == PanelState.BLOCKED:
                counts["blocked"] += 1
            else:
                counts["unknown"] += 1
        return counts

    @staticmethod
    def extract_usernames(users) -> set[str]:
        names = set()
        if isinstance(users, dict):
            names.update(str(name) for name in users.keys() if name)
        elif isinstance(users, list):
            for item in users:
                if isinstance(item, dict) and item.get("username"):
                    names.add(str(item["username"]))
        return names

    @staticmethod
    def _creation_cache_ttl_seconds() -> float:
        load_dotenv(TELEGRAM_ENV_PATH)
        try:
            ttl = float(os.getenv("SERVER_USERS_CACHE_TTL_SECONDS", "30"))
        except (TypeError, ValueError):
            return 30.0
        return max(0.0, ttl)

    @classmethod
    def _user_snapshot_cache_ttl_seconds(cls, cache_ttl_seconds: float | None = None) -> float:
        if cache_ttl_seconds is None:
            return cls._creation_cache_ttl_seconds()
        try:
            ttl = float(cache_ttl_seconds)
        except (TypeError, ValueError):
            return cls._creation_cache_ttl_seconds()
        return max(0.0, ttl)

    @staticmethod
    def _servers_signature(servers: list[dict]):
        return tuple(
            (
                server.get("id"),
                server.get("url"),
                server.get("token"),
                bool(server.get("enabled", True)),
                _safe_weight(server.get("weight", 1)),
                _normalise_panel_type(server.get("panel")),
                tuple(_safe_inbound_ids(server.get("default_inbound_ids"))),
                _safe_nonnegative_int(server.get("default_limit_ip"), 0),
            )
            for server in servers
        )

    def _build_creation_snapshot(self, force_refresh: bool = False) -> dict:
        usernames = set()
        server_states = []

        for entry in self._get_user_snapshot(include_disabled=False, force_refresh=force_refresh).get("entries", []):
            server = entry["server"]
            client = entry["client"]
            users = entry["users"]
            healthy = users is not None
            readiness = self._client_creation_readiness(client, healthy)
            creation_ready, creation_error = readiness
            allocated_count = self.allocated_user_count(users) if healthy else None
            weight = _safe_weight(server.get("weight", 1))
            accepting_new_users, placement_reason = self.server_placement_status(
                server,
                healthy=healthy,
                creation_ready=bool(creation_ready),
            )
            if healthy:
                usernames.update(self.extract_usernames(users))
            server_states.append({
                "server": server,
                "client": client,
                "index": entry["index"],
                "healthy": healthy,
                "creation_ready": bool(creation_ready),
                "creation_error": creation_error,
                "accepting_new_users": accepting_new_users,
                "placement_reason": placement_reason,
                "active_count": allocated_count,
                "allocated_count": allocated_count,
                "weight": weight,
                "load_ratio": (allocated_count / weight) if healthy and weight > 0 else None,
            })

        return {
            "created_at": time.monotonic(),
            "signature": self._servers_signature(self.servers),
            "usernames": usernames,
            "servers": server_states,
        }

    def _get_creation_snapshot(self, force_refresh: bool = False) -> dict:
        signature = self._servers_signature(self.servers)
        ttl = self._creation_cache_ttl_seconds()
        now = time.monotonic()

        with self._creation_cache_lock:
            cached = self.__class__._creation_cache
            if (
                not force_refresh
                and cached is not None
                and cached.get("signature") == signature
                and ttl > 0
                and now - cached.get("created_at", 0) < ttl
            ):
                return cached

        with self.__class__._creation_refresh_lock:
            now = time.monotonic()
            with self._creation_cache_lock:
                cached = self.__class__._creation_cache
                if (
                    not force_refresh
                    and cached is not None
                    and cached.get("signature") == signature
                    and ttl > 0
                    and now - cached.get("created_at", 0) < ttl
                ):
                    return cached

            snapshot = self._build_creation_snapshot(force_refresh=force_refresh)
            with self._creation_cache_lock:
                self.__class__._creation_cache = snapshot
            return snapshot

    def invalidate_creation_cache(self):
        self.__class__.invalidate_all_caches()

    def prepare_new_user_creation(self, force_refresh: bool = False) -> dict:
        snapshot = self._get_creation_snapshot(force_refresh=force_refresh)
        candidates = []
        for state in snapshot.get("servers", []):
            if not state.get("accepting_new_users", False):
                continue
            candidates.append((state["load_ratio"], state["index"], state["client"]))

        selected_client = None
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            selected_client = candidates[0][2]

        return {
            "client": selected_client,
            "existing_usernames": set(snapshot.get("usernames", set())),
            "server_states": list(snapshot.get("servers", [])),
        }

    @classmethod
    def record_created_user(cls, server_id: str, username: str):
        if not username:
            return
        with cls._creation_cache_lock:
            cls._user_snapshot_cache = {}
            cached = cls._creation_cache
            if cached is None:
                return
            usernames = cached.setdefault("usernames", set())
            if username in usernames:
                return
            usernames.add(username)
            for state in cached.get("servers", []):
                client = state.get("client")
                state_server_id = getattr(client, "server_id", None) or (state.get("server") or {}).get("id")
                if state_server_id != server_id:
                    continue
                if state.get("healthy"):
                    allocated_count = int(
                        state.get("allocated_count", state.get("active_count")) or 0
                    ) + 1
                    state["active_count"] = allocated_count
                    state["allocated_count"] = allocated_count
                    weight = _safe_weight(state.get("weight", 1))
                    state["load_ratio"] = allocated_count / weight if weight > 0 else None
                break

    def create_user_with_retry(
        self,
        username_allocator,
        creator,
        fallback_client: APIClient | None = None,
        on_username_allocated=None,
        reuse_username_on_retry: bool = False,
    ):
        with self.__class__._creation_write_lock:
            last_username = None
            last_client = None

            for attempt in range(2):
                if attempt > 0 and reuse_username_on_retry and last_client is not None:
                    target_client = last_client
                    username = last_username
                else:
                    creation = self.prepare_new_user_creation(force_refresh=attempt > 0)
                    target_client = creation.get("client")
                    if target_client is None and not self.servers:
                        target_client = fallback_client
                    if target_client is None:
                        return None, None, None

                    existing_usernames = set(creation.get("existing_usernames") or set())
                    if not existing_usernames and fallback_client is not None and target_client is fallback_client:
                        users = fallback_client.get_users()
                        existing_usernames = self.extract_usernames(users)

                    username = username_allocator(existing_usernames)
                if on_username_allocated is not None:
                    on_username_allocated(username, target_client)
                result = creator(target_client, username)
                last_username = username
                last_client = target_client
                if result is not None:
                    self.record_created_user(target_client.server_id, username)
                    return username, result, target_client

                self.invalidate_creation_cache()

            return last_username, None, last_client

    def get_server_statuses(self) -> list[dict]:
        statuses = []
        for entry in self._fetch_users_for_servers(include_disabled=True):
            server = entry["server"]
            users = entry["users"]
            healthy = users is not None
            client = entry["client"]
            creation_ready, creation_error = self._client_creation_readiness(client, healthy)
            allocated_count = self.allocated_user_count(users)
            state_counts = self.account_state_counts(users) if healthy else {
                "allocated": None,
                "started": None,
                "online": None,
                "offline": None,
                "active": None,
                "hold": None,
                "blocked": None,
                "unknown": None,
            }
            weight = _safe_weight(server.get("weight", 1))
            accepting_new_users, placement_reason = self.server_placement_status(
                server,
                healthy=healthy,
                creation_ready=bool(creation_ready),
            )
            statuses.append({
                **server,
                "index": entry["index"],
                "healthy": healthy,
                "panel": getattr(client, "panel_type", server.get("panel", BLITZ_PANEL)),
                "creation_ready": bool(creation_ready),
                "creation_error": creation_error,
                "accepting_new_users": accepting_new_users,
                "placement_reason": placement_reason,
                "active_count": allocated_count if healthy else None,
                "allocated_count": allocated_count if healthy else None,
                "connected_count": state_counts["active"],
                "started_count": state_counts["started"],
                "online_count": state_counts["online"],
                "offline_count": state_counts["offline"],
                "hold_count": state_counts["hold"],
                "blocked_count": state_counts["blocked"],
                "unknown_count": state_counts["unknown"],
                "load_ratio": (allocated_count / weight) if healthy and weight > 0 else None,
            })
        return statuses

    def select_server_for_new_user(self) -> APIClient | None:
        return self.prepare_new_user_creation().get("client")

    def get_all_usernames(self) -> set[str]:
        usernames = set()
        for entry in self._get_user_snapshot(include_disabled=True).get("entries", []):
            users = entry["users"]
            if users is not None:
                usernames.update(self.extract_usernames(users))
        return usernames

    def _copy_user(
        self,
        source_ref: UserRef,
        destination_server_id: str,
        inbound_ids: list[int] | None = None,
    ) -> dict:
        """Copy a supported live account without changing its source identity.

        Every post-create failure attempts to remove only the newly-created
        destination.  The returned error code is safe to show or log and never
        contains credential values.
        """
        source_client, source, source_result = self.find_user_on_server(
            source_ref.username,
            source_ref.server_id,
        )
        if source_result.get("status") != "found" or source is None:
            return {"ok": False, "error": f"source_{source_result.get('status', 'unavailable')}"}
        source_panel = getattr(source_client, "panel_type", BLITZ_PANEL)
        if source_panel not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
            return {"ok": False, "error": "source_panel_not_supported"}

        destination = self.get_client(destination_server_id)
        if destination is None or destination.server_id == source_client.server_id:
            return {"ok": False, "error": "destination_invalid"}
        if destination.panel_type not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
            return {"ok": False, "error": "destination_panel_not_supported"}
        if source_panel == THREE_X_UI_PANEL and destination.panel_type != BLITZ_PANEL:
            return {"ok": False, "error": "destination_panel_not_supported"}
        destination_result = destination.get_user_result(source_ref.username)
        if destination_result.get("status") == "found":
            return {"ok": False, "error": "destination_exists"}
        if destination_result.get("status") != "missing":
            return {"ok": False, "error": "destination_unavailable"}

        password = source.get("password")
        if source_panel == THREE_X_UI_PANEL:
            credential_metadata = source.get("credential_metadata")
            credential_fields = (
                credential_metadata.get("fields_present")
                if isinstance(credential_metadata, dict)
                else []
            )
            selected_field = (
                credential_metadata.get("selected_field")
                if isinstance(credential_metadata, dict)
                else None
            )
            if (
                "auth" not in (credential_fields or [])
                or selected_field != "auth"
                or not isinstance(password, str)
                or not password.strip()
            ):
                return {"ok": False, "error": "source_auth_missing"}
        elif not isinstance(password, str) or not password:
            return {"ok": False, "error": "source_password_missing"}

        def required_int(field, minimum=0):
            value = source.get(field)
            if isinstance(value, bool):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if parsed >= minimum else None

        def parsed_nonnegative(value):
            if isinstance(value, bool):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if parsed >= 0 else None

        def parsed_destination_counter(value):
            # A newly-created started Blitz account has no traffic row until
            # its first connection, so 2.5.x reports null counters. Within
            # this owned post-create verification that state is exactly zero.
            if destination.panel_type == BLITZ_PANEL and value is None:
                return 0
            return parsed_nonnegative(value)

        total_bytes = required_int("max_download_bytes")
        upload_bytes = required_int("upload_bytes")
        download_bytes = required_int("download_bytes")
        duration_days = required_int("expiration_days", minimum=0)
        blocked = source.get("blocked") if isinstance(source.get("blocked"), bool) else None
        access_field = "unlimited_ip" if source_panel == THREE_X_UI_PANEL else "unlimited_user"
        unlimited_access = source.get(access_field) if isinstance(source.get(access_field), bool) else None
        if None in (total_bytes, upload_bytes, download_bytes, duration_days, blocked, unlimited_access):
            return {"ok": False, "error": "source_state_malformed"}

        status = " ".join(str(source.get("status") or "").lower().replace("-", " ").replace("_", " ").split())
        delayed_start = source.get("delayed_start")
        if not isinstance(delayed_start, bool):
            timer_started = source.get("timer_started")
            delayed_start = (timer_started is False) if isinstance(timer_started, bool) else status == "on hold"
        unlimited_duration = duration_days == 0
        if unlimited_duration:
            delayed_start = False
        expiry = panel_deadline(source)
        if not unlimited_duration and not delayed_start and expiry is None:
            return {"ok": False, "error": "source_state_malformed"}
        note = _canonical_note(source.get("note"))

        if source_panel == THREE_X_UI_PANEL:
            source_options = source_client.get_inbound_options()
            if source_options is None:
                return {"ok": False, "error": "source_inbounds_unavailable"}
            source_option_map = {item["id"]: item for item in source_options}
            source_inbound_ids = _safe_inbound_ids(source.get("inbound_ids"))
            allowed_protocols = {"hysteria", "hysteria2", "hy2"}
            if not any(
                inbound_id in source_option_map
                and str(source_option_map[inbound_id].get("protocol") or "").lower() in allowed_protocols
                for inbound_id in source_inbound_ids
            ):
                return {"ok": False, "error": "source_not_hysteria2"}

        snapshot = _UserTransferSnapshot(
            username=source_ref.username,
            source_panel_type=source_panel,
            password=password,
            total_bytes=total_bytes,
            upload_bytes=upload_bytes,
            download_bytes=download_bytes,
            duration_days=duration_days,
            blocked=blocked,
            delayed_start=bool(delayed_start),
            absolute_expiry=expiry,
            account_creation_date=(str(source.get("account_creation_date") or "").strip() or None),
            note=note,
            unlimited_access=unlimited_access,
        )

        selected_inbounds = []
        expected_expiry = snapshot.absolute_expiry
        blitz_creation_date = None
        expiry_extension_seconds = 0.0
        if destination.panel_type == THREE_X_UI_PANEL:
            selected_inbounds = _safe_inbound_ids(inbound_ids)
            if not selected_inbounds:
                return {"ok": False, "error": "inbounds_required"}
            options = destination.get_inbound_options()
            if options is None:
                return {"ok": False, "error": "inbounds_unavailable"}
            option_map = {item["id"]: item for item in options}
            allowed_protocols = {"hysteria", "hysteria2", "hy2"}
            if any(
                inbound_id not in option_map
                or str(option_map[inbound_id].get("protocol") or "").lower() not in allowed_protocols
                for inbound_id in selected_inbounds
            ):
                return {"ok": False, "error": "inbounds_not_hysteria2"}
            destination_limit = snapshot.total_bytes
        else:
            # Blitz's ``unlimited_user`` flag describes access/IP policy and
            # can coexist with a finite byte quota. Only a non-positive byte
            # limit is an unlimited allowance that cannot be re-imported.
            if snapshot.total_bytes <= 0:
                return {"ok": False, "error": "blitz_unlimited_not_representable"}
            remaining_bytes = snapshot.total_bytes - snapshot.upload_bytes - snapshot.download_bytes
            if remaining_bytes <= 0:
                return {"ok": False, "error": "blitz_allowance_exhausted"}
            destination_limit = int(math.ceil(remaining_bytes / GIB)) * GIB
            if snapshot.duration_days == 0:
                source_creation = parse_timestamp(snapshot.account_creation_date)
                blitz_creation_date = (
                    source_creation.date().isoformat()
                    if source_creation is not None
                    else utc_now().date().isoformat()
                )
            elif not snapshot.delayed_start:
                expected_expiry = _ceil_utc_day(snapshot.absolute_expiry)
                expiry_extension_seconds = max(
                    0.0,
                    (expected_expiry - snapshot.absolute_expiry).total_seconds(),
                )
                blitz_creation_date = (
                    expected_expiry - timedelta(days=snapshot.duration_days)
                ).date().isoformat()

        spec = UserProvisionSpec(
            username=snapshot.username,
            traffic_limit_bytes=destination_limit,
            expiration_days=snapshot.duration_days,
            password=_destination_transfer_password(
                snapshot.username,
                snapshot.password,
                snapshot.source_panel_type,
                destination.panel_type,
            ),
            creation_date=blitz_creation_date,
            absolute_expiry=snapshot.absolute_expiry,
            delayed_start=snapshot.delayed_start,
            upload_bytes=snapshot.upload_bytes if destination.panel_type == THREE_X_UI_PANEL else 0,
            download_bytes=snapshot.download_bytes if destination.panel_type == THREE_X_UI_PANEL else 0,
            blocked=False,
            unlimited_ip=snapshot.unlimited_access,
            note=snapshot.note,
            inbound_ids=selected_inbounds,
            limit_ip=None,
        )

        created = False

        def fail(error_code):
            if not created:
                return {"ok": False, "error": error_code}
            rollback = destination.delete_user(source_ref.username)
            self.invalidate_all_caches()
            if rollback is None:
                return {
                    "ok": False,
                    "error": error_code,
                    "rollback_failed": True,
                    "partial_destination": destination.server_id,
                }
            return {"ok": False, "error": error_code, "rolled_back": True}

        defer_note = False
        if destination.panel_type == THREE_X_UI_PANEL:
            creation_result = destination.create_from_spec(spec)
        else:
            # Blitz 2.5.x duplicates its positional ``false`` argument when a
            # limited started user has both a note and creation date. Defer the
            # note to a verified PATCH so existing servers remain compatible.
            defer_note = bool(spec.creation_date and spec.note is not None)
            creation_result = destination.add_user(
                spec.username,
                int(destination_limit // GIB),
                spec.expiration_days,
                unlimited=spec.unlimited_ip,
                note=None if defer_note else spec.note,
                password=spec.password,
                creation_date=spec.creation_date,
                blocked=False,
            )
        if creation_result is None:
            post_create = destination.get_user_result(spec.username)
            if post_create.get("status") == "found":
                return {
                    "ok": False,
                    "error": "destination_create_outcome_unknown",
                    "partial_destination": destination.server_id,
                }
            return {"ok": False, "error": "destination_create_failed"}
        created = True

        if destination.panel_type == THREE_X_UI_PANEL:
            if destination.update_traffic(spec.username, snapshot.upload_bytes, snapshot.download_bytes) is None:
                return fail("traffic_import_failed")
        elif defer_note and destination.update_user(spec.username, {"note": spec.note}) is None:
            return fail("destination_note_failed")

        uri_data = destination.get_user_uri(spec.username)
        if not isinstance(uri_data, dict) or not uri_data.get("normal_sub"):
            return fail("destination_uri_failed")

        if snapshot.blocked and destination.update_user(spec.username, {"blocked": True}) is None:
            return fail("destination_state_failed")

        verification = destination.get_user_result(spec.username)
        copied = verification.get("data") if verification.get("status") == "found" else None
        if not isinstance(copied, dict):
            return fail("destination_verification_failed")
        expected_upload = snapshot.upload_bytes if destination.panel_type == THREE_X_UI_PANEL else 0
        expected_download = snapshot.download_bytes if destination.panel_type == THREE_X_UI_PANEL else 0
        copied_access = (
            copied.get("unlimited_ip")
            if destination.panel_type == THREE_X_UI_PANEL
            else copied.get("unlimited_user")
        )
        verified = (
            copied.get("password") == spec.password
            and parsed_nonnegative(copied.get("max_download_bytes")) == destination_limit
            and parsed_destination_counter(copied.get("upload_bytes")) == expected_upload
            and parsed_destination_counter(copied.get("download_bytes")) == expected_download
            and copied.get("blocked") is snapshot.blocked
            and parsed_nonnegative(copied.get("expiration_days")) == snapshot.duration_days
            and copied_access is snapshot.unlimited_access
            and _canonical_note(copied.get("note")) == snapshot.note
        )
        if snapshot.duration_days == 0:
            verified = (
                verified
                and copied.get("delayed_start") is False
                and panel_deadline(copied) is None
            )
        elif snapshot.delayed_start:
            copied_status = " ".join(
                str(copied.get("status") or "").lower().replace("-", " ").replace("_", " ").split()
            )
            verified = verified and (
                copied.get("delayed_start") is True
                or (copied_status == "on hold" and not copied.get("account_creation_date"))
            )
        elif expected_expiry is not None:
            copied_expiry = panel_deadline(copied)
            verified = (
                verified
                and copied_expiry is not None
                and abs((copied_expiry - expected_expiry).total_seconds()) <= 2
            )
        if destination.panel_type == THREE_X_UI_PANEL:
            verified = verified and set(selected_inbounds).issubset(set(_safe_inbound_ids(copied.get("inbound_ids"))))
        if not verified:
            return fail("destination_verification_failed")

        self.invalidate_all_caches()
        return {
            "ok": True,
            "username": spec.username,
            "source_server_id": source_client.server_id,
            "source_panel_type": snapshot.source_panel_type,
            "destination_server_id": destination.server_id,
            "destination_server_name": destination.server_name,
            "panel_type": destination.panel_type,
            "inbound_ids": selected_inbounds,
            "normal_sub": uri_data["normal_sub"],
            "direct_link": bool(uri_data.get("direct")),
            "blitz_quota_gib": int(destination_limit // GIB) if destination.panel_type == BLITZ_PANEL else None,
            "expiry_rounded": expiry_extension_seconds > 0,
            "expiry_extension_seconds": expiry_extension_seconds,
        }

    def copy_blitz_user(
        self,
        source_ref: UserRef,
        destination_server_id: str,
        inbound_ids: list[int] | None = None,
    ) -> dict:
        """Backward-compatible wrapper for legacy copy callers."""
        return self._copy_user(source_ref, destination_server_id, inbound_ids)

    def copy_user(self, spec: UserCopySpec) -> dict:
        """Panel-neutral copy entry point for a structured copy request."""
        if not isinstance(spec, UserCopySpec):
            return {"ok": False, "error": "copy_spec_invalid"}
        return self._copy_user(
            spec.source,
            spec.destination_server_id,
            list(spec.inbound_ids),
        )

    def get_user_snapshot_entries(
        self,
        include_disabled: bool = True,
        force_refresh: bool = False,
        cache_ttl_seconds: float | None = None,
    ) -> list[dict]:
        return list(
            self._get_user_snapshot(
                include_disabled=include_disabled,
                force_refresh=force_refresh,
                cache_ttl_seconds=cache_ttl_seconds,
            ).get("entries", [])
        )

    def get_cached_user_snapshot_entries(
        self,
        include_disabled: bool = True,
        cache_ttl_seconds: float | None = None,
        allow_expired: bool = True,
    ) -> list[dict] | None:
        signature = (bool(include_disabled), self._servers_signature(self.servers))
        ttl = self._user_snapshot_cache_ttl_seconds(cache_ttl_seconds)
        now = time.monotonic()
        cache_key = bool(include_disabled)

        with self._creation_cache_lock:
            cached = self.__class__._user_snapshot_cache.get(cache_key)
            if cached is None or cached.get("signature") != signature:
                self.last_user_snapshot_cache_hit = False
                self.last_user_snapshot_cache_stale = None
                return None

            fresh = ttl > 0 and now - cached.get("created_at", 0) < ttl
            if not fresh and not allow_expired:
                self.last_user_snapshot_cache_hit = False
                self.last_user_snapshot_cache_stale = True
                return None

            self.last_user_snapshot_cache_hit = True
            self.last_user_snapshot_cache_stale = not fresh
            return list(cached.get("entries", []))

    def find_user(self, username: str, preferred_server_id: str | None = None):
        if preferred_server_id:
            client = self.get_client(preferred_server_id)
            if client:
                user = client.get_user(username)
                if user is not None:
                    return client, user
        for _, client in self.iter_clients(include_disabled=True):
            if preferred_server_id and client.server_id == preferred_server_id:
                continue
            user = client.get_user(username)
            if user is not None:
                return client, user
        return None, None

    def find_user_matches(self, username: str, force_refresh: bool = True) -> list[dict]:
        """Return every exact ``(server, username)`` identity that matches."""
        target = str(username or "").strip().casefold()
        if not target:
            return []
        matches = []
        for entry in self.get_user_snapshot_entries(
            include_disabled=True,
            force_refresh=force_refresh,
        ):
            client = entry.get("client")
            users = entry.get("users")
            if client is None or users is None:
                continue
            candidates = []
            if isinstance(users, dict):
                candidates = [
                    (str(name), value) for name, value in users.items()
                    if str(name).casefold() == target and isinstance(value, dict)
                ]
            elif isinstance(users, list):
                candidates = [
                    (str(value.get("username") or ""), value) for value in users
                    if isinstance(value, dict)
                    and str(value.get("username") or "").casefold() == target
                ]
            for actual_username, user in candidates:
                matches.append({
                    "ref": UserRef(
                        server_id=str(client.server_id),
                        username=actual_username or str(username),
                        panel_type=getattr(client, "panel_type", BLITZ_PANEL),
                    ),
                    "client": client,
                    "user": user,
                    "server": entry.get("server") or {},
                })
        return matches

    def find_user_on_server(self, username: str, server_id: str):
        """Look up a user only on the exact recorded server.

        Returns ``(client, user_data, result)`` where result contains the
        structured ``found``, ``missing``, or ``unavailable`` status.
        """
        target_server_id = str(server_id or "").strip()
        if not target_server_id:
            result = {
                "status": "unavailable",
                "data": None,
                "http_status": None,
                "error": "server_id_missing",
            }
            return None, None, result

        for server in self.servers:
            if str(server.get("id")) != target_server_id:
                continue
            client = create_panel_client(server)
            result = client.get_user_result(username)
            return client, result.get("data"), result

        result = {
            "status": "unavailable",
            "data": None,
            "http_status": None,
            "error": "server_not_configured",
        }
        return None, None, result

    def find_user_on_server_cached(
        self,
        username: str,
        server_id: str,
        cache_ttl_seconds: float | None = None,
    ):
        """Resolve an exact identity from a fresh snapshot cache, then live."""
        target_server_id = str(server_id or "").strip()
        cached_entries = self.get_cached_user_snapshot_entries(
            include_disabled=True,
            cache_ttl_seconds=cache_ttl_seconds,
            allow_expired=False,
        )
        if cached_entries is not None and target_server_id:
            for entry in cached_entries:
                server = entry.get("server") or {}
                client = entry.get("client")
                entry_server_id = server.get("id") or getattr(client, "server_id", "")
                if str(entry_server_id) != target_server_id:
                    continue
                users = entry.get("users")
                if users is None:
                    return client, None, {
                        "status": "unavailable",
                        "data": None,
                        "http_status": None,
                        "error": "cached_server_unavailable",
                        "source": "cache",
                    }
                target = str(username or "").casefold()
                if isinstance(users, dict):
                    user = users.get(username)
                    if user is None:
                        user = next(
                            (value for name, value in users.items() if str(name).casefold() == target),
                            None,
                        )
                elif isinstance(users, list):
                    user = next(
                        (
                            value for value in users
                            if isinstance(value, dict)
                            and str(value.get("username") or "").casefold() == target
                        ),
                        None,
                    )
                else:
                    user = None
                return client, user, {
                    "status": "found" if user is not None else "missing",
                    "data": user,
                    "http_status": 200,
                    "error": None,
                    "source": "cache",
                }
        return self.find_user_on_server(username, target_server_id)

    def iter_all_users(
        self,
        include_disabled: bool = True,
        force_refresh: bool = False,
        cache_ttl_seconds: float | None = None,
    ):
        for entry in self._get_user_snapshot(
            include_disabled=include_disabled,
            force_refresh=force_refresh,
            cache_ttl_seconds=cache_ttl_seconds,
        ).get("entries", []):
            client = entry["client"]
            users = entry["users"]
            if users is None:
                continue
            if isinstance(users, dict):
                for username, data in users.items():
                    yield client, username, data
            elif isinstance(users, list):
                for data in users:
                    if isinstance(data, dict):
                        yield client, data.get("username"), data
