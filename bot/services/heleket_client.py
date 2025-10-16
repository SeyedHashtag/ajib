from __future__ import annotations

"""
Heleket payment client (minimal placeholder)

This module provides a small async client for integrating Heleket crypto payments
based on the public documentation (https://doc.heleket.com/). It is intentionally
minimal and uses placeholder endpoints that you should adjust to your actual
Heleket API deployment.

What it does out of the box:
- Reads API base URL, API key, and network from environment or .env
  - HELEKET_API_BASE (required)
  - HELEKET_API_KEY (optional; if your deployment needs it)
  - HELEKET_NETWORK (optional; defaults to "mainnet")
  - HELEKET_WEBHOOK_SECRET (optional; used for webhook signature verification)
- Exposes typed async methods:
  - create_invoice(...) -> returns invoice/payment payload
  - get_invoice(invoice_id) -> returns invoice/payment payload
  - verify_webhook_signature(body_bytes, signature) -> bool (HMAC-SHA256)

Disclaimer:
- Endpoints and payloads are placeholders based on common REST patterns. Adapt
  ApiEndpoints and request body keys to match your Heleket integration.
- Add idempotency keys, retries, and more detailed error mapping for production.

Example:

    from ajib.bot.services.heleket_client import HeleketClient

    async def example():
        async with HeleketClient.from_env() as pay:
            inv = await pay.create_invoice(
                amount="5.00",
                currency="USDT",
                description="AJIB VPN Plan: BASIC_30",
                metadata={"plan_id": "BASIC_30", "telegram_id": 123456789},
                callback_url="https://your-bot.example.com/heleket/webhook",
                success_url="https://t.me/your_bot",
                cancel_url="https://t.me/your_bot",
            )
            status = await pay.get_invoice(inv["id"])

"""

import hmac
import os
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Dict, Optional, TypedDict, Union

import httpx

try:
    # Optional .env reader helper from the ajib package
    from ajib.bot import read_env as _read_env  # type: ignore
except Exception:  # pragma: no cover - helper is optional
    _read_env = None  # type: ignore[assignment]


__all__ = [
    "HeleketError",
    "HeleketAuthError",
    "HeleketNotFound",
    "ApiEndpoints",
    "Invoice",
    "HeleketClient",
]


# ============
# Data Models
# ============


class Invoice(TypedDict, total=False):
    id: Union[str, int]
    status: str  # e.g., "pending", "paid", "expired", "cancelled"
    amount: Union[str, float]  # prefer string to avoid float precision
    currency: str  # e.g., "USDT", "USDC", "BTC"
    network: str  # e.g., "mainnet", "testnet", chain id/name if applicable
    address: Optional[str]  # deposit address, if provided
    payment_url: Optional[str]
    qr_code_url: Optional[str]
    description: Optional[str]
    metadata: Dict[str, Any]
    created_at: Optional[str]
    expires_at: Optional[str]
    extra: Dict[str, Any]


# =============
# Exceptions
# =============


class HeleketError(Exception):
    """Base Heleket client exception."""


class HeleketAuthError(HeleketError):
    """Authorization/Authentication error while calling Heleket API."""


class HeleketNotFound(HeleketError):
    """Resource not found error (404)."""


# ===================
# Endpoint configuration
# ===================


@dataclass(frozen=True)
class ApiEndpoints:
    """
    API endpoint paths (relative to base_url). Adjust these to match Heleket.

    Variables:
    - {invoice_id}
    """

    # Invoices
    create_invoice: str = "/api/invoices"
    get_invoice: str = "/api/invoices/{invoice_id}"


# ============
# Client
# ============


