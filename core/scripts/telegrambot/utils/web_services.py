"""Application services for web transports. Never import a Telegram handler here."""
import json
import os
import re
import time
from pathlib import Path
from decimal import Decimal, InvalidOperation
from . import database, web_store
from .account_access import username_belongs_to_user
from .catalog_service import load_catalog


class ServiceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _object(value, default=None):
    try:
        return json.loads(value) if value else (default or {})
    except (TypeError, ValueError):
        return default or {}


def payment_public(payment_id, data):
    fields = ("status", "type", "plan_gb", "price", "original_price", "currency",
              "payment_method", "created_at", "completed_at", "days", "username",
              "server_id", "renewal_username", "renewal_status", "converted_amount",
              "converted_currency", "account_credit_reserved", "discount_amount")
    result = {key: data[key] for key in fields if key in data}
    url = data.get("payment_url")
    if isinstance(url, str) and url.startswith("https://"):
        result["payment_url"] = url
    return {"id": payment_id, **result}


class Services:
    def __init__(self, panels=None):
        self._panels = panels

    @property
    def panels(self):
        if self._panels is None:
            from .api_client import MultiServerAPI
            self._panels = MultiServerAPI()
        return self._panels

    def storefront(self, slug=None, *, scope=None):
        if (not slug and not scope) or scope == "main":
            return {"scope": "main", "slug": None, "title": "ajib",
                    "bot_username": os.getenv("AJIB_WEB_BOT_USERNAME", "").lstrip("@"),
                    "support": self.support()}
        connection = database.get_connection()
        column, value = ("scope", scope) if scope else ("slug", slug)
        row = connection.execute(f"SELECT * FROM web_storefronts WHERE {column}=? AND enabled=1", (value,)).fetchone()
        if not row:
            raise ServiceError("Storefront unavailable", 404)
        reseller_id = row["scope"].removeprefix("hosted:")
        reseller = connection.execute("SELECT status FROM resellers WHERE reseller_id=?", (reseller_id,)).fetchone()
        bot = connection.execute("SELECT username,enabled,status FROM hosted_bots WHERE reseller_id=?", (reseller_id,)).fetchone()
        if not reseller or reseller["status"] != "approved" or not bot or not bot["enabled"] or bot["status"] in {"disabled", "error"}:
            raise ServiceError("Storefront unavailable", 404)
        settings = connection.execute("SELECT payload_json FROM hosted_settings WHERE reseller_id=?", (reseller_id,)).fetchone()
        data = _object(settings[0]) if settings else {}
        return {"scope": row["scope"], "slug": row["slug"], "title": row["title"],
                "bot_username": (bot["username"] or "").lstrip("@"),
                "support": data.get("support_texts") or {"en": data.get("support_text", "")}}

    def bot_token(self, scope):
        if scope == "main":
            return os.getenv("API_TOKEN", "")
        self.storefront(scope=scope)
        from .hosted_bots import get_token
        return get_token(scope.removeprefix("hosted:")) or ""

    def support(self):
        path = Path(database.bot_dir()) / "support_info.json"
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        # Only explicitly public fields are returned, never arbitrary configuration.
        return {key: value for key, value in info.items()
                if key in {"text", "username", "link", "support", "support_id", "fa", "en", "ru", "tk"}
                and isinstance(value, str)} if isinstance(info, dict) else {}

    def identity(self, user_id, scope):
        self.storefront(scope=scope)
        user_id = str(user_id)
        roles = ["customer"]
        admins = _object(os.getenv("ADMIN_USER_IDS", "[]"), [])
        if scope == "main":
            if user_id in {str(value) for value in admins}:
                roles.append("admin")
            row = database.get_connection().execute("SELECT status FROM resellers WHERE reseller_id=?", (user_id,)).fetchone()
            if row and row[0] == "approved":
                roles.append("reseller")
            if user_id == os.getenv("RECEIPT_CHECKER_USER_ID"):
                roles.append("reviewer")
        namespace = "user_languages" if scope == "main" else "hosted_languages"
        row = database.get_connection().execute("SELECT value_json FROM kv_state WHERE namespace=? AND scope=? AND state_key=?",
                                               (namespace, scope, user_id)).fetchone()
        language = _object(row[0]) if row else "en"
        return {"user_id": user_id, "scope": scope, "roles": roles,
                "language": language if language in ("en", "fa", "tk", "ru") else "en"}

    def set_language(self, user_id, scope, language):
        if language not in {"en", "fa", "tk", "ru"}:
            raise ServiceError("Unsupported language")
        from .atomic_store import locked_json
        root = Path(database.bot_dir())
        path = root / "user_languages.json" if scope == "main" else root / "hosted_bots" / scope.removeprefix("hosted:") / "languages.json"
        with locked_json(str(path), {}) as values:
            values[str(user_id)] = language

    def catalog(self, scope="main"):
        plans = load_catalog(Path(database.bot_dir()) / "plans.json")
        settings, reseller = {}, None
        if scope != "main":
            self.storefront(scope=scope)
            from .hosted_bots import get_settings
            from .reseller import get_reseller_data
            owner = scope.removeprefix("hosted:")
            settings, reseller = get_settings(owner), get_reseller_data(owner)
        result = []
        for key, plan in plans.items():
            if not isinstance(plan, dict) or plan.get("target", "both") == "reseller":
                continue
            if scope != "main" and (not settings.get("plan_selection_configured") or key not in settings.get("enabled_plan_ids", [])):
                continue
            try:
                price = Decimal(str(plan["price"]))
                days, traffic = int(plan["days"]), int(key)
                if not price.is_finite() or price <= 0 or days <= 0 or traffic <= 0:
                    continue
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if reseller:
                from .hosted_bots import calculate_quote
                from .reseller import calculate_reseller_wholesale_price
                quote = calculate_quote(calculate_reseller_wholesale_price(price, reseller),
                                        settings.get("markup_percent", 20), retail_base=price)
                price = Decimal(str(quote["retail"]))
            result.append({"id": str(key), "traffic_gb": traffic, "days": days,
                           "price": str(price.quantize(Decimal("0.01"))), "currency": "USD",
                           "unlimited": bool(plan.get("unlimited", False))})
        return sorted(result, key=lambda item: item["traffic_gb"])

    def payments(self, user_id, scope, *, all_users=False):
        params = [scope]
        query = "SELECT payment_id,payload_json FROM payments WHERE scope=?"
        if not all_users:
            query += " AND user_id=?"
            params.append(str(user_id))
        query += " ORDER BY created_at DESC LIMIT 500"
        return {row[0]: _object(row[1]) for row in database.get_connection().execute(query, params)}

    def payment(self, user_id, scope, payment_id, *, reviewer=False):
        row = database.get_connection().execute("SELECT payload_json,user_id FROM payments WHERE scope=? AND payment_id=?", (scope, payment_id)).fetchone()
        if not row or (str(row[1]) != str(user_id) and not reviewer):
            raise ServiceError("Payment not found", 404)
        return _object(row[0])

    def owned(self, user_id, scope, username, server_id):
        records = self.payments(user_id, scope)
        # Recorded ownership works across migrations and old naming conventions.
        for record in records.values():
            if record.get("status") not in {"completed", "paid", "success", "succeeded"}:
                continue
            if (record.get("username") or record.get("renewal_username")) == username and (
                    record.get("server_id") or record.get("renewal_server_id")) == server_id:
                return True
        return scope == "main" and username_belongs_to_user(username, user_id)

    def accounts(self, user_id, scope):
        from .account_state import inspect_account
        from .renewal import resolve_record_history_cycle
        entries = self.panels.get_user_snapshot_entries(include_disabled=True, cache_ttl_seconds=30)
        candidates, counts = [], {}
        for entry in entries:
            users = entry["users"]
            values = users.items() if isinstance(users, dict) else ((item.get("username"), item) for item in users or [] if isinstance(item, dict))
            for name, data in values:
                if not name:
                    continue
                counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
                if self.owned(user_id, scope, name, entry["client"].server_id):
                    candidates.append((name, data, entry["client"].server_id))
        result = []
        complete = all(entry["users"] is not None for entry in entries)
        for name, data, server_id in candidates:
            cycle = resolve_record_history_cycle(self.payments(user_id, scope), username=name,
                                                 source="customer" if scope == "main" else "hosted")
            state = inspect_account(data, cycle=cycle)
            result.append({"username": name, "server_id": server_id,
                           "state": state.state, "expires_at": state.service_deadline,
                           "used_bytes": int(data.get("download_bytes", 0) or 0) + int(data.get("upload_bytes", 0) or 0),
                           "limit_bytes": int(data.get("max_download_bytes", 0) or 0),
                           "available": complete and counts[name.casefold()] == 1})
        return result

    def resolve_account(self, user_id, scope, username, server_id):
        if not self.owned(user_id, scope, username, server_id):
            raise ServiceError("Account not found", 404)
        client, user, result = self.panels.resolve_unique_user(username, preferred_server_id=server_id)
        if result.get("status") != "found" or not result.get("uniqueness_verified", False):
            raise ServiceError("Account identity could not be verified; contact support", 409)
        return client, user, result

    def configuration(self, user_id, scope, username, server_id):
        client, _, _ = self.resolve_account(user_id, scope, username, server_id)
        uri = client.get_user_uri(username)
        if uri is None:
            raise ServiceError("Configuration temporarily unavailable", 503)
        if isinstance(uri, dict):
            return {key: value for key, value in uri.items() if key in {"uri", "url", "sub_url", "ipv4", "ipv4_url", "ipv6", "ipv6_url"} and isinstance(value, str)}
        return {"uri": str(uri)}

    def reseller_summary(self, user_id):
        from .reseller import get_reseller_data, get_reseller_credit_policy, get_reseller_level_summary
        from .reseller_wholesale_credit import get_wholesale_balance
        record = get_reseller_data(user_id) or {}
        return {"status": record.get("status"), "debt": record.get("debt", 0),
                "credit_policy": get_reseller_credit_policy(record),
                "level": get_reseller_level_summary(record), "balance": get_wholesale_balance(user_id)}

    def reseller_customers(self, user_id, query=""):
        rows = database.get_connection().execute("""SELECT username,server_id,payload_json FROM reseller_configs
            WHERE reseller_id=? AND removed=0 ORDER BY config_index DESC""", (str(user_id),))
        result = []
        for row in rows:
            data = _object(row[2])
            if query.casefold() not in (row[0] or "").casefold():
                continue
            result.append({"username": row[0], "server_id": row[1],
                           **{key: data[key] for key in ("created_at", "price", "plan_gb", "days", "customer_id") if key in data}})
        return result[:500]

    def referrals(self, user_id, scope):
        row = database.get_connection().execute("SELECT code,invited_count,total_earnings_cents,available_balance_cents,wallet FROM referral_accounts WHERE scope=? AND user_id=?", (scope, str(user_id))).fetchone()
        return dict(row) if row else {"code": None, "invited_count": 0, "total_earnings_cents": 0, "available_balance_cents": 0, "wallet": None}

    def overview(self):
        connection = database.get_connection()
        return {"resellers": connection.execute("SELECT COUNT(*) FROM resellers").fetchone()[0],
                "pending_payments": connection.execute("SELECT COUNT(*) FROM payments WHERE status IN ('pending','pending_approval','processing')").fetchone()[0],
                "pending_notifications": connection.execute("SELECT COUNT(*) FROM web_outbox WHERE status!='sent'").fetchone()[0],
                "uncertain_operations": connection.execute("SELECT (SELECT COUNT(*) FROM web_operations WHERE status='uncertain') + (SELECT COUNT(*) FROM web_trials WHERE status='uncertain')").fetchone()[0]}

    def operations(self):
        connection = database.get_connection()
        orders = [dict(row) for row in connection.execute("""SELECT id,scope,user_id,kind,status,created_at,updated_at
            FROM web_operations WHERE status NOT IN ('completed','cancelled','rejected') ORDER BY created_at LIMIT 200""")]
        trials = [dict(row) for row in connection.execute("""SELECT id,'main' AS scope,user_id,'trial' AS kind,status,created_at,updated_at
            FROM web_trials WHERE status NOT IN ('completed','cancelled') ORDER BY created_at LIMIT 200""")]
        health = connection.execute("SELECT * FROM web_worker_health WHERE role='worker'").fetchone()
        outbox = connection.execute("SELECT COUNT(*) AS pending,MIN(next_attempt_at) AS oldest_due_at,MAX(attempts) AS maximum_attempts FROM web_outbox WHERE status!='sent'").fetchone()
        return {"operations": sorted(orders + trials, key=lambda row:row['created_at']),
                "worker": dict(health) if health else None, "notifications": dict(outbox)}
