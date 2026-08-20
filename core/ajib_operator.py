"""Secure operator configuration and service helpers for the ajib CLI.

This module deliberately has no Click dependency.  The interactive and JSON
interfaces live in :mod:`cli`, while this file owns validation, atomic state
changes, remote probes, and server-removal safety checks.
"""

from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

try:  # pragma: no cover - Windows development fallback
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SCHEMA_VERSION = 1
ACTIVE_TRANSFER_STATUSES = {"queued", "running", "cancel_requested"}
_IMPORT_LOCK = threading.RLock()


class OperatorError(Exception):
    """An actionable operator-facing error."""


class ValidationError(OperatorError):
    """Configuration did not pass local validation."""


class ConfigurationConflictError(OperatorError):
    """The configuration changed after an operator began editing it."""


class UnsafeRemovalError(OperatorError):
    """A server cannot be removed safely."""


@dataclass(frozen=True)
class ApplyResult:
    status: str
    message: str
    had_previous: bool
    fingerprint: str
    ready: bool = False
    verified: bool = False

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "healthy" else 2 if self.status == "degraded" else 1


def install_dir() -> Path:
    return Path(os.getenv("AJIB_INSTALL_DIR", "/etc/ajib"))


def bot_dir() -> Path:
    return Path(os.getenv("AJIB_BOT_DIR", str(install_dir() / "core/scripts/telegrambot")))


def env_path() -> Path:
    return Path(os.getenv("AJIB_TELEGRAM_ENV", str(bot_dir() / ".env")))


def previous_env_path() -> Path:
    return Path(os.getenv("AJIB_PREVIOUS_ENV", str(bot_dir() / ".env.previous")))


def database_path() -> Path:
    return Path(os.getenv("AJIB_DB_PATH", str(bot_dir() / "ajib.db")))


def ready_path() -> Path:
    return Path(os.getenv("AJIB_READY_FILE", "/run/ajib/main.ready"))


def backup_dir() -> Path:
    return Path(os.getenv("AJIB_BACKUP_DIR", "/opt/ajib-backups"))


