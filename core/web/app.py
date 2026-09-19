"""Versioned HTTP transport. Start with uvicorn core.web.app:create_app --factory."""
from contextlib import asynccontextmanager
from typing import Annotated, Literal
import hmac
import io
import json
import logging
import secrets
import time
from datetime import datetime

from fastapi import Depends, FastAPI, Request, Response, UploadFile, File, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from .settings import Settings


class ChallengeInput(BaseModel):
    storefront: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9-]+$")


class MiniAppInput(ChallengeInput):
    init_data: str = Field(min_length=1, max_length=16384)


class ConsumeInput(BaseModel):
    challenge: str = Field(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class CheckoutInput(BaseModel):
    plan_id: str = Field(min_length=1, max_length=32)
    method: Literal["card", "crypto"]
    username: str | None = Field(default=None, max_length=128)
    server_id: str | None = Field(default=None, max_length=64)
    reserved: bool = False


class LanguageInput(BaseModel):
    language: Literal["fa", "en", "tk", "ru"]


class ReviewInput(BaseModel):
    approve: bool
    reason: str = Field(min_length=3, max_length=500)


class StorefrontInput(BaseModel):
    slug: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=80)


class WalletInput(BaseModel):
    address: str = Field(min_length=10, max_length=256)


class AttributionInput(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class RecruitmentInput(BaseModel):
    reseller_id: str = Field(pattern=r"^[0-9]+$", max_length=20)
    choice: Literal["cash", "credit"]


class IdentityResponse(BaseModel):
    user_id: str
    scope: str
    roles: list[str]
    language: Literal["fa", "en", "tk", "ru"]
    csrf_token: str
    writes_enabled: bool


class PlanResponse(BaseModel):
    id: str
    traffic_gb: int
    days: int
    price: str
    currency: str
    unlimited: bool


class DownloadResponse(BaseModel):
    platform: str
    id: str
    label: str
    url: str
    details: str


class AccountResponse(BaseModel):
    username: str
    server_id: str
    state: str
    expires_at: datetime | None
    used_bytes: int
    limit_bytes: int
    available: bool


class PaymentMethodResponse(BaseModel):
    id: Literal['crypto', 'card']
    available: bool
    reason: str | None


class RenewalChoiceResponse(BaseModel):
    plan: PlanResponse
    mode: Literal['immediate', 'reserved']
    available: bool
    reason: str | None


class ReservationResponse(BaseModel):
    payment_id: str
    status: str


class RenewalOptionsResponse(BaseModel):
    username: str
    server_id: str
    choices: list[RenewalChoiceResponse]
    reservation: ReservationResponse | None
    reason: str | None


class PaymentProgressResponse(BaseModel):
    code: Literal['preparing_payment', 'awaiting_receipt', 'awaiting_payment', 'awaiting_review',
                  'preparing_service', 'needs_attention', 'completed', 'cancelled', 'rejected',
                  'expired', 'renewal_reserved', 'renewal_activating']
    actions: list[Literal['upload_receipt', 'cancel', 'pay', 'contact_support']]
    poll: bool


class PaymentResponse(BaseModel):
    id: str
    status: str | None = None
    type: str | None = None
    plan_gb: str | int | float | None = None
    price: float | str | None = None
    original_price: float | str | None = None
    currency: str | None = None
    payment_method: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    days: int | None = None
    username: str | None = None
    server_id: str | None = None
    renewal_username: str | None = None
    renewal_status: str | None = None
    converted_amount: float | str | None = None
    converted_currency: str | None = None
    account_credit_reserved: float | str | None = None
    discount_amount: float | str | None = None
    payment_url: str | None = None
    card_number: str | None = None
    receipt_id: str | None = None
    review_in_telegram: bool | None = None
    progress: PaymentProgressResponse


def create_app(settings=None, services=None):
    from .runtime import configure
    configure()
    from utils import database, web_auth, web_store, web_release
    from utils.web_services import Services, ServiceError, payment_public
    from utils.web_orders import Orders, save_payment
    from utils.public_branding import PublicContentUnavailable, contains_private, UNAVAILABLE
    settings = settings or Settings.from_env()
    services = services or Services()
    orders = Orders(services)

    @asynccontextmanager
    async def lifespan(app):
        import os
        if os.getenv("AJIB_SQLITE_ACTIVE") != "1":
            raise RuntimeError("Run the existing state migration first; AJIB_SQLITE_ACTIVE=1 is required")
        web_store.initialize()
        yield

    app = FastAPI(title="Connection service API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/v1/openapi.json", redoc_url=None)
    app.state.services = services

    @app.exception_handler(PublicContentUnavailable)
    async def private_content(request, error):
        logging.getLogger('ajib.web').warning('public_content_blocked')
        return JSONResponse({'detail': UNAVAILABLE}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Validation input/context can contain secrets or private configured labels.
        return JSONResponse({'detail': 'Request fields are invalid'}, status_code=422)

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.exception_handler(web_auth.AuthenticationError)
    async def authentication_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=401)

    @app.middleware("http")
    async def security(request, call_next):
        started, request_id = time.monotonic(), secrets.token_hex(8)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != settings.origin:
            return JSONResponse({"detail": "Request origin rejected"}, status_code=403)
        length = request.headers.get("content-length", "0")
        try:
            if int(length) > 6 * 1024 * 1024:
                return JSONResponse({"detail": "Upload is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, status_code=400)
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger('ajib.web').exception('request_failed id=%s', request_id)
            response = JSONResponse({'detail': UNAVAILABLE}, status_code=500)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                                 "Cache-Control": "no-store", "X-Request-ID": request_id})
        logging.getLogger("ajib.web").info("request id=%s method=%s status=%s elapsed_ms=%d",
                                           request_id, request.method, response.status_code,
                                           (time.monotonic() - started) * 1000)
        return response

    def session(request: Request):
        value = web_auth.authenticate(request.cookies.get("service_session"))
        identity = services.identity(value["user_id"], value["scope"])
        if not web_release.permits(identity, web_release.policy(settings)):
            raise ServiceError("The portal is currently open to pilot users", 403)
        if request.method not in {"GET", "HEAD"}:
            token = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(value["csrf_hash"], web_store.digest(token)):
                raise ServiceError("Request verification failed; refresh and try again", 403)
        return {**value, **identity}

    def write_session(value=Depends(session)):
        if not web_release.policy(settings)['accept_writes']:
            raise ServiceError("Changes are temporarily paused", 503)
        return value

    def reseller(value=Depends(session)):
        if "reseller" not in value["roles"]:
            raise ServiceError("Reseller access required", 403)
        return value

    def admin(value=Depends(session)):
        if "admin" not in value["roles"]:
            raise ServiceError("Administrator access required", 403)
        return value

    def reviewer(value=Depends(write_session)):
        if not set(value["roles"]) & {"admin", "reviewer"}:
            raise ServiceError("Payment reviewer access required", 403)
        if int(time.time()) - value["created_at"] > 900:
            raise ServiceError("Sign in again before reviewing a payment", 403)
        return value

    def throttle(request, kind):
        remote = request.client.host if request.client else "unknown"
        if not web_store.rate_limit(f"{kind}:{web_store.digest(remote)}", maximum=60 if kind == "poll" else 10):
            raise ServiceError("Too many attempts; try again shortly", 429)

    def action_key(request):
        key = request.headers.get("idempotency-key", "")
        if not 16 <= len(key) <= 128 or not key.isascii():
            raise ServiceError("A stable Idempotency-Key is required")
        return key

    def set_session(response, credentials):
        response.set_cookie("service_session", credentials[0], max_age=86400, httponly=True,
                            secure=settings.secure_cookies, samesite="lax", path="/")
        response.delete_cookie("service_login", path="/api/v1/auth", secure=settings.secure_cookies, httponly=True, samesite="strict")

    @app.get("/api/v1/health")
    def health():
        database.get_connection().execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/v1/storefront")
    def storefront(slug: str | None = None):
        store = services.storefront(slug)
        current = web_release.policy(settings)
        return {**store, "public_portal": current['access'] == 'public',
                "writes_enabled": current['accept_writes'] and store["scope"] == "main"}

    @app.get("/api/v1/plans", response_model=list[PlanResponse])
    def plans(storefront: str | None = None):
        return services.catalog(services.storefront(storefront)["scope"])

    @app.get("/api/v1/downloads", response_model=list[DownloadResponse])
    def downloads(language: Literal["fa", "en", "tk", "ru"] = "en"):
        from utils.download_catalog import DOWNLOAD_CATALOG
        from utils.translations import get_message_text
        return [{"platform": platform, "id": app["id"],
                 "label": app.get("label") or get_message_text(language, app["label_key"]),
                 "url": app["url"], "details": get_message_text(language, app["details_key"])}
                for platform, apps in DOWNLOAD_CATALOG.items() for app in apps]

    @app.post("/api/v1/auth/challenge")
    def challenge(data: ChallengeInput, request: Request, response: Response):
        throttle(request, "login")
        store = services.storefront(data.storefront)
        if not store["bot_username"]:
            raise ServiceError("Telegram sign-in is not configured", 503)
        challenge_id, browser = web_auth.create_challenge(store["scope"])
        response.set_cookie("service_login", browser, max_age=300, httponly=True,
                            secure=settings.secure_cookies, samesite="strict", path="/api/v1/auth")
        return {"challenge": challenge_id, "telegram_url": f"https://t.me/{store['bot_username']}?start=web_{challenge_id}", "expires_in": 300}

    @app.post("/api/v1/auth/consume")
    def consume(data: ConsumeInput, request: Request, response: Response):
        throttle(request, "poll")
        credentials = web_auth.consume_challenge(data.challenge, request.cookies.get("service_login"))
        if credentials is None:
            return {"status": "waiting"}
        set_session(response, credentials)
        return {"status": "authenticated", "csrf_token": credentials[1]}

    @app.post("/api/v1/auth/telegram")
    def telegram(data: MiniAppInput, request: Request, response: Response):
        throttle(request, "login")
        scope = services.storefront(data.storefront)["scope"]
        credentials = web_auth.mini_app_session(data.init_data, services.bot_token(scope), scope)
        set_session(response, credentials)
        return {"status": "authenticated", "csrf_token": credentials[1]}

    @app.get("/api/v1/me", response_model=IdentityResponse)
    def me(request: Request, value=Depends(session)):
        return {**value, "csrf_token": web_store.digest("csrf:" + request.cookies["service_session"]),
                "writes_enabled": web_release.policy(settings)['accept_writes'] and value['scope'] == 'main'}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response, value=Depends(session)):
        web_auth.revoke(request.cookies["service_session"])
        response.delete_cookie("service_session", path="/")
        return {"status": "signed_out"}

    @app.put("/api/v1/me/language")
    def language(data: LanguageInput, value=Depends(write_session)):
        services.set_language(value["user_id"], value["scope"], data.language)
        return {"language": data.language}

    @app.get("/api/v1/accounts", response_model=list[AccountResponse])
    def accounts(value=Depends(session)):
        return services.accounts(value["user_id"], value["scope"])

    @app.get('/api/v1/accounts/{server_id}/{username}/renewal-options', response_model=RenewalOptionsResponse)
    def renewal_options(server_id: str, username: str, value=Depends(session)):
        from utils.customer_options import renewal_options as options
        return options(services, value['user_id'], value['scope'], server_id, username,
                       writes=web_release.policy(settings)['accept_writes'])

    @app.get('/api/v1/payment-methods', response_model=list[PaymentMethodResponse])
    def payment_methods(value=Depends(session)):
        from utils.customer_options import payment_methods as options
        return options(services, value['scope'], value['language'],
                       writes=web_release.policy(settings)['accept_writes'])

    @app.get("/api/v1/accounts/{server_id}/{username}/configuration")
    def configuration(server_id: str, username: str, value=Depends(session)):
        return services.configuration(value["user_id"], value["scope"], username, server_id)

    @app.get("/api/v1/accounts/{server_id}/{username}/qr")
    def qr(server_id: str, username: str, value=Depends(session)):
        import qrcode
        data = services.configuration(value["user_id"], value["scope"], username, server_id)
        uri = data.get("uri") or data.get("sub_url") or data.get("ipv4") or data.get("url")
        if not uri or len(uri) > 2000:
            raise ServiceError("QR code unavailable", 409)
        output = io.BytesIO()
        qrcode.make(uri).save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png")

    @app.get("/api/v1/payments", response_model=list[PaymentResponse], response_model_exclude_unset=True)
    def payments(value=Depends(session)):
        return [payment_public(key, row, scope=value['scope'], writes=web_release.policy(settings)['accept_writes'])
                for key, row in services.payments(value["user_id"], value["scope"]).items()]

    @app.get("/api/v1/payments/{payment_id}", response_model=PaymentResponse, response_model_exclude_unset=True)
    def payment(payment_id: str, value=Depends(session)):
        record = services.payment(value["user_id"], value["scope"], payment_id)
        result = payment_public(payment_id, record, scope=value['scope'], writes=web_release.policy(settings)['accept_writes'])
        if record.get("status") == "waiting_receipt":
            result["card_number"] = record.get("card_number")
        return result

    @app.post("/api/v1/orders", response_model=PaymentResponse, response_model_exclude_unset=True)
    def create_order(data: CheckoutInput, request: Request, value=Depends(write_session)):
        key = action_key(request)
        if not web_store.rate_limit(f"orders:{value['scope']}:{value['user_id']}", maximum=20):
            raise ServiceError("Too many checkout attempts; try again shortly", 429)
        return orders.create(value["user_id"], value["scope"], key, data.plan_id, data.method,
                             value["language"], data.username, data.server_id, data.reserved)

    @app.post("/api/v1/payments/{payment_id}/cancel", response_model=PaymentResponse, response_model_exclude_unset=True)
    def cancel(payment_id: str, value=Depends(write_session)):
        return orders.cancel(value["user_id"], value["scope"], payment_id)

    @app.post("/api/v1/payments/{payment_id}/receipt")
    async def receipt(payment_id: str, file: Annotated[UploadFile, File()], value=Depends(write_session)):
        from utils.customer_payments import submit_receipt
        contents = await file.read(5 * 1024 * 1024 + 1)
        return submit_receipt(value['user_id'], value['scope'], payment_id, contents)

    @app.get("/api/v1/receipts/{receipt_id}")
    def view_receipt(receipt_id: str, value=Depends(session)):
        row = database.get_connection().execute("SELECT * FROM web_receipts WHERE id=? AND scope=?", (receipt_id, value["scope"])).fetchone()
        if not row:
            raise ServiceError("Receipt not found", 404)
        if row["user_id"] != value["user_id"]:
            from utils.receipt_checker import can_review_receipt
            record = services.payment(value["user_id"], value["scope"], row["payment_id"], reviewer=True)
            if not can_review_receipt(int(value["user_id"]), record, is_admin_user="admin" in value["roles"]):
                raise ServiceError("Receipt not found", 404)
        return Response(row["contents"], media_type=row["content_type"], headers={"Content-Disposition": "inline; filename=receipt.jpg"})

    @app.get("/api/v1/referrals")
    def referrals(value=Depends(session)):
        from utils.web_rewards import details
        return {**services.referrals(value["user_id"], value["scope"]),
                **details(value["user_id"], value["scope"])}

    @app.get("/api/v1/trial")
    def trial(value=Depends(session)):
        from utils.web_trials import state
        return state(value["user_id"], value["scope"])

    @app.post("/api/v1/trial")
    def request_trial(request: Request, value=Depends(write_session)):
        from utils.web_trials import request as create_trial
        return create_trial(value["user_id"], value["scope"], action_key(request), value["language"])

    @app.post("/api/v1/trial/connected")
    def trial_connected(value=Depends(write_session)):
        from utils.web_trials import connected
        return connected(value["user_id"], value["scope"])

    @app.post("/api/v1/referrals/code")
    def referral_code(request: Request, value=Depends(write_session)):
        from utils.web_rewards import perform
        return perform(value["user_id"], value["scope"], "code", action_key(request), {})

    @app.put("/api/v1/referrals/wallet")
    def referral_wallet(data: WalletInput, request: Request, value=Depends(write_session)):
        from utils.web_rewards import perform
        return perform(value["user_id"], value["scope"], "wallet", action_key(request), data.model_dump())

    @app.post("/api/v1/referrals/attribution")
    def referral_attribution(data: AttributionInput, request: Request, value=Depends(write_session)):
        from utils.web_rewards import perform
        return perform(value["user_id"], value["scope"], "attribution", action_key(request), data.model_dump())

    @app.post("/api/v1/referrals/withdrawals")
    def referral_withdrawal(request: Request, value=Depends(write_session)):
        from utils.web_rewards import perform
        return perform(value["user_id"], value["scope"], "withdrawal", action_key(request), {})

    @app.post("/api/v1/referrals/recruitment")
    def recruitment(data: RecruitmentInput, request: Request, value=Depends(write_session)):
        from utils.web_rewards import perform
        return perform(value["user_id"], value["scope"], "recruitment", action_key(request), data.model_dump())

    @app.get("/api/v1/credits")
    def credits(value=Depends(session)):
        if value["scope"] != "main":
            return {"available": 0, "reserved": 0}
        from utils.account_credit import get_account_credit
        return get_account_credit(value["user_id"])

    @app.get("/api/v1/reseller")
    def reseller_summary(value=Depends(reseller)):
        return services.reseller_summary(value["user_id"])

    @app.get("/api/v1/reseller/customers")
    def reseller_customers(q: str = Query(default="", max_length=128), value=Depends(reseller)):
        return services.reseller_customers(value["user_id"], q)

    @app.put("/api/v1/reseller/storefront")
    def save_storefront(data: StorefrontInput, value=Depends(reseller), writable=Depends(write_session)):
        import sqlite3
        if contains_private([data.title, data.slug]):
            raise ServiceError('Choose a different public title and address')
        with database.transaction(operation="web_storefront_update") as connection:
            try:
                connection.execute("""INSERT INTO web_storefronts(scope,slug,title) VALUES (?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET slug=excluded.slug,title=excluded.title""",
                    ("hosted:" + value["user_id"], data.slug, data.title))
            except sqlite3.IntegrityError:
                raise ServiceError("This storefront address is already in use", 409)
            web_store.audit(connection, value["user_id"], "main", "storefront.update", data.slug)
        return {"path": "/s/" + data.slug}

    @app.get("/api/v1/admin/overview")
    def overview(value=Depends(admin)):
        return services.overview()

    @app.get("/api/v1/admin/operations")
    def operation_status(value=Depends(admin)):
        return services.operations()

    @app.get("/api/v1/admin/payments")
    def pending_reviews(value=Depends(session)):
        if not set(value["roles"]) & {"admin", "reviewer"}:
            raise ServiceError("Payment reviewer access required", 403)
        from utils.receipt_checker import can_review_receipt
        result = []
        for key, record in services.payments(value["user_id"], value["scope"], all_users=True).items():
            if record.get("status") == "pending_approval" and can_review_receipt(int(value["user_id"]), record, is_admin_user="admin" in value["roles"]):
                result.append({**payment_public(key, record, scope=value['scope']), "receipt_id": record.get("web_receipt_id"),
                               "review_in_telegram": record.get("fulfillment_owner") != "web"})
        return result

    @app.post("/api/v1/admin/payments/{payment_id}/review")
    def review(payment_id: str, data: ReviewInput, value=Depends(reviewer)):
        return orders.review(value["user_id"], value["scope"], payment_id, data.approve, data.reason)

    @app.get("/api/v1/admin/audit")
    def audit(value=Depends(admin)):
        return [dict(row) for row in database.get_connection().execute(
            "SELECT id,actor,scope,action,resource,occurred_at FROM web_audit ORDER BY id DESC LIMIT 200")]

    from .public_content import PublicContentMiddleware
    app.add_middleware(PublicContentMiddleware)
    return app
