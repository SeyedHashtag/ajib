"""Durable main-store checkout using existing incentive and renewal services.

External operations are never replayed after an uncertain result. Web orders
share the payments table, but carry an explicit owner so legacy pollers cannot
fulfil them concurrently with this worker.
"""
import json
import secrets
import os
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from . import database, web_store
from .web_services import ServiceError, payment_public
from .time_utils import format_utc_timestamp


def save_payment(connection, scope, payment_id, changes):
    row = connection.execute("SELECT payload_json FROM payments WHERE scope=? AND payment_id=?", (scope, payment_id)).fetchone()
    record = json.loads(row[0]) if row else {}
    before = record.get("status")
    record.update(changes)
    record["updated_at"] = format_utc_timestamp()
    record.setdefault("created_at", record["updated_at"])
    if record.get("status") == "completed":
        record.setdefault("completed_at", record["updated_at"])
    connection.execute("""INSERT INTO payments
        (scope,payment_id,user_id,status,kind,payment_method,amount_cents,currency,created_at,updated_at,payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,payment_id) DO UPDATE SET
        status=excluded.status,kind=excluded.kind,payment_method=excluded.payment_method,
        amount_cents=excluded.amount_cents,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
        (scope, payment_id, str(record["user_id"]), record.get("status"), record.get("type", "purchase"),
         record.get("payment_method"), int((Decimal(str(record.get("price", 0))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
         record.get("currency", "USD"), record["created_at"], record["updated_at"], json.dumps(record)))
    if record.get("status") != before:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM payment_events WHERE scope=? AND payment_id=?", (scope, payment_id)).fetchone()[0]
        event = {"status": record.get("status"), "previous_status": before, "timestamp": record["updated_at"]}
        connection.execute("INSERT INTO payment_events VALUES (?,?,?,?,?,?,?)",
                           (scope, payment_id, sequence, record.get("status"), before, record["updated_at"], json.dumps(event)))
    return record


class Orders:
    def __init__(self, services):
        self.services = services

    def create(self, user_id, scope, key, plan_id, method, language="en", username=None, server_id=None, reserved=False):
        if scope != "main":
            raise ServiceError("Hosted checkout is not enabled in this release", 409)
        if method not in {"card", "crypto"}:
            raise ServiceError("Choose an available payment method")
        payload = {"plan_id": plan_id, "method": method, "username": username,
                   "server_id": server_id, "reserved": reserved}
        request_hash = web_store.digest(json.dumps(payload, sort_keys=True))
        existing = database.get_connection().execute("SELECT * FROM web_operations WHERE scope=? AND user_id=? AND key=?", (scope, str(user_id), key)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ServiceError("This request key was already used for another checkout", 409)
            return payment_public(existing["id"], self.services.payment(user_id, scope, existing["id"]))
        plan = next((item for item in self.services.catalog(scope) if item["id"] == plan_id), None)
        if not plan:
            raise ServiceError("Plan unavailable", 404)
        from .purchase_incentives import reserve_order_checkout
        offer, renewal_metadata = None, {}
        if username:
            from .catalog_service import load_catalog
            from .renewal import find_customer_renewal_offer, customer_payment_metadata
            client, data, lookup = self.services.resolve_account(user_id, scope, username, server_id)
            offer = find_customer_renewal_offer(int(user_id), username, client, data,
                load_catalog(Path(database.bot_dir()) / "plans.json"),
                payments=self.services.payments(user_id, scope), server_id=server_id,
                allow_reservation=reserved, target_plan_gb=plan_id, lookup_result=lookup)
            if not offer.get("eligible"):
                raise ServiceError(offer.get("reason", "Renewal unavailable"), 409)
            renewal_metadata = customer_payment_metadata(offer)
        card, rate = None, None
        if method == "card":
            from .receipt_checker import get_card_number_for_receipt_type
            from .exchange_rate import get_exchange_rate
            card, rate = get_card_number_for_receipt_type("regular"), get_exchange_rate()
            if not card or not rate or language != "fa":
                raise ServiceError("Card payment is unavailable for this language", 409)
        if method == "crypto" and not self.services.bot_token(scope):
            raise ServiceError("Store authentication is not configured", 503)
        payment_id = "web_" + secrets.token_hex(16)
        now = int(time.time())
        with database.transaction(operation="web_checkout") as connection:
            existing = connection.execute("SELECT * FROM web_operations WHERE scope=? AND user_id=? AND key=?", (scope, str(user_id), key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ServiceError("Request key conflict", 409)
                return payment_public(existing["id"], self.services.payment(user_id, scope, existing["id"]))
            if username:
                pending = connection.execute("SELECT payload_json FROM web_operations WHERE scope=? AND user_id=? AND kind='renewal' AND status NOT IN ('completed','cancelled','rejected')", (scope, str(user_id)))
                if any(json.loads(row[0]).get("username") == username for row in pending):
                    raise ServiceError("Another renewal is already pending for this account", 409)
            quote = reserve_order_checkout(int(user_id), payment_id,
                offer.get("full_price", plan["price"]) if offer else plan["price"],
                payment_method=method,
                discount_cap_percent=(float(offer.get("renewal_discount_percent", 0) or 0) + (5 if method == "crypto" else 0)) if offer else None,
                renewal_discount_percent=(offer or {}).get("renewal_discount_percent", 0),
                allow_invite_discount=not username, payments=self.services.payments(user_id, scope))
            record = {**renewal_metadata, **quote, "user_id": int(user_id), "plan_gb": plan_id,
                      "days": plan["days"], "unlimited": plan["unlimited"], "language": language,
                      "payment_method": "Crypto" if method == "crypto" else "Card to Card",
                      "receipt_type": "regular", "currency": "USD", "fulfillment_owner": "web",
                      "status": "approved" if quote["price"] == 0 else "creating" if method == "crypto" else "waiting_receipt"}
            release = connection.execute('SELECT revision FROM web_release_control WHERE id=1').fetchone()
            record['web_revision'] = release['revision'] if release else ''
            if method == 'crypto' and quote['price'] > 0:
                record.update(gateway_order_id=payment_id, gateway_merchant_id=os.getenv('CRYPTO_MERCHANT_ID', ''))
            if card:
                from .receipt_checker import should_route_to_receipt_checker, get_receipt_checker_user_id
                routed = should_route_to_receipt_checker("regular")
                record.update(card_number=card, exchange_rate=rate,
                              routed_to_checker=routed, receipt_checker_user_id=get_receipt_checker_user_id() if routed else None,
                              converted_amount=float(Decimal(str(quote["price"])) * Decimal(str(rate))),
                              converted_currency="Tomans")
            record = save_payment(connection, scope, payment_id, record)
            connection.execute("INSERT INTO web_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (payment_id, scope, str(user_id), key, request_hash,
                                "renewal" if username else "purchase", record["status"], json.dumps(payload), now, now))
            web_store.audit(connection, user_id, scope, "checkout.create", payment_id, {"plan": plan_id, "method": method})
        if method == "crypto" and quote["price"] > 0:
            from .payments import CryptoPayment
            response = CryptoPayment().create_payment(quote["price"], plan_id, int(user_id),
                                                       additional_data={"web_order_id": payment_id}, order_id=payment_id)
            result = response.get("result") or {}
            with database.transaction(operation="web_gateway_created") as connection:
                latest = connection.execute('SELECT status FROM web_operations WHERE id=?', (payment_id,)).fetchone()
                if latest['status'] != 'creating':
                    return payment_public(payment_id, self.services.payment(user_id, scope, payment_id))
                if (response.get("error") or result.get('order_id') != payment_id or not result.get("uuid")
                        or not str(result.get("url", "")).startswith("https://")):
                    record = save_payment(connection, scope, payment_id, {"status": "uncertain", "web_attention_reason": "gateway_creation_uncertain"})
                else:
                    record = save_payment(connection, scope, payment_id, {"status": "pending", "gateway_payment_id": result["uuid"],
                        "gateway_order_id": result.get("order_id"), "payment_url": result["url"]})
                connection.execute("UPDATE web_operations SET status=?,updated_at=? WHERE id=?", (record["status"], int(time.time()), payment_id))
        return payment_public(payment_id, record)

    def review(self, actor, scope, payment_id, approve, reason):
        record = self.services.payment(actor, scope, payment_id, reviewer=True)
        from .receipt_checker import can_review_receipt
        is_admin = "admin" in self.services.identity(actor, scope)["roles"]
        if not can_review_receipt(int(actor), record, is_admin_user=is_admin):
            raise ServiceError("You cannot review this receipt", 403)
        if record.get("fulfillment_owner") != "web":
            raise ServiceError("Review this legacy payment in Telegram", 409)
        with database.transaction(operation="web_payment_review") as connection:
            current = self.services.payment(actor, scope, payment_id, reviewer=True)
            if current.get("status") != "pending_approval":
                raise ServiceError("Payment is no longer awaiting review", 409)
            status = "approved" if approve else "rejected"
            review_fields = {"status": status, "reviewed_by_user_id": int(actor),
                             "reviewed_by_role": "admin" if is_admin else "checker",
                             "reviewed_action": "approve" if approve else "reject",
                             "reviewed_at": format_utc_timestamp()}
            if approve and current.get("routed_to_checker"):
                from .receipt_checker import get_receipt_checker_share_percent, calculate_checker_share_amount, calculate_checker_share_amount_toman
                share = get_receipt_checker_share_percent()
                review_fields.update(checker_share_percent=share,
                    checker_accounting_amount_toman=current.get("converted_amount"),
                    checker_share_amount=calculate_checker_share_amount(current.get("price", 0), share),
                    checker_share_amount_toman=calculate_checker_share_amount_toman(current.get("converted_amount", 0), share))
            record = save_payment(connection, scope, payment_id, review_fields)
            connection.execute("UPDATE web_operations SET status=?,updated_at=? WHERE id=?", (status, int(time.time()), payment_id))
            if not approve:
                from .purchase_incentives import release_main_checkout
                release_main_checkout(record["user_id"], payment_id)
            web_store.audit(connection, actor, scope, "payment.approve" if approve else "payment.reject", payment_id, {"reason": reason})
            web_store.enqueue(connection, "review:" + payment_id, scope, record["user_id"], f"Payment {payment_id}: {status}.")
        return payment_public(payment_id, record)

    def cancel(self, actor, scope, payment_id):
        with database.transaction(operation="web_checkout_cancel") as connection:
            record = self.services.payment(actor, scope, payment_id)
            if record.get("fulfillment_owner") != "web" or record.get("status") != "waiting_receipt":
                raise ServiceError("This payment cannot be cancelled automatically", 409)
            from .purchase_incentives import release_main_checkout
            release_main_checkout(record["user_id"], payment_id)
            record = save_payment(connection, scope, payment_id, {"status": "cancelled"})
            connection.execute("UPDATE web_operations SET status='cancelled',updated_at=? WHERE id=?", (int(time.time()), payment_id))
            web_store.audit(connection, actor, scope, "checkout.cancel", payment_id)
        return payment_public(payment_id, record)

    def process_one(self):
        # Worker ownership is claimed durably before any external call.
        with database.transaction(operation="web_fulfillment_claim") as connection:
            row = connection.execute("SELECT * FROM web_operations WHERE status='approved' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            connection.execute("UPDATE web_operations SET status='processing',updated_at=? WHERE id=?", (int(time.time()), row["id"]))
            record = save_payment(connection, row["scope"], row["id"], {"status": "processing"})
        payment_id, scope = row["id"], row["scope"]
        try:
            if record.get("type") == "renewal":
                if record.get("renewal_mode") == "reserved":
                    from .renewal import mark_payment_renewal_reserved
                    ok = mark_payment_renewal_reserved(payment_id)
                    if not ok:
                        raise ServiceError("Could not persist renewal reservation")
                    fields = {"renewal_status": "reserved"}
                else:
                    from .renewal import execute_customer_renewal
                    result = execute_customer_renewal({**record, 'mutation_operation_id': 'main-payment:' + payment_id}, multi_api=self.services.panels)
                    if not result.get("success"):
                        raise ServiceError("Renewal needs reconciliation")
                    fields = {"username": record["renewal_username"], "server_id": record["renewal_server_id"]}
            else:
                client = self.services.panels.select_server_for_new_user()
                if client is None:
                    raise ServiceError("No server available")
                suffix = "".join(chr(97 + int(char, 16)) for char in payment_id[4:])
                username = f"s{record['user_id']}{suffix}"
                fields = {"username": username, "server_id": client.server_id}
                with database.transaction(operation="web_provision_intent") as connection:
                    save_payment(connection, scope, payment_id, fields)
                # One request only. A timeout may mean success: do not try another server.
                from .username_utils import build_user_note
                note = build_user_note(username=username, traffic_limit=int(record["plan_gb"]),
                    expiration_days=int(record["days"]), unlimited=record.get("unlimited", False),
                    note_text=f"sale web-order:{payment_id}")
                from .account_operations import execute
                def create_account():
                    created = client.add_user(username, int(record['plan_gb']), int(record['days']),
                                              unlimited=record.get('unlimited', False), note=note)
                    return {'success': bool(created), **fields}
                result = execute('main-payment:' + payment_id, client.server_id, username, 'create',
                                 {'plan_gb': record['plan_gb'], 'days': record['days'],
                                  'unlimited': record.get('unlimited', False), 'note': note}, create_account)
                if not result.get('success'):
                    raise ServiceError("Account creation outcome is uncertain")
            with database.transaction(operation="web_fulfillment_complete") as connection:
                completed = save_payment(connection, scope, payment_id, {**fields, "status": "completed"})
                from .purchase_incentives import finalize_main_checkout
                finalize_main_checkout(payment_id, completed)
                connection.execute("UPDATE web_operations SET status='completed',updated_at=? WHERE id=?", (int(time.time()), payment_id))
                web_store.audit(connection, "worker", scope, "checkout.fulfilled", payment_id)
                web_store.enqueue(connection, "complete:" + payment_id, scope, record["user_id"], f"Payment {payment_id} completed. Open My Configs to view your service.")
        except Exception as error:
            with database.transaction(operation="web_fulfillment_uncertain") as connection:
                save_payment(connection, scope, payment_id, {"status": "uncertain", "web_attention_reason": type(error).__name__})
                connection.execute("UPDATE web_operations SET status='uncertain',updated_at=? WHERE id=?", (int(time.time()), payment_id))
                web_store.audit(connection, "worker", scope, "checkout.uncertain", payment_id, {"error_type": type(error).__name__})
        return True