def _clean_secret(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValidationError(f"{label} cannot be empty.")
    if "\n" in result or "\r" in result or "\0" in result:
        raise ValidationError(f"{label} cannot contain line breaks or NUL bytes.")
    if len(result) > 4096:
        raise ValidationError(f"{label} is too long.")
    return result


def normalize_admin_ids(value: Any) -> list[int]:
    if isinstance(value, str):
        raw_values = value.strip().strip("[]").split(",")
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raise ValidationError("Admin IDs must be a list or comma-separated string.")
    result: list[int] = []
    for raw in raw_values:
        text = str(raw).strip()
        if not text or not text.isdigit() or int(text) <= 0:
            raise ValidationError("Admin IDs must be positive numbers separated by commas.")
        parsed = int(text)
        if parsed not in result:
            result.append(parsed)
    if not result:
        raise ValidationError("At least one Telegram administrator ID is required.")
    return result


def normalize_panel(value: Any) -> str:
    panel = str(value or "blitz").strip().lower().replace("_", "-")
    if panel in {"3x", "3xui", "xui", "x-ui"}:
        panel = "3x-ui"
    if panel not in {"blitz", "3x-ui"}:
        raise ValidationError("Panel must be blitz or 3x-ui.")
    return panel


def _normalize_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y", "enabled", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "disabled", "off"}:
        return False
    raise ValidationError("Enabled must be true or false.")


def _normalize_inbound_ids(value: Any) -> list[int]:
    if value in (None, "", []):
        return []
    raw_values = str(value).split("|") if isinstance(value, str) else value
    if not isinstance(raw_values, (list, tuple)):
        raise ValidationError("Inbound IDs must be a list or pipe-separated integers.")
    result: list[int] = []
    for raw in raw_values:
        try:
            inbound_id = int(str(raw).strip())
        except (TypeError, ValueError) as error:
            raise ValidationError("Inbound IDs must be positive integers.") from error
        if inbound_id <= 0:
            raise ValidationError("Inbound IDs must be positive integers.")
        if inbound_id not in result:
            result.append(inbound_id)
    return result


def normalize_server(value: Any, index: int = 0) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Each server must be a JSON object.")
    server_id = str(value.get("id") or value.get("name") or "").strip()
    if not server_id:
        raise ValidationError(f"Server {index + 1} needs an ID.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", server_id):
        raise ValidationError(
            f"Server ID '{server_id}' must start with a letter or number and contain only letters, numbers, '.', '_' or '-'."
        )
    url = str(value.get("url") or value.get("URL") or "").strip()
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValidationError(f"Server '{server_id}' needs a valid HTTP(S) URL.")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValidationError(f"Server '{server_id}' URL cannot contain embedded credentials.")
    if parsed_url.query or parsed_url.fragment:
        raise ValidationError(f"Server '{server_id}' URL cannot contain a query string or fragment.")
    if any(char in url for char in "\r\n\0"):
        raise ValidationError(f"Server '{server_id}' URL contains invalid characters.")
    token = _clean_secret(value.get("token") or value.get("TOKEN"), f"API token for '{server_id}'")
    try:
        weight = float(value.get("weight", 1))
    except (TypeError, ValueError) as error:
        raise ValidationError(f"Server '{server_id}' weight must be a number.") from error
    if not math.isfinite(weight) or weight < 0:
        raise ValidationError(f"Server '{server_id}' weight must be finite and non-negative.")
    weight = 0.0 if weight == 0 else weight
    enabled = _normalize_bool(value.get("enabled"), True)
    panel = normalize_panel(value.get("panel"))
    inbound_ids = _normalize_inbound_ids(
        value.get("default_inbound_ids", value.get("inbound_ids"))
    )
    try:
        limit_ip = int(value.get("default_limit_ip", value.get("limit_ip", 0)) or 0)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"Server '{server_id}' IP limit must be a non-negative integer.") from error
    if limit_ip < 0:
        raise ValidationError(f"Server '{server_id}' IP limit must be a non-negative integer.")
    if panel == "3x-ui" and not inbound_ids:
        raise ValidationError(f"3x-ui server '{server_id}' requires default inbound IDs.")
    return {
        "id": server_id,
        "name": str(value.get("name") or server_id).strip() or server_id,
        "url": url.rstrip("/"),
        "token": token,
        "enabled": enabled,
        "weight": weight,
        "panel": panel,
        "default_inbound_ids": inbound_ids,
        "default_limit_ip": limit_ip,
    }


def normalize_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Configuration must be a JSON object.")
    schema_version = value.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise ValidationError(f"Unsupported configuration schema version: {schema_version!r}.")
    telegram = value.get("telegram")
    if not isinstance(telegram, dict):
        telegram = {
            "token": value.get("telegram_token") or value.get("token"),
            "admin_ids": value.get("admin_ids") or value.get("adminid"),
        }
    token = _clean_secret(telegram.get("token"), "Telegram bot token")
    admin_ids = normalize_admin_ids(telegram.get("admin_ids"))
    raw_servers = value.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ValidationError("At least one VPN server is required.")
    servers = [normalize_server(server, index) for index, server in enumerate(raw_servers)]
    seen: dict[str, str] = {}
    for server in servers:
        folded = server["id"].casefold()
        if folded in seen:
            raise ValidationError(
                f"Server IDs '{seen[folded]}' and '{server['id']}' conflict; IDs are case-insensitive."
            )
        seen[folded] = server["id"]
    return {
        "schema_version": SCHEMA_VERSION,
        "telegram": {"token": token, "admin_ids": admin_ids},
        "servers": servers,
    }


