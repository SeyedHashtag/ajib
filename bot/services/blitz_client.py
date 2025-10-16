from __future__ import annotations

"""
Blitz API client (minimal placeholder)

This module provides a small, dependency-light async client for a Blitz-compatible
backend API. The goal is to keep the surface minimal while being easy to extend.

What it does out of the box:
- Reads base URL and API key from env (BLITZ_API_BASE, BLITZ_API_KEY)
- Exposes typed async methods to:
  - list plans
  - fetch a Telegram user's active configs
  - request a 1GB test config for a Telegram user
  - create an order/purchase (skeleton payload)
- Offers configurable endpoints so you can adapt to your Blitz deployment

Notes and assumptions:
- Endpoints here are placeholders based on common REST patterns. Adjust `ApiEndpoints`
  to match your backend routes.
- Responses are returned as Dict/TypedDict with minimal shape validation. Refine these
  as you integrate with your actual backend schema.
- For production usage, add retries, circuit-breaking, and more detailed error mapping.

Example:

    from ajib.bot.services.blitz_client import BlitzClient

    async def example(telegram_id: int):
        async with BlitzClient.from_env() as blitz:
            plans = await blitz.list_plans()
            configs = await blitz.get_user_configs(telegram_id=telegram_id)
            test = await blitz.request_test_config(telegram_id=telegram_id)
            order = await blitz.create_order(
                telegram_id=telegram_id, plan_id="BASIC_30", quantity=1
            )
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict, Union

import httpx

try:
    # read_env reads /opt/ajib/.env (if present) and returns a dict of key/values
    # It is optional; if unavailable for any reason, we fall back to os.environ.
    from ajib.bot import read_env as _read_env  # type: ignore
except Exception:  # pragma: no cover - optional convenience only
    _read_env = None  # type: ignore[assignment]


__all__ = [
    "BlitzError",
    "BlitzAuthError",
    "BlitzNotFound",
    "ApiEndpoints",
    "ConfigItem",
    "PlanItem",
    "OrderResult",
    "BlitzClient",
]


# ============
# Data Models
# ============


class ConfigItem(TypedDict, total=False):
    id: Union[str, int]
    label: str
    created_at: str
    expires_at: Optional[str]
    bytes_total: Optional[int]
    bytes_used: Optional[int]
    payload: Dict[str, Any]  # e.g., JSON payload holding config contents


class PlanItem(TypedDict, total=False):
    id: Union[str, int]
    name: str
    price: float
    duration_days: int
    data_gb: int
    backend_plan_id: Optional[str]


class OrderResult(TypedDict, total=False):
    order_id: Union[str, int]
    status: str  # e.g., "created", "pending_payment", "paid"
    amount: float
    currency: str
    payment_link: Optional[str]
    extra: Dict[str, Any]


# =============
# Exceptions
# =============


class BlitzError(Exception):
    """Base Blitz client exception."""


class BlitzAuthError(BlitzError):
    """Authorization/Authentication error while calling Blitz API."""


class BlitzNotFound(BlitzError):
    """Resource not found error (404)."""


# ===================
# Endpoint configuration
# ===================


@dataclass(frozen=True)
class ApiEndpoints:
    """
    API endpoint paths (relative to base_url). Adjust these to match your backend.

    Variables:
    - {telegram_id}: The Telegram numeric user ID
    """

    # Plans catalog
    plans_list: str = "/api/plans"

    # User configs
    user_configs: str = "/api/users/by-telegram/{telegram_id}/configs"

    # 1GB test config
    test_config: str = "/api/users/by-telegram/{telegram_id}/test-config"

    # Orders
    create_order: str = "/api/orders"


# ============
# Client
# ============


class BlitzClient:
    """
    Minimal async client for Blitz-compatible API.

    Create it directly:

        blitz = BlitzClient(base_url="https://backend", api_key="...")

    Or from environment:

        async with BlitzClient.from_env() as blitz:
            plans = await blitz.list_plans()

    The client uses an internal httpx.AsyncClient (shared per instance). It can be
    used as an async context manager to ensure clean shutdown of the connection pool.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: float = 20.0,
        endpoints: Optional[ApiEndpoints] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.endpoints = endpoints or ApiEndpoints()
        self._client = client

    # ------------
    # Constructors
    # ------------

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "BlitzClient":
        """
        Create a client using BLITZ_API_BASE and BLITZ_API_KEY from environment or .env.
        Environment precedence:
          - Provided env (if given)
          - ajib.bot.read_env() (if available)
          - os.environ
        """
        merged: Dict[str, str] = {}
        # Load from .env via helper if possible
        if _read_env is not None:
            try:
                merged.update(_read_env())
            except Exception:
                pass
        # Overlay OS env (highest priority)
        merged.update(os.environ)
        # Overlay explicit env dict (highest of all if provided)
        if env:
            merged.update(env)

        base = (merged.get("BLITZ_API_BASE") or "").strip()
        key = (merged.get("BLITZ_API_KEY") or "").strip() or None
        if not base:
            raise ValueError("BLITZ_API_BASE is not set in environment/.env")
        return cls(base_url=base, api_key=key)

    @classmethod
    def from_config(cls, cfg: Any) -> "BlitzClient":
        """
        Create from a config-like object with attributes:
          - blitz_api_base
          - blitz_api_key
        """
        base = getattr(cfg, "blitz_api_base", None)
        key = getattr(cfg, "blitz_api_key", None)
        if not base:
            raise ValueError("cfg.blitz_api_base is missing")
        return cls(base_url=str(base), api_key=(str(key) if key else None))

    # ------------
    # Context manager
    # ------------

    async def __aenter__(self) -> "BlitzClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._default_headers(),
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------
    # Public API
    # ------------

    async def list_plans(self) -> List[PlanItem]:
        """
        GET /plans
        Returns a list of available plans.
        """
        data = await self._get(self.endpoints.plans_list)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        # Some backends may wrap data under a key
        return data.get("plans", [])  # type: ignore[return-value]

    async def get_user_configs(self, telegram_id: int) -> List[ConfigItem]:
        """
        GET /users/by-telegram/{telegram_id}/configs
        Returns a list of active configs for the given Telegram user.
        """
        path = self.endpoints.user_configs.format(telegram_id=telegram_id)
        data = await self._get(path)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        return data.get("configs", [])  # type: ignore[return-value]

    async def request_test_config(self, telegram_id: int) -> ConfigItem:
        """
        POST /users/by-telegram/{telegram_id}/test-config
        Requests a 1GB test config for the Telegram user.
        """
        path = self.endpoints.test_config.format(telegram_id=telegram_id)
        data = await self._post(path, json={})
        # Assume the created config is returned
        return data  # type: ignore[return-value]

    async def create_order(
        self,
        telegram_id: int,
        plan_id: Union[str, int],
        *,
        quantity: int = 1,
        promo_code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderResult:
        """
        POST /orders
        Creates an order for the given Telegram user and plan.

        The payload here is a placeholder; adjust to your backend's expectations.
        """
        payload: Dict[str, Any] = {
            "telegram_id": telegram_id,
            "plan_id": plan_id,
            "quantity": quantity,
        }
        if promo_code:
            payload["promo_code"] = promo_code
        if metadata:
            payload["metadata"] = metadata

        data = await self._post(self.endpoints.create_order, json=payload)
        return data  # type: ignore[return-value]

    # ------------
    # Internal helpers
    # ------------

    def _default_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "AJIB-BlitzClient/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Allow using the client without context manager (less recommended)
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._default_headers(),
            )
        return self._client

    async def _get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        client = self._ensure_client()
        r = await client.get(path, params=params)
        return await self._handle_response(r)

    async def _post(
        self,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        client = self._ensure_client()
        r = await client.post(path, json=json, data=data)
        return await self._handle_response(r)

    async def _handle_response(self, r: httpx.Response) -> Any:
        if r.status_code == 401 or r.status_code == 403:
            raise BlitzAuthError(f"Unauthorized (status {r.status_code})")
        if r.status_code == 404:
            raise BlitzNotFound("Resource not found")
        if r.status_code >= 400:
            # Attempt to extract error detail
            try:
                err = r.json()
            except Exception:
                err = r.text
            raise BlitzError(f"HTTP {r.status_code}: {err}")

        if r.status_code == 204:
            return {}

        # Try JSON; if fails, return text
        try:
            return r.json()
        except Exception:
            return r.text
