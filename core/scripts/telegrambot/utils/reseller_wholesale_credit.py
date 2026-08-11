"""Auditable prepaid wholesale balance for reseller fulfillment."""

from utils import database
from utils import account_credit as _account_credit

consume_account_credit = getattr(_account_credit, "consume_account_credit", lambda *_a, **_k: 0.0)
credit_account = getattr(_account_credit, "credit_account", None)
get_account_credit = getattr(
    _account_credit,
    "get_account_credit",
    lambda _user_id: {"available": 0.0, "reserved": 0.0},
)
release_account_credit = getattr(_account_credit, "release_account_credit", lambda *_a, **_k: False)
reserve_account_credit = getattr(_account_credit, "reserve_account_credit", lambda *_a, **_k: 0.0)
transfer_account_credit = getattr(_account_credit, "transfer_account_credit", None)


def wholesale_account_key(reseller_id):
    return f"reseller-wholesale:{str(reseller_id).strip()}"


def get_wholesale_balance(reseller_id):
    account = get_account_credit(wholesale_account_key(reseller_id))
    return {
        "reseller_id": str(reseller_id),
        "available": float(account.get("available", 0.0)),
        "reserved": float(account.get("reserved", 0.0)),
    }


def credit_wholesale_balance(reseller_id, amount, transaction_id, *, source=None, metadata=None):
    if credit_account is None:
        raise RuntimeError("Wholesale crediting is unavailable during this rolling update")
    return credit_account(
        wholesale_account_key(reseller_id),
        amount,
        transaction_id,
        source=source or "wholesale_topup",
        metadata={"reseller_id": str(reseller_id), **dict(metadata or {})},
    )


def transfer_purchase_credit_to_wholesale(reseller_id, amount, transaction_id):
    if transfer_account_credit is None:
        raise RuntimeError("Account-credit transfers are unavailable during this rolling update")
    return transfer_account_credit(
        reseller_id,
        wholesale_account_key(reseller_id),
        amount,
        transaction_id,
        source="purchase_credit_transfer",
        metadata={"reseller_id": str(reseller_id)},
    )


def reserve_wholesale_balance(reseller_id, reservation_id, amount, *, metadata=None):
    return reserve_account_credit(
        wholesale_account_key(reseller_id),
        reservation_id,
        amount,
        order_id=reservation_id,
        metadata={"reseller_id": str(reseller_id), **dict(metadata or {})},
    )


def consume_wholesale_balance(reseller_id, reservation_id, *, metadata=None):
    consumed = consume_account_credit(
        wholesale_account_key(reseller_id),
        reservation_id,
        order_id=reservation_id,
        metadata={"reseller_id": str(reseller_id), **dict(metadata or {})},
    )
    if consumed > 0:
        try:
            from utils.reseller import record_reseller_credit_outcome

            record_reseller_credit_outcome(
                reseller_id,
                "good",
                "prepaid_wholesale_order",
                reference_id=f"prepaid:{reservation_id}",
            )
        except Exception:
            pass
    return consumed


def release_wholesale_balance(reseller_id, reservation_id):
    return release_account_credit(wholesale_account_key(reseller_id), reservation_id)


def finalize_prepaid_config(reseller_id, reservation_id, amount, config_data):
    from utils.reseller import record_funded_reseller_config

    with database.write_transaction(operation="reseller_prepaid_config_finalize"):
        if not record_funded_reseller_config(reseller_id, amount, config_data):
            raise RuntimeError("Prepaid reseller config could not be recorded")
        consumed = consume_wholesale_balance(
            reseller_id,
            reservation_id,
            metadata={"kind": "config", "username": config_data.get("username")},
        )
        if round(float(consumed or 0), 2) != round(float(amount or 0), 2):
            raise RuntimeError("Prepaid wholesale reservation could not be consumed")
    return True


def finalize_prepaid_renewal(reseller_id, reservation_id, username, amount, renewal_data, server_id=None):
    from utils.reseller import record_funded_reseller_renewal

    with database.write_transaction(operation="reseller_prepaid_renewal_finalize"):
        if not record_funded_reseller_renewal(
            reseller_id,
            username,
            amount,
            renewal_data,
            server_id=server_id,
        ):
            raise RuntimeError("Prepaid reseller renewal could not be recorded")
        consumed = consume_wholesale_balance(
            reseller_id,
            reservation_id,
            metadata={"kind": "renewal", "username": username},
        )
        if round(float(consumed or 0), 2) != round(float(amount or 0), 2):
            raise RuntimeError("Prepaid wholesale reservation could not be consumed")
    return True


def finalize_prepaid_reserved_renewal(
    reseller_id,
    wholesale_reservation_id,
    username,
    amount,
    renewal_data,
    server_id=None,
):
    from utils.reseller import reserve_reseller_renewal

    with database.write_transaction(operation="reseller_prepaid_reserved_renewal_finalize"):
        reserved, detail = reserve_reseller_renewal(
            reseller_id,
            username,
            amount,
            renewal_data,
            server_id=server_id,
            funded=True,
            enforce_credit=False,
        )
        if not reserved:
            raise RuntimeError((detail or {}).get("reason", "Prepaid renewal could not be reserved"))
        consumed = consume_wholesale_balance(
            reseller_id,
            wholesale_reservation_id,
            metadata={"kind": "reserved_renewal", "username": username},
        )
        if round(float(consumed or 0), 2) != round(float(amount or 0), 2):
            raise RuntimeError("Prepaid wholesale reservation could not be consumed")
    return True, detail