def _read_env(path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    target = path or env_path()
    lines: list[str] = []
    values: dict[str, str] = {}
    try:
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return lines, values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        values[key.strip()] = raw.strip()
    return lines, values


def load_config(path: Path | None = None) -> dict[str, Any] | None:
    _lines, values = _read_env(path)
    if not values.get("API_TOKEN") or not values.get("ADMIN_USER_IDS"):
        return None
    try:
        admin_ids = json.loads(values["ADMIN_USER_IDS"])
    except json.JSONDecodeError:
        admin_ids = values["ADMIN_USER_IDS"]
    servers: Any = []
    if values.get("SERVERS_JSON"):
        try:
            servers = json.loads(values["SERVERS_JSON"])
        except json.JSONDecodeError as error:
            raise ValidationError(f"Existing SERVERS_JSON is invalid: {error}.") from error
    if not servers and values.get("URL") and values.get("TOKEN"):
        servers = [{
            "id": "primary", "name": "Primary", "url": values["URL"],
            "token": values["TOKEN"], "enabled": True, "weight": 1,
            "panel": "blitz", "default_inbound_ids": [], "default_limit_ip": 0,
        }]
    return normalize_config({
        "schema_version": SCHEMA_VERSION,
        "telegram": {"token": values["API_TOKEN"], "admin_ids": admin_ids},
        "servers": servers,
    })


def config_fingerprint(config: dict[str, Any]) -> str:
    canonical = json.dumps(normalize_config(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    text = str(value or "")
    return "********" if len(text) <= 8 else f"{text[:4]}...{text[-4:]}"


def public_server(server: dict[str, Any]) -> dict[str, Any]:
    return {**server, "token": mask_secret(str(server.get("token") or ""))}


def configuration_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, list[str]]:
    old_servers = {item["id"].casefold(): item for item in (old or {}).get("servers", [])}
    new_servers = {item["id"].casefold(): item for item in new.get("servers", [])}
    added = [item["id"] for key, item in new_servers.items() if key not in old_servers]
    removed = [item["id"] for key, item in old_servers.items() if key not in new_servers]
    changed = []
    for key, item in new_servers.items():
        previous = old_servers.get(key)
        if previous is not None and previous != item:
            changed.append(item["id"])
    if old and old.get("telegram") != new.get("telegram"):
        changed.insert(0, "Telegram settings")
    return {"added": added, "changed": changed, "removed": removed}


@contextmanager
def config_lock(path: Path | None = None) -> Iterator[None]:
    target = path or env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _env_updates(config: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_config(config)
    first = normalized["servers"][0]
    fingerprint = config_fingerprint(normalized)
    return {
        "API_TOKEN": normalized["telegram"]["token"],
        "ADMIN_USER_IDS": json.dumps(normalized["telegram"]["admin_ids"], separators=(",", ":")),
        "URL": first["url"],
        "TOKEN": first["token"],
        "SERVERS_JSON": json.dumps(normalized["servers"], separators=(",", ":")),
        "AJIB_CONFIG_FINGERPRINT": fingerprint,
    }


def _atomic_write_env(
    config: dict[str, Any], *, keep_previous: bool = True, path: Path | None = None
) -> str:
    target = path or env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines, _values = _read_env(target)
    updates = _env_updates(config)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}\n")
                seen.add(key)
        else:
            output.append(line if line.endswith("\n") else f"{line}\n")
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}\n")
    if keep_previous and target.exists():
        previous = previous_env_path() if path is None else target.with_name(f"{target.name}.previous")
        previous.parent.mkdir(parents=True, exist_ok=True)
        previous_descriptor, previous_temporary = tempfile.mkstemp(
            prefix=f".{previous.name}.", dir=previous.parent
        )
        try:
            with target.open("rb") as source, os.fdopen(previous_descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(previous_temporary, 0o600)
            os.replace(previous_temporary, previous)
        finally:
            try:
                os.unlink(previous_temporary)
            except FileNotFoundError:
                pass
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
            temporary.writelines(output)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return updates["AJIB_CONFIG_FINGERPRINT"]


def save_config(
    config: dict[str, Any], *, keep_previous: bool = True, path: Path | None = None,
    expected_fingerprint: str | None = None,
) -> str:
    normalized = normalize_config(config)
    with config_lock(path):
        if expected_fingerprint is not None:
            current = load_config(path)
            current_fingerprint = config_fingerprint(current) if current else None
            expected_value = expected_fingerprint or None
            if current_fingerprint != expected_value:
                raise ConfigurationConflictError(
                    "Configuration changed while it was being edited. Reload it and retry; no changes were written."
                )
        return _atomic_write_env(normalized, keep_previous=keep_previous, path=path)


def replace_servers(
    servers: list[dict[str, Any]], *, keep_previous: bool = True, path: Path | None = None
) -> str:
    """Atomically replace only server settings in the canonical configuration."""
    normalized_servers = [normalize_server(server, index) for index, server in enumerate(servers)]
    with config_lock(path):
        current = load_config(path)
        if current is None:
            raise OperatorError("Telegram bot configuration is missing.")
        current["servers"] = normalized_servers
        return _atomic_write_env(current, keep_previous=keep_previous, path=path)


def update_server(
    server_id: str, changes: dict[str, Any], *, path: Path | None = None,
    keep_previous: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """Apply a small server edit while holding the configuration lock."""
    protected = {"id", "name", "url", "token", "panel", "enabled", "weight", "default_inbound_ids", "default_limit_ip"}
    unknown = set(changes) - protected
    if unknown:
        raise ValidationError(f"Unsupported server fields: {', '.join(sorted(unknown))}.")
    if "id" in changes and str(changes["id"]).casefold() != str(server_id).casefold():
        raise ValidationError("Server IDs are immutable.")
    with config_lock(path):
        current = load_config(path)
        if current is None:
            raise OperatorError("Telegram bot configuration is missing.")
        for index, server in enumerate(current["servers"]):
            if server["id"].casefold() != str(server_id).casefold():
                continue
            candidate = {**server, **changes, "id": server["id"]}
            active = active_transfer_for_server(server["id"])
            protected_changed = any(
                key in changes and candidate.get(key) != server.get(key)
                for key in ("url", "token", "panel", "default_inbound_ids")
            )
            paused = (
                ("enabled" in changes and not candidate.get("enabled", True))
                or ("weight" in changes and float(candidate.get("weight", 1)) == 0)
            )
            if active and (protected_changed or paused):
                raise OperatorError(
                    f"Server is used by active transfer {active.get('job_id')}; this edit is blocked."
                )
            current["servers"][index] = normalize_server(candidate, index)
            fingerprint = _atomic_write_env(current, keep_previous=keep_previous, path=path)
            return current["servers"], fingerprint
        raise OperatorError(f"VPN server '{server_id}' is not configured.")


def restore_previous_config() -> bool:
    previous = previous_env_path()
    target = env_path()
    if not previous.is_file():
        return False
    with config_lock():
        current_copy = target.with_name(f"{target.name}.failed")
        if target.exists():
            shutil.copy2(target, current_copy)
            os.chmod(current_copy, 0o600)
        os.replace(previous, target)
        os.chmod(target, 0o600)
    return True


def _telegram_probe(token: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=timeout,
        )
        payload = response.json() if response.content else {}
        if response.ok and payload.get("ok") and isinstance(payload.get("result"), dict):
            result = payload["result"]
            return {
                "name": "telegram", "ok": True,
                "message": f"Telegram bot @{result.get('username') or result.get('id')} verified.",
                "latency_ms": round((time.monotonic() - started) * 1000),
                "bot_id": result.get("id"), "username": result.get("username"),
            }
        return {
            "name": "telegram", "ok": False,
            "message": f"Telegram rejected the bot token (HTTP {response.status_code}).",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except (requests.RequestException, ValueError) as error:
        return {
            "name": "telegram", "ok": False,
            "message": f"Telegram check failed: {type(error).__name__}.",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


def _load_api_client_module():
    path = str(bot_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
    with _IMPORT_LOCK:
        previous_role = os.environ.get("AJIB_BOT_ROLE")
        os.environ["AJIB_BOT_ROLE"] = "supervisor"
        try:
            from utils import api_client  # type: ignore
        finally:
            if previous_role is None:
                os.environ.pop("AJIB_BOT_ROLE", None)
            else:
                os.environ["AJIB_BOT_ROLE"] = previous_role
    return api_client


def probe_server(server: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        module = _load_api_client_module()
        client = module.create_panel_client(server)
        users = client.get_users()
        if users is None:
            return {
                "id": server["id"], "ok": False, "healthy": False,
                "creation_ready": False, "message": "Panel user list is unavailable.",
                "latency_ms": round((time.monotonic() - started) * 1000),
                "account_count": None,
            }
        readiness = getattr(client, "is_creation_ready", None)
        ready, reason = readiness(verify_remote=True) if callable(readiness) else (True, None)
        if isinstance(users, dict):
            count = len(users)
        elif isinstance(users, list):
            count = len(users)
        else:
            count = 0
        ok = bool(ready) if server.get("enabled", True) and float(server.get("weight", 1)) > 0 else True
        message = "Healthy and ready for placement." if ready else f"Healthy, but placement is not ready ({reason})."
        if not server.get("enabled", True):
            message = "Healthy; administratively disabled."
        elif float(server.get("weight", 1)) == 0:
            message = "Healthy; placement paused by weight 0."
        return {
            "id": server["id"], "ok": ok, "healthy": True,
            "creation_ready": bool(ready), "creation_error": reason,
            "message": message,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "account_count": count,
        }
    except Exception as error:
        return {
            "id": server.get("id", "unknown"), "ok": False, "healthy": False,
            "creation_ready": False,
            "message": f"Panel check failed: {type(error).__name__}.",
            "latency_ms": round((time.monotonic() - started) * 1000),
            "account_count": None,
        }


def probe_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Probe panels concurrently while preserving configuration order."""
    if len(servers) <= 1:
        return [probe_server(server) for server in servers]
    workers = min(8, len(servers))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ajib-preflight") as executor:
        return list(executor.map(probe_server, servers))


def preflight_config(config: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    normalized = normalize_config(config)
    timeout_value = timeout if timeout is not None else float(os.getenv("AJIB_PREFLIGHT_TIMEOUT", "10"))
    telegram = _telegram_probe(normalized["telegram"]["token"], timeout_value)
    servers = probe_servers(normalized["servers"])
    return {
        "ok": telegram["ok"] and all(item["ok"] for item in servers),
        "telegram": telegram,
        "servers": servers,
    }


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def service_action(action: str) -> str:
    if action not in {"start", "restart", "stop"}:
        raise OperatorError(f"Unsupported service action: {action}.")
    if os.getenv("AJIB_SKIP_SERVICE_ACTIONS") == "1":
        return "Service action skipped by AJIB_SKIP_SERVICE_ACTIONS."
    script = bot_dir() / "runbot.sh"
    try:
        result = _run(["bash", str(script), action])
    except (OSError, subprocess.CalledProcessError) as error:
        output = getattr(error, "stdout", "") or str(error)
        raise OperatorError(f"Telegram service {action} failed.\n{output}".strip()) from error
    return result.stdout.strip()


def _ready_for_fingerprint(fingerprint: str) -> bool:
    try:
        payload = json.loads(ready_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if payload.get("config_fingerprint") != fingerprint:
        return False
    try:
        pid = int(payload.get("pid"))
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_readiness(fingerprint: str, timeout: float | None = None) -> bool:
    if os.getenv("AJIB_SKIP_SERVICE_ACTIONS") == "1":
        return True
    deadline = time.monotonic() + (timeout if timeout is not None else float(os.getenv("AJIB_READY_TIMEOUT", "15")))
    while time.monotonic() < deadline:
        if _ready_for_fingerprint(fingerprint):
            return True
        time.sleep(0.25)
    return False


def refresh_ready_fingerprint(fingerprint: str) -> None:
    """Refresh an in-process readiness marker after an atomic server edit."""
    target = ready_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if int(payload.get("pid") or 0) != os.getpid():
        return
    payload["config_fingerprint"] = fingerprint
    payload["ready_at"] = time.time()
    descriptor, temporary = tempfile.mkstemp(prefix=".main.ready.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as ready_file:
            json.dump(payload, ready_file, separators=(",", ":"))
            ready_file.flush()
            os.fsync(ready_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_config(
    config: dict[str, Any], *, preflight: dict[str, Any] | None = None,
    expected_fingerprint: str | None = None,
) -> ApplyResult:
    normalized = normalize_config(config)
    target = env_path()
    had_previous = target.is_file()
    saved = False
    try:
        try:
            ready_path().unlink()
        except FileNotFoundError:
            pass
        fingerprint = save_config(
            normalized, keep_previous=True, expected_fingerprint=expected_fingerprint
        )
        saved = True
        service_action("start")
    except ConfigurationConflictError:
        raise
    except Exception as error:
        if saved and had_previous and previous_env_path().is_file():
            restore_previous_config()
            try:
                service_action("start")
            except OperatorError:
                pass
        elif saved:
            try:
                service_action("stop")
            except OperatorError:
                pass
        raise OperatorError(f"Configuration was not applied: {error}") from error
    ready = wait_for_readiness(fingerprint)
    checks_ok = True if preflight is None else bool(preflight.get("ok"))
    if ready and checks_ok:
        return ApplyResult(
            "healthy", "Configuration applied and bot readiness confirmed.",
            had_previous, fingerprint, ready=True, verified=True,
        )
    reasons = []
    if not checks_ok:
        reasons.append("one or more live checks failed")
    if not ready:
        reasons.append("bot readiness was not confirmed")
    return ApplyResult(
        "degraded",
        "Configuration saved, but " + " and ".join(reasons) + ".",
        had_previous,
        fingerprint,
        ready=ready,
        verified=checks_ok,
    )


def rollback_config(*, restart: bool = True) -> ApplyResult:
    if not restore_previous_config():
        raise OperatorError("No previous configuration is available.")
    restored = load_config()
    if restored is None:
        raise OperatorError("The restored configuration is incomplete.")
    fingerprint = config_fingerprint(restored)
    if restart:
        service_action("start")
        ready = wait_for_readiness(fingerprint)
    else:
        ready = True
    return ApplyResult(
        "healthy" if ready else "degraded",
        "Previous configuration restored." + ("" if ready else " Bot readiness was not confirmed."),
        True,
        fingerprint,
        ready=ready,
        verified=True,
    )


def _walk_server_references(value: Any, server_id: str) -> int:
    target = server_id.casefold()
    count = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"server_id", "renewal_server_id", "source_server_id", "destination_server_id"}:
                if str(item or "primary").casefold() == target:
                    count += 1
            else:
                count += _walk_server_references(item, server_id)
    elif isinstance(value, list):
        count += sum(_walk_server_references(item, server_id) for item in value)
    return count


def database_server_references(server_id: str) -> dict[str, int]:
    path = database_path()
    if not path.is_file():
        raise OperatorError(
            f"State database is missing at {path}; server-reference safety cannot be proven."
        )
    result: dict[str, int] = {}
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as error:
        raise OperatorError(f"State database could not be opened: {error}.") from error
    connection.row_factory = sqlite3.Row
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            if table == "bulk_transfer_jobs":
                # Active jobs are handled separately. Completed job history must
                # not permanently prevent removal after records are rehomed.
                continue
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            direct_columns = [column for column in columns if column in {
                "server_id", "renewal_server_id", "source_server_id", "destination_server_id"
            }]
            table_count = 0
            for column in direct_columns:
                row = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE lower(COALESCE("{column}", \'primary\'))=lower(?)',
                    (server_id,),
                ).fetchone()
                table_count += int(row[0])
            json_columns = [column for column in columns if column.endswith("_json")]
            if json_columns:
                selection = ",".join(f'"{column}"' for column in json_columns)
                for row in connection.execute(f'SELECT {selection} FROM "{table}"'):
                    for raw in row:
                        if not raw:
                            continue
                        try:
                            table_count += _walk_server_references(json.loads(raw), server_id)
                        except (TypeError, json.JSONDecodeError) as error:
                            raise OperatorError(
                                f'Malformed JSON in table "{table}" prevents a safe server-reference scan.'
                            ) from error
            if table_count:
                result[table] = table_count
    except sqlite3.Error as error:
        raise OperatorError(f"State database reference scan failed: {error}.") from error
    finally:
        connection.close()
    return result


def active_transfer_for_server(server_id: str) -> dict[str, Any] | None:
    path = database_path()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as error:
        raise OperatorError(f"State database could not be opened: {error}.") from error
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bulk_transfer_jobs'"
        ).fetchone()
        if table is None:
            return None
        placeholders = ",".join("?" for _ in ACTIVE_TRANSFER_STATUSES)
        row = connection.execute(
            f"""SELECT * FROM bulk_transfer_jobs
                WHERE status IN ({placeholders})
                AND (lower(source_server_id)=lower(?) OR lower(destination_server_id)=lower(?))
                ORDER BY created_at LIMIT 1""",
            (*sorted(ACTIVE_TRANSFER_STATUSES), server_id, server_id),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as error:
        raise OperatorError(f"Active migration check failed: {error}.") from error
    finally:
        connection.close()


def server_removal_report(server_id: str, *, live: bool = True) -> dict[str, Any]:
    config = load_config()
    if config is None:
        raise OperatorError("Telegram bot configuration is missing.")
    matches = [item for item in config["servers"] if item["id"].casefold() == server_id.casefold()]
    if not matches:
        raise OperatorError(f"VPN server '{server_id}' is not configured.")
    server = matches[0]
    probe = probe_server(server) if live else None
    references = database_server_references(server["id"])
    active = active_transfer_for_server(server["id"])
    blockers = []
    if len(config["servers"]) <= 1:
        blockers.append("This is the final configured server.")
    if active:
        blockers.append(f"Bulk transfer {active.get('job_id')} is {active.get('status')}.")
    if references:
        blockers.append(f"Local records reference this server ({sum(references.values())} references).")
    if live:
        if not probe or not probe.get("healthy"):
            blockers.append("The panel is unavailable, so an empty server cannot be proven.")
        elif int(probe.get("account_count") or 0) > 0:
            blockers.append(f"The panel still contains {probe['account_count']} account(s).")
    return {
        "server": public_server(server), "probe": probe, "references": references,
        "active_transfer": active, "blockers": blockers, "removable": not blockers,
        "config_fingerprint": config_fingerprint(config),
    }


def safety_backup() -> str:
    script = install_dir() / "core/scripts/ajib/backup.sh"
    try:
        return _run(["bash", str(script)]).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        output = getattr(error, "stdout", "") or str(error)
        raise OperatorError(f"Safety backup failed.\n{output}".strip()) from error


def remove_server(server_id: str, *, force: bool = False, backup: bool = True) -> dict[str, Any]:
    report = server_removal_report(server_id)
    config = load_config()
    assert config is not None
    if config_fingerprint(config) != report["config_fingerprint"]:
        raise ConfigurationConflictError(
            "Configuration changed during removal preflight. Reload it and retry; nothing was removed."
        )
    server = next(item for item in config["servers"] if item["id"].casefold() == server_id.casefold())
    if len(config["servers"]) <= 1:
        raise UnsafeRemovalError("The final configured server cannot be removed.")
    active = report.get("active_transfer")
    if active:
        raise UnsafeRemovalError(f"Server is used by active transfer {active.get('job_id')}.")
    if report["blockers"] and not force:
        raise UnsafeRemovalError("Removal blocked: " + " ".join(report["blockers"]))
    if force and (server.get("enabled", True) or float(server.get("weight", 1)) != 0):
        raise UnsafeRemovalError("Forced removal requires the server to be disabled with weight 0.")
    backup_path = safety_backup() if backup else ""
    config["servers"] = [item for item in config["servers"] if item["id"].casefold() != server_id.casefold()]
    fingerprint = save_config(
        config, expected_fingerprint=str(report["config_fingerprint"])
    )
    return {"removed": server["id"], "backup": backup_path, "fingerprint": fingerprint, "report": report}


def service_status() -> dict[str, Any]:
    if os.getenv("AJIB_SKIP_SERVICE_ACTIONS") == "1":
        active = True
        enabled = True
    else:
        try:
            active = _run(["systemctl", "is-active", "--quiet", "ajib-telegram-bot.service"], check=False).returncode == 0
            enabled = _run(["systemctl", "is-enabled", "--quiet", "ajib-telegram-bot.service"], check=False).returncode == 0
        except OSError as error:
            raise OperatorError(f"systemctl is unavailable: {error}.") from error
    config = load_config()
    fingerprint = config_fingerprint(config) if config else None
    ready = bool(fingerprint and _ready_for_fingerprint(fingerprint))
    return {
        "installed": install_dir().is_dir(), "configured": config is not None,
        "service_active": active, "service_enabled": enabled, "ready": ready,
        "status": "healthy" if active and ready else "degraded" if active or config else "stopped",
    }


def doctor(*, live: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    status = service_status()
    checks.extend([
        {"name": "python", "ok": sys.version_info >= (3, 10), "message": f"Python {sys.version_info.major}.{sys.version_info.minor} is supported." if sys.version_info >= (3, 10) else "Python 3.10 or newer is required."},
        {"name": "installation", "ok": status["installed"], "message": "Installation directory exists." if status["installed"] else "Installation directory is missing."},
        {"name": "configuration", "ok": status["configured"], "message": "Configuration loaded." if status["configured"] else "Configuration is missing."},
        {"name": "service", "ok": status["service_active"], "message": "Systemd service is active." if status["service_active"] else "Systemd service is inactive."},
        {"name": "readiness", "ok": status["ready"], "message": "Bot readiness is current." if status["ready"] else "Bot readiness is not confirmed."},
    ])
    target = env_path()
    if target.exists():
        try:
            safe_permissions = (target.stat().st_mode & 0o077) == 0
        except OSError:
            safe_permissions = False
        checks.append({"name": "permissions", "ok": safe_permissions, "message": ".env permissions are private." if safe_permissions else ".env must not be group/world accessible."})
    database = database_path()
    if not database.is_file():
        checks.append({"name": "database", "ok": False, "message": f"State database is missing at {database}."})
    else:
        connection = None
        try:
            connection = sqlite3.connect(database)
            row = connection.execute("PRAGMA quick_check").fetchone()
            database_ok = bool(row and row[0] == "ok")
            database_message = "SQLite quick check passed." if database_ok else f"SQLite quick check failed: {row[0] if row else 'no result'}."
        except sqlite3.Error as error:
            database_ok = False
            database_message = f"SQLite quick check failed: {error}."
        finally:
            if connection is not None:
                connection.close()
        checks.append({"name": "database", "ok": database_ok, "message": database_message})
    config = load_config()
    preflight = preflight_config(config) if live and config else None
    if preflight:
        checks.append(preflight["telegram"])
        checks.extend(preflight["servers"])
    ok = all(item.get("ok") for item in checks)
    broken = any(item["name"] in {"installation", "configuration"} and not item.get("ok") for item in checks)
    return {"ok": ok, "status": "healthy" if ok else "broken" if broken else "degraded", "checks": checks}
