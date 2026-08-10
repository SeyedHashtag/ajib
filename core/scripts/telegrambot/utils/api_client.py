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
import os
import requests
try:
    from utils.account_state import PanelState, inspect_account
except ModuleNotFoundError:  # Standalone diagnostics/tests.
    from account_state import PanelState, inspect_account
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv


TELEGRAM_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
_HTTP_POOL_CONNECTIONS = 8
_HTTP_POOL_MAXSIZE = 16
_thread_local = threading.local()


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
    return weight if weight > 0 else 1.0


def _normalise_server_config(config: dict, index: int = 0) -> dict | None:
    if not isinstance(config, dict):
        return None
    url = str(config.get("url") or config.get("URL") or "").strip()
    token = str(config.get("token") or config.get("TOKEN") or "").strip()
    if not url or not token:
        return None
    server_id = _safe_server_id(config.get("id") or config.get("name") or f"server{index + 1}")
    return {
        "id": server_id,
        "name": str(config.get("name") or server_id),
        "url": url,
        "token": token,
        "enabled": bool(config.get("enabled", True)),
        "weight": _safe_weight(config.get("weight", 1)),
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

    os.makedirs(os.path.dirname(TELEGRAM_ENV_PATH), exist_ok=True)
    existing_lines = []
    if os.path.exists(TELEGRAM_ENV_PATH):
        with open(TELEGRAM_ENV_PATH, "r") as f:
            existing_lines = f.readlines()

    updates = {
        "SERVERS_JSON": json.dumps(normalized, separators=(",", ":")),
        "URL": normalized[0]["url"],
        "TOKEN": normalized[0]["token"],
    }
    seen = set()
    new_lines = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}\n")

    with open(TELEGRAM_ENV_PATH, "w") as f:
        f.writelines(new_lines)
    os.environ["SERVERS_JSON"] = updates["SERVERS_JSON"]
    os.environ["URL"] = updates["URL"]
    os.environ["TOKEN"] = updates["TOKEN"]
    return True


class APIClient:
    """HTTP client for the ajib REST API."""

    def __init__(self, server_config: dict | None = None):
        load_dotenv(TELEGRAM_ENV_PATH)

        server_config = _normalise_server_config(server_config or {}, 0) if server_config else None
        base_url: str = server_config["url"] if server_config else os.getenv('URL', '')
        self.token: str = server_config["token"] if server_config else os.getenv('TOKEN', '')
        self.server_id: str = server_config["id"] if server_config else "primary"
        self.server_name: str = server_config["name"] if server_config else "Primary"

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

    def get_users(self):
        """Return list or dict of all users, or ``None`` on failure."""
        return self._get(self.users_endpoint)

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
            "data": data,
            "http_status": status_code,
            "error": None,
        }

    def add_user(self, username: str, traffic_limit: int, expiration_days: int, unlimited: bool = False, note: str | None = None):
        """Create a new user. Returns response data or ``None`` on failure."""
        payload = {
            "username": username,
            "traffic_limit": traffic_limit,
            "expiration_days": expiration_days,
            "unlimited": unlimited,
        }
        if note is not None:
            payload["note"] = note
        result = self._post(self.users_endpoint, payload)
        if result is not None:
            MultiServerAPI.record_created_user(self.server_id, username)
        return result

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

    def delete_user(self, username: str):
        """Delete a user. Returns response data or ``None`` on failure."""
        return self._delete(f"{self.users_endpoint}{username}")

    def get_user_uri(self, username: str):
        """Return subscription URI data dict, or ``None`` on failure."""
        return self._get(f"{self.base_url}api/v1/users/{username}/uri")

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

    def get_client(self, server_id: str | None = None) -> APIClient | None:
        if not self.servers:
            return None
        if server_id:
            for server in self.servers:
                if server["id"] == server_id:
                    return APIClient(server)
        return APIClient(self.servers[0])

    def iter_clients(self, include_disabled: bool = False):
        for server in self.servers:
            if include_disabled or server.get("enabled", True):
                yield server, APIClient(server)

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
                    "client": APIClient(server),
                    "index": len(entries),
                })
        return entries

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
        counts = {"active": 0, "hold": 0, "blocked": 0, "unknown": 0}
        if isinstance(users, dict):
            iterable = users.values()
        elif isinstance(users, list):
            iterable = users
        else:
            return counts
        for user in iterable:
            snapshot = inspect_account(user if isinstance(user, dict) else None)
            if snapshot.panel_state == PanelState.CONNECTED:
                counts["active"] += 1
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
            allocated_count = self.allocated_user_count(users) if healthy else None
            weight = _safe_weight(server.get("weight", 1))
            if healthy:
                usernames.update(self.extract_usernames(users))
            server_states.append({
                "server": server,
                "client": client,
                "index": entry["index"],
                "healthy": healthy,
                "active_count": allocated_count,
                "allocated_count": allocated_count,
                "weight": weight,
                "load_ratio": (allocated_count / weight) if healthy else None,
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
            if not state.get("healthy"):
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
                    state["load_ratio"] = allocated_count / weight
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
                    target_client = creation.get("client") or fallback_client
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
            allocated_count = self.allocated_user_count(users)
            state_counts = self.account_state_counts(users) if healthy else {
                "active": None, "hold": None, "blocked": None, "unknown": None,
            }
            weight = _safe_weight(server.get("weight", 1))
            statuses.append({
                **server,
                "index": entry["index"],
                "healthy": healthy,
                "active_count": allocated_count if healthy else None,
                "allocated_count": allocated_count if healthy else None,
                "connected_count": state_counts["active"],
                "hold_count": state_counts["hold"],
                "blocked_count": state_counts["blocked"],
                "unknown_count": state_counts["unknown"],
                "load_ratio": (allocated_count / weight) if healthy else None,
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
            client = APIClient(server)
            result = client.get_user_result(username)
            return client, result.get("data"), result

        result = {
            "status": "unavailable",
            "data": None,
            "http_status": None,
            "error": "server_not_configured",
        }
        return None, None, result

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