class HeleketClient:
    """
    Minimal async client for Heleket payments.

    Create it directly:

        pay = HeleketClient(base_url="https://api.heleket.example", api_key="...")

    Or from environment:

        async with HeleketClient.from_env() as pay:
            invoice = await pay.create_invoice(...)

    The client uses httpx.AsyncClient and can be used as an async context manager
    to ensure connection pool shutdown.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        network: str = "mainnet",
        timeout: float = 20.0,
        endpoints: Optional[ApiEndpoints] = None,
        client: Optional[httpx.AsyncClient] = None,
        webhook_secret: Optional[str] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.network = network or "mainnet"
        self.timeout = timeout
        self.endpoints = endpoints or ApiEndpoints()
        self._client = client
        self.webhook_secret = webhook_secret

    # ------------
    # Constructors
    # ------------

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "HeleketClient":
        """
        Create a client using HELEKET_API_BASE/HELEKET_API_KEY/HELEKET_NETWORK from environment or .env.
        Precedence:
          - Provided env (highest)
          - .env via helper (if available)
          - os.environ
        """
        merged: Dict[str, str] = {}
        # Load from .env via helper if possible
        if _read_env is not None:
            try:
                merged.update(_read_env())
            except Exception:
                pass
        # Overlay OS env
        merged.update(os.environ)
        # Overlay explicit env
        if env:
            merged.update(env)

        base = (merged.get("HELEKET_API_BASE") or "").strip()
        key = (merged.get("HELEKET_API_KEY") or "").strip() or None
        net = (merged.get("HELEKET_NETWORK") or "mainnet").strip() or "mainnet"
        whs = (merged.get("HELEKET_WEBHOOK_SECRET") or "").strip() or None

        if not base:
            raise ValueError("HELEKET_API_BASE is not set in environment/.env")
        return cls(base_url=base, api_key=key, network=net, webhook_secret=whs)

    # ------------
    # Context manager
    # ------------

    async def __aenter__(self) -> "HeleketClient":
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

    async def create_invoice(
        self,
        *,
        amount: Union[str, float],
        currency: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        callback_url: Optional[str] = None,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        network: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Invoice:
        """
        Create a new Heleket invoice.

        Arguments:
          - amount: Use string (e.g., "5.00") to avoid float precision issues.
          - currency: e.g., "USDT", "USDC", "BTC"
          - description: Optional human-readable description
          - metadata: Arbitrary object echoed back (e.g., plan_id, telegram_id)
          - callback_url: Webhook URL for payment notifications
          - success_url: Redirect after successful payment
          - cancel_url: Redirect if user cancels
          - network: Override default network
          - extra: Additional keys to include based on your backend expectations

        Returns:
          Invoice dict with keys like id, status, amount, currency, payment_url, etc.
        """
        body: Dict[str, Any] = {
            "amount": str(amount) if not isinstance(amount, str) else amount,
            "currency": currency,
            "network": (network or self.network),
        }
        if description:
            body["description"] = description
        if metadata:
            body["metadata"] = metadata
        if callback_url:
            body["callback_url"] = callback_url
        if success_url:
            body["success_url"] = success_url
        if cancel_url:
            body["cancel_url"] = cancel_url
        if extra:
            body.update(extra)

        data = await self._post(self.endpoints.create_invoice, json=body)
        return data  # type: ignore[return-value]

    async def get_invoice(self, invoice_id: Union[str, int]) -> Invoice:
        """
        Fetch invoice/payment status by ID.
        """
        path = self.endpoints.get_invoice.format(invoice_id=invoice_id)
        data = await self._get(path)
        return data  # type: ignore[return-value]

    # ------------
    # Webhook verification
    # ------------

    def verify_webhook_signature(
        self,
        body: Union[str, bytes],
        signature: str,
        *,
        secret: Optional[str] = None,
        algo: str = "sha256",
    ) -> bool:
        """
        Verify Heleket webhook signature using HMAC.

        - body: raw request body (bytes preferred; str will be encoded as UTF-8)
        - signature: value from the provider's signature header
        - secret: override HELEKET_WEBHOOK_SECRET
        - algo: digest algorithm ("sha256")

        Returns:
          True if signature is valid; False otherwise.

        Notes:
          - Confirm header name and signature format with your Heleket integration.
          - Some providers prefix signatures (e.g., "sha256=<hex>"). Strip if needed.
        """
        key = (secret or self.webhook_secret or "").encode("utf-8")
        if not key:
            # If no secret is configured, we cannot verify
            return False

        body_bytes = (
            body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        )

        # Normalize signature: allow formats like "sha256=<hex>" or raw hex
        sig = signature.strip()
        prefix = f"{algo}="
        if sig.lower().startswith(prefix):
            sig = sig[len(prefix) :]

        try:
            digest = hmac.new(key, body_bytes, sha256).hexdigest()
            # Use compare_digest for timing-attack-resistant comparison
            return hmac.compare_digest(digest, sig)
        except Exception:
            return False

    # ------------
    # Internal helpers
    # ------------

    def _default_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "AJIB-HeleketClient/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Allow use without context manager (less recommended)
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
        if r.status_code in (401, 403):
            raise HeleketAuthError(f"Unauthorized (status {r.status_code})")
        if r.status_code == 404:
            raise HeleketNotFound("Resource not found")
        if r.status_code >= 400:
            # Attempt to extract error detail
            try:
                err = r.json()
            except Exception:
                err = r.text
            raise HeleketError(f"HTTP {r.status_code}: {err}")

        if r.status_code == 204:
            return {}

        try:
            return r.json()
        except Exception:
            return r.text
