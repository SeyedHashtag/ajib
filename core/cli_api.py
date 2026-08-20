import os
import subprocess
import logging
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from datetime import datetime, timedelta, timezone
import json
import sys

import psutil

SCRIPT_DIR = '/etc/ajib/core/scripts'


class Command(Enum):
    '''Contains path to command's script'''
    BACKUP_AJIB = os.path.join(SCRIPT_DIR, 'ajib', 'backup.sh')
    RESTORE_AJIB = os.path.join(SCRIPT_DIR, 'ajib', 'restore.sh')
    INSTALL_TELEGRAMBOT = os.path.join(SCRIPT_DIR, 'telegrambot', 'runbot.sh')
    SERVICES_STATUS = os.path.join(SCRIPT_DIR, 'services_status.sh')
    VERSION = os.path.join(SCRIPT_DIR, 'ajib', 'version.py')

TELEGRAM_UTILS_PATH = '/etc/ajib/core/scripts/telegrambot'
PAID_STATUSES = {'completed', 'paid', 'success', 'succeeded'}
FAILED_STATUSES = {'rejected', 'failed', 'canceled', 'cancelled', 'error'}
EXPIRED_STATUSES = {'expired'}
PENDING_STATUSES = {'pending', 'pending_approval', 'processing', 'waiting', 'unpaid'}
SERVER_INFO_SECTIONS = {'overview', 'business', 'customers', 'tech', 'traffic', 'alerts', 'full'}

# region Custom Exceptions


class ajibError(Exception):
    '''Base class for ajib-related exceptions.'''
    pass


class CommandExecutionError(ajibError):
    '''Raised when a command execution fails.'''
    pass


class InvalidInputError(ajibError):
    '''Raised when the provided input is invalid.'''
    pass


def run_cmd(command: list[str]) -> str | None:
    '''
    Runs a command and returns the output.
    Raises CommandExecutionError when the command fails.
    '''
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            shell=False,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        output = e.stdout or ''
        raise CommandExecutionError(f"Command failed: {' '.join(command)}\n{output}".strip()) from e

# endregion

# region APIs

# region ajib


def backup_ajib():
    '''Back up Telegram bot state.'''
    return run_cmd(['bash', Command.BACKUP_AJIB.value])


def restore_ajib(backup_file_path: str):
    '''Restore Telegram bot state from a backup file.'''
    return run_cmd(['bash', Command.RESTORE_AJIB.value, backup_file_path])


def _ensure_telegram_utils_path():
    if TELEGRAM_UTILS_PATH not in sys.path:
        sys.path.append(TELEGRAM_UTILS_PATH)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _safe_weight(value) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(weight) or weight < 0:
        return 1.0
    return 0.0 if weight == 0 else weight


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raw = str(value).strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _empty_order_bucket():
    return {'revenue': 0.0, 'orders': 0, 'paid': 0, 'failed': 0, 'expired': 0, 'pending': 0}


def _bump_order_bucket(bucket: dict, status: str, price: float):
    bucket['orders'] += 1
    if status in PAID_STATUSES:
        bucket['paid'] += 1
        bucket['revenue'] += price
    elif status in FAILED_STATUSES:
        bucket['failed'] += 1
    elif status in EXPIRED_STATUSES:
        bucket['expired'] += 1
    elif status in PENDING_STATUSES:
        bucket['pending'] += 1


def _iter_named_user_records(users):
    if isinstance(users, dict):
        for username, data in users.items():
            if isinstance(data, dict):
                yield str(data.get("username") or username), data
    elif isinstance(users, list):
        for data in users:
            if isinstance(data, dict) and data.get("username"):
                yield str(data.get("username")), data


def _format_bytes(value) -> str:
    amount = float(value or 0)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)}B"
    return f"{amount:.2f}{unit}"


def _format_toman_amount(value) -> str:
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0')
    return f"{amount:,}"


def _format_usd_amount(value) -> str:
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0.00')
    return f"{amount:.2f}"


def _format_share_percent(value) -> str:
    percent = _safe_float(value)
    return str(int(percent)) if percent.is_integer() else f"{percent:.2f}"


def build_online_users_from_userlist(vpn: dict) -> dict:
    enabled_servers = [server for server in vpn.get("servers", []) if server.get("enabled", True)]
    if not enabled_servers:
        return {"count": None, "status": "unavailable", "error": "No enabled VPN server configured."}

    healthy_servers = [server for server in enabled_servers if server.get("healthy")]
    if not healthy_servers:
        return {"count": None, "status": "error", "error": "No enabled VPN server userlist available."}

    count = sum(_safe_int(server.get("online_count", 0)) for server in healthy_servers)
    return {"count": count, "status": "ok", "error": None}


def _collect_payment_stats(payments: dict, now: datetime, timestamp_resolver) -> dict:
    last_30_days_start = now - timedelta(days=30)
    seven_day_start = now.date() - timedelta(days=6)

    buckets = {
        'all': _empty_order_bucket(),
        'month': _empty_order_bucket(),
        'today': _empty_order_bucket(),
        'last30': _empty_order_bucket(),
    }
    daily_sales = []
    daily_sales_by_date = {}
    for offset in range(7):
        date_key = now.date() - timedelta(days=offset)
        entry = {"date": date_key.isoformat(), "label": date_key.strftime("%b %d"), "revenue": 0.0, "paid": 0}
        daily_sales.append(entry)
        daily_sales_by_date[date_key] = entry

    plan_revenue = {}
    plan_count = {}

    future_timestamp_payments = []
    for payment_id, payment in payments.items():
        if not isinstance(payment, dict):
            continue
        status = str(payment.get('status', '')).lower()
        price = _safe_float(payment.get('price', 0))
        payment_dt = timestamp_resolver(payment)
        if payment_dt is None:
            continue
        not_future = payment_dt <= now
        if payment_dt > now + timedelta(minutes=5):
            anomaly = {
                "payment_id": str(payment_id),
                "timestamp": payment_dt.isoformat().replace("+00:00", "Z"),
            }
            future_timestamp_payments.append(anomaly)
            logging.getLogger("ajib.reporting").warning(
                "future_payment_timestamp payment_id=%s timestamp=%s now=%s",
                anomaly["payment_id"],
                anomaly["timestamp"],
                now.isoformat().replace("+00:00", "Z"),
                extra={
                    "event": "future_payment_timestamp",
                    "payment_id": anomaly["payment_id"],
                    "payment_timestamp": anomaly["timestamp"],
                },
            )
        in_month = not_future and (payment_dt.year, payment_dt.month) == (now.year, now.month)
        in_today = not_future and payment_dt.date() == now.date()
        in_last30 = not_future and payment_dt >= last_30_days_start

        _bump_order_bucket(buckets['all'], status, price)
        if in_month:
            _bump_order_bucket(buckets['month'], status, price)
        if in_today:
            _bump_order_bucket(buckets['today'], status, price)
        if in_last30:
            _bump_order_bucket(buckets['last30'], status, price)

        if status in PAID_STATUSES:
            if not_future and seven_day_start <= payment_dt.date() <= now.date():
                daily_sales_by_date[payment_dt.date()]["revenue"] += price
                daily_sales_by_date[payment_dt.date()]["paid"] += 1
            plan = str(payment.get('plan_gb') or 'Unknown')
            plan_revenue[plan] = plan_revenue.get(plan, 0.0) + price
            plan_count[plan] = plan_count.get(plan, 0) + 1

    def aov(bucket: str) -> float:
        paid = buckets[bucket]['paid']
        return buckets[bucket]['revenue'] / paid if paid else 0.0

    return {
        "buckets": buckets,
        "aov": {"all": aov('all'), "last30": aov('last30')},
        "daily_sales": daily_sales,
        "top_plans_revenue": sorted(plan_revenue.items(), key=lambda item: item[1], reverse=True)[:3],
        "top_plans_orders": sorted(plan_count.items(), key=lambda item: item[1], reverse=True)[:3],
        "future_timestamp_payments": future_timestamp_payments,
    }


def _empty_sold_traffic_bucket():
    return {"used_bytes": 0, "sold_bytes": 0, "matched_configs": 0, "sold_configs": 0}


def _sold_traffic_snapshot():
    return {
        "direct": _empty_sold_traffic_bucket(),
        "reseller": _empty_sold_traffic_bucket(),
        "unattributed": _empty_sold_traffic_bucket(),
        "total": {"used_bytes": 0, "sold_bytes": 0, "usage_percent": None},
        "missing_configs": 0,
        "ambiguous_configs": 0,
        "skipped_no_username": 0,
        "unavailable_servers": 0,
        "partial": False,
    }


def _collect_vpn_and_live_users(api_client_module=None) -> tuple[dict, dict]:
    vpn = {
        "configured": 0,
        "enabled": 0,
        "disabled": 0,
        "healthy": 0,
        "unhealthy": 0,
        "active_configs": 0,
        "started_configs": 0,
        "online_configs": 0,
        "offline_configs": 0,
        "hold_configs": 0,
        "blocked_expired_configs": 0,
        "unknown_configs": 0,
        "allocated_configs": 0,
        "servers": [],
        "error": None,
    }
    live_users = {"by_server": {}, "by_username": {}, "unavailable_servers": set()}
    try:
        if api_client_module is None:
            _ensure_telegram_utils_path()
            from utils import api_client as api_client_module
        multi_api = api_client_module.MultiServerAPI()
        for index, (server, client) in enumerate(multi_api.iter_clients(include_disabled=True)):
            server_id = str(server.get("id") or getattr(client, "server_id", None) or f"server{index + 1}")
            users = client.get_users()
            healthy = users is not None
            allocated_count = multi_api.active_user_count(users) if healthy else None
            if healthy and callable(getattr(multi_api, "account_state_counts", None)):
                state_counts = multi_api.account_state_counts(users)
                active_count = state_counts["active"]
                started_count = state_counts.get("started", active_count)
                online_count = state_counts.get("online", 0)
                offline_count = state_counts.get("offline", max(0, started_count - online_count))
                hold_count = state_counts["hold"]
                blocked_count = state_counts["blocked"]
                unknown_count = state_counts["unknown"]
            elif healthy:
                records = list(_iter_named_user_records(users))
                blocked_count = sum(1 for _username, record in records if bool(record.get("blocked", False)))
                hold_count = sum(
                    1 for _username, record in records
                    if not bool(record.get("blocked", False))
                    and " ".join(str(record.get("status") or "").replace("-", " ").replace("_", " ").lower().split()) == "on hold"
                    and not record.get("account_creation_date")
                )
                online_count = sum(
                    1 for _username, record in records
                    if not bool(record.get("blocked", False))
                    and str(record.get("status") or "").strip().casefold() == "online"
                    and bool(record.get("account_creation_date"))
                )
                offline_count = sum(
                    1 for _username, record in records
                    if not bool(record.get("blocked", False))
                    and str(record.get("status") or "").strip().casefold() == "offline"
                    and bool(record.get("account_creation_date"))
                )
                started_count = online_count + offline_count
                active_count = started_count
                unknown_count = max(0, len(records) - blocked_count - hold_count - started_count)
            else:
                active_count = started_count = online_count = offline_count = None
                hold_count = blocked_count = unknown_count = None
            weight = _safe_weight(server.get("weight", 1))
            enabled = bool(server.get("enabled", True))

            vpn["configured"] += 1
            vpn["enabled" if enabled else "disabled"] += 1
            vpn["healthy" if healthy else "unhealthy"] += 1
            if active_count is not None:
                vpn["active_configs"] += active_count
                vpn["started_configs"] += started_count
                vpn["online_configs"] += online_count
                vpn["offline_configs"] += offline_count
                vpn["hold_configs"] += hold_count
                vpn["blocked_expired_configs"] += blocked_count
                vpn["unknown_configs"] += unknown_count
                vpn["allocated_configs"] += allocated_count

            server_status = {
                "id": server_id,
                "name": server.get("name") or server_id,
                "enabled": enabled,
                "healthy": healthy,
                "active_count": active_count,
                "started_count": started_count,
                "online_count": online_count,
                "offline_count": offline_count,
                "hold_count": hold_count,
                "blocked_count": blocked_count,
                "unknown_count": unknown_count,
                "allocated_count": allocated_count,
                "weight": weight,
                "load_ratio": (allocated_count / weight) if healthy and weight > 0 else None,
            }
            vpn["servers"].append(server_status)

            if healthy:
                for username, user in _iter_named_user_records(users):
                    username_key = username.lower()
                    live_users["by_server"][(server_id.casefold(), username_key)] = user
                    live_users["by_username"].setdefault(username_key, []).append((server_id, user))
            else:
                live_users["unavailable_servers"].add(server_id)
    except Exception as e:
        vpn["error"] = str(e)
    return vpn, live_users


def _is_regular_paid_payment(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if str(record.get("status", "")).lower() not in PAID_STATUSES:
        return False
    if record.get("type") == "settlement" or record.get("plan_gb") == "Settlement":
        return False
    return True


def _sold_record_removed(record: dict) -> bool:
    return bool(
        record.get("removed_from_vpn")
        or record.get("cleanup_deleted_at")
        or str(record.get("cleanup_status") or "").lower() in {"deleted", "already_missing"}
        or str(record.get("cleanup_delete_result") or "").lower() in {"deleted", "already_missing"}
    )


def _sold_record_timestamp(record: dict):
    for field in ("renewal_applied_at", "updated_at", "created_at", "completed_at", "timestamp"):
        parsed = _parse_datetime(record.get(field))
        if parsed is not None:
            return parsed
    return None


def _live_sold_identity(live_users: dict, server_id, username):
    username_key = str(username or "").casefold()
    if server_id:
        key = (str(server_id).casefold(), username_key)
        return key, live_users.get("by_server", {}).get(key), False
    matches = live_users.get("by_username", {}).get(username_key, [])
    if len(matches) == 1:
        matched_server, user = matches[0]
        return (str(matched_server), username_key), user, False
    return ("", username_key), None, len(matches) > 1


def _load_resellers(reseller_module=None) -> dict:
    try:
        if reseller_module is None:
            _ensure_telegram_utils_path()
            from utils import reseller as reseller_module
        resellers = reseller_module.get_all_resellers()
    except Exception:
        return {}
    return resellers if isinstance(resellers, dict) else {}


def _collect_sold_traffic_stats(payments: dict, live_users: dict, reseller_module=None, resellers=None) -> dict:
    traffic = _sold_traffic_snapshot()
    traffic["unavailable_servers"] = len(live_users.get("unavailable_servers", set()))
    traffic["partial"] = traffic["unavailable_servers"] > 0
    candidates = {}

    def add_candidate(source, record, username, server_id):
        if not username:
            traffic["skipped_no_username"] += 1
            return
        if _sold_record_removed(record):
            return
        identity, live_user, ambiguous_server = _live_sold_identity(
            live_users, server_id, username
        )
        candidates.setdefault(identity, []).append({
            "source": source,
            "timestamp": _sold_record_timestamp(record),
            "live_user": live_user,
            "ambiguous_server": ambiguous_server,
        })

    for record in (payments or {}).values():
        if not _is_regular_paid_payment(record):
            continue
        add_candidate(
            "direct",
            record,
            record.get("renewal_username") or record.get("username"),
            record.get("renewal_server_id") or record.get("server_id"),
        )

    if resellers is None:
        resellers = _load_resellers(reseller_module)

    if isinstance(resellers, dict):
        for reseller_data in resellers.values():
            configs = reseller_data.get("configs", []) if isinstance(reseller_data, dict) else []
            if not isinstance(configs, list):
                continue
            for config in configs:
                if not isinstance(config, dict):
                    continue
                add_candidate(
                    "reseller",
                    config,
                    config.get("username"),
                    config.get("server_id"),
                )

    for identity, owned_records in candidates.items():
        live_user = next(
            (candidate["live_user"] for candidate in owned_records if candidate["live_user"] is not None),
            None,
        )
        if live_user is None:
            if any(candidate["ambiguous_server"] for candidate in owned_records):
                traffic["ambiguous_configs"] += 1
            else:
                traffic["missing_configs"] += 1
            continue

        dated = [candidate for candidate in owned_records if candidate["timestamp"] is not None]
        if dated:
            latest_timestamp = max(candidate["timestamp"] for candidate in dated)
            newest = [candidate for candidate in dated if candidate["timestamp"] == latest_timestamp]
        else:
            newest = owned_records
        sources = {candidate["source"] for candidate in newest}
        source = next(iter(sources)) if len(sources) == 1 else "unattributed"
        if source == "unattributed":
            traffic["ambiguous_configs"] += 1

        used_bytes = max(0, _safe_int(live_user.get("upload_bytes", 0))) + max(
            0, _safe_int(live_user.get("download_bytes", 0))
        )
        quota_bytes = max(0, _safe_int(live_user.get("max_download_bytes", 0)))
        bucket = traffic[source]
        bucket["used_bytes"] += used_bytes
        bucket["sold_bytes"] += quota_bytes
        bucket["matched_configs"] += 1
        bucket["sold_configs"] += 1
        traffic["total"]["used_bytes"] += used_bytes
        traffic["total"]["sold_bytes"] += quota_bytes

    if traffic["total"]["sold_bytes"] > 0:
        traffic["total"]["usage_percent"] = (traffic["total"]["used_bytes"] / traffic["total"]["sold_bytes"]) * 100
    return traffic


def _collect_checker_financials(payments: dict, receipt_checker_module=None) -> dict:
    financials = {
        "open_account_total": 0.0,
        "unpaid_total": 0.0,
        "share_percent": 0.0,
    }
    try:
        if receipt_checker_module is None:
            _ensure_telegram_utils_path()
            from utils import receipt_checker as receipt_checker_module
        stats = receipt_checker_module.build_receipt_checker_stats(payments)
    except Exception:
        return financials
    if not isinstance(stats, dict):
        return financials
    return {
        "open_account_total": _safe_float(stats.get("open_account_total", 0)),
        "unpaid_total": _safe_float(stats.get("unpaid_total", 0)),
        "share_percent": _safe_float(stats.get("share_percent", 0)),
    }


def _collect_reseller_financials(resellers: dict) -> dict:
    outstanding_debt = sum(
        _safe_float((reseller_data or {}).get("debt", 0))
        for reseller_data in (resellers or {}).values()
        if isinstance(reseller_data, dict)
    )
    return {"outstanding_debt": outstanding_debt}


def _collect_customer_growth_stats(payments: dict, now: datetime, timestamp_resolver) -> dict:
    today = now.date()
    seven_day_start = today - timedelta(days=6)
    last_30_days_start = now - timedelta(days=30)
    first_purchase_by_user = {}
    purchase_dates_by_user = {}
    last30_purchase_users = set()
    returning_30d_users = set()
    paid_orders_without_user_id = 0
    regular_paid_orders = 0

    for record in (payments or {}).values():
        if not _is_regular_paid_payment(record):
            continue
        payment_dt = timestamp_resolver(record)
        if payment_dt is None or payment_dt > now:
            continue
        regular_paid_orders += 1
        user_id = str(record.get('user_id') or '').strip()
        if not user_id:
            paid_orders_without_user_id += 1
            continue
        current_first = first_purchase_by_user.get(user_id)
        if current_first is None or payment_dt < current_first:
            first_purchase_by_user[user_id] = payment_dt
        purchase_dates_by_user.setdefault(user_id, []).append(payment_dt)

    for user_id, dates in purchase_dates_by_user.items():
        sorted_dates = sorted(dates)
        for index, payment_dt in enumerate(sorted_dates):
            if payment_dt >= last_30_days_start:
                last30_purchase_users.add(user_id)
                if index > 0:
                    returning_30d_users.add(user_id)

    first_purchase_dates = [value.date() for value in first_purchase_by_user.values()]
    new_today = sum(1 for value in first_purchase_dates if value == today)
    new_7d = sum(1 for value in first_purchase_dates if seven_day_start <= value <= today)
    new_30d = sum(1 for value in first_purchase_by_user.values() if value >= last_30_days_start)

    return {
        "all_time_paying_customers": len(first_purchase_by_user),
        "regular_paid_orders": regular_paid_orders,
        "paid_orders_without_user_id": paid_orders_without_user_id,
        "new_today": new_today,
        "new_7d": new_7d,
        "new_30d": new_30d,
        "active_30d": len(last30_purchase_users),
        "returning_30d": len(returning_30d_users),
    }


def _collect_referral_stats(referral_module) -> dict:
    try:
        referral_data = referral_module.load_referrals()
    except Exception:
        referral_data = {}
    total_payouts = 0.0
    if isinstance(referral_data, dict) and 'stats' in referral_data:
        for stat in referral_data['stats'].values():
            if isinstance(stat, dict):
                total_payouts += _safe_float(stat.get('total_earnings', 0))
    return {"total_rewards": total_payouts}


def _collect_language_stats(language_module, translations_module) -> dict:
    try:
        lang_prefs = language_module.load_user_languages()
    except Exception:
        lang_prefs = {}
    lang_counts = {}
    if isinstance(lang_prefs, dict):
        for lang in lang_prefs.values():
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
    total_prefs = sum(lang_counts.values())
    languages = []
    for code, count in sorted(lang_counts.items(), key=lambda item: item[1], reverse=True):
        percent = (count / total_prefs) * 100 if total_prefs else 0
        lang_name = getattr(translations_module, "LANGUAGES", {}).get(code, code)
        languages.append({"code": code, "name": lang_name, "count": count, "percent": percent})
    return {"total": total_prefs, "languages": languages}


def build_server_info_snapshot(now=None) -> dict:
    '''Collects server information as structured data.'''
    _ensure_telegram_utils_path()
    from utils import (
        api_client,
        language,
        payment_lifecycle,
        payment_records,
        receipt_checker,
        referral,
        reseller,
        translations,
    )

    now = _parse_datetime(now) if now is not None else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("Invalid server-info timestamp")
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    payments = payment_records.load_payments()
    if not isinstance(payments, dict):
        payments = {}

    resellers = _load_resellers(reseller)
    vpn, live_users = _collect_vpn_and_live_users(api_client)
    traffic = _collect_sold_traffic_stats(payments, live_users, resellers=resellers)
    sales = _collect_payment_stats(payments, now, payment_lifecycle.payment_lifecycle_timestamp)
    customers = _collect_customer_growth_stats(
        payments,
        now,
        payment_lifecycle.payment_lifecycle_timestamp,
    )
    online = build_online_users_from_userlist(vpn)
    referrals = _collect_referral_stats(referral)
    languages = _collect_language_stats(language, translations)
    checker = _collect_checker_financials(payments, receipt_checker)
    reseller_financials = _collect_reseller_financials(resellers)

    return {
        "generated_at": now,
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "ram_percent": ram.percent,
            "ram_used_mb": ram.used // (1024 * 1024),
            "ram_total_mb": ram.total // (1024 * 1024),
            "disk_percent": disk.percent,
            "disk_used_gb": disk.used // (1024 * 1024 * 1024),
            "disk_total_gb": disk.total // (1024 * 1024 * 1024),
        },
        "online": online,
        "vpn": vpn,
        "traffic": traffic,
        "sales": sales,
        "customers": customers,
        "referrals": referrals,
        "languages": languages,
        "checker": checker,
        "resellers": reseller_financials,
    }


def _dashboard_status(snapshot: dict) -> str:
    system = snapshot.get("system", {})
    vpn = snapshot.get("vpn", {})
    sales = snapshot.get("sales", {}).get("buckets", {})
    pending = sales.get("all", {}).get("pending", 0)
    disk_percent = _safe_float(system.get("disk_percent", 0))

    if disk_percent >= 95 or (vpn.get("configured", 0) and vpn.get("healthy", 0) == 0):
        return "🔴 Attention needed"
    if disk_percent >= 85 or vpn.get("unhealthy", 0) or pending or snapshot.get("online", {}).get("status") != "ok":
        return "🟡 Watch"
    return "🟢 Healthy"


def _format_orders(bucket: dict) -> str:
    return (
        f"${bucket['revenue']:,.2f} • {bucket['orders']} orders "
        f"(✅ {bucket['paid']} • ❌ {bucket['failed']} • ⌛ {bucket['expired']} • ⏳ {bucket['pending']})"
    )


def _online_text(online: dict) -> str:
    return str(online.get("count")) if online.get("count") is not None else "N/A"


def _traffic_usage_text(total_traffic: dict) -> str:
    usage_percent = total_traffic.get("usage_percent")
    return f" ({usage_percent:.1f}%)" if usage_percent is not None else ""


def _notable_servers(vpn: dict, limit: int = 3) -> list:
    return sorted(
        vpn.get("servers", []),
        key=lambda item: (item.get("healthy", True), -(item.get("load_ratio") or 0)),
    )[:limit]


def _build_server_info_alerts(snapshot: dict) -> list[str]:
    system = snapshot.get("system", {})
    online = snapshot.get("online", {})
    vpn = snapshot.get("vpn", {})
    traffic = snapshot.get("traffic", {})
    sales = snapshot.get("sales", {})
    customers = snapshot.get("customers", {})
    buckets = sales.get("buckets", {})
    alerts = []

    disk_percent = _safe_float(system.get("disk_percent", 0))
    if disk_percent >= 95:
        alerts.append(f"🔴 Disk critical: {disk_percent}% used")
    elif disk_percent >= 85:
        alerts.append(f"🟡 Disk high: {disk_percent}% used")

    if vpn.get("configured", 0) and vpn.get("healthy", 0) == 0:
        alerts.append("🔴 No healthy enabled VPN userlist is available")
    elif vpn.get("unhealthy", 0):
        alerts.append(f"🟡 Unhealthy VPN servers: {vpn.get('unhealthy', 0)}")
    if vpn.get("error"):
        alerts.append(f"🟡 VPN check error: {vpn.get('error')}")

    if online.get("status") not in (None, "ok"):
        alerts.append(f"🟡 Online users unavailable: {online.get('status')} ({online.get('error')})")

    pending = buckets.get("all", {}).get("pending", 0)
    if pending:
        alerts.append(f"🟡 Pending payments: {pending}")
    future_payments = sales.get("future_timestamp_payments", [])
    if future_payments:
        alerts.append(f"🔴 Future payment timestamps: {len(future_payments)}")
    if traffic.get("missing_configs"):
        alerts.append(f"🟡 Missing sold configs: {traffic.get('missing_configs')}")
    if traffic.get("unavailable_servers"):
        alerts.append(f"🟡 Servers unavailable for traffic matching: {traffic.get('unavailable_servers')}")
    if customers.get("paid_orders_without_user_id"):
        alerts.append(f"🟡 Paid orders without user ID: {customers.get('paid_orders_without_user_id')}")

    return alerts


def _format_business_section(snapshot: dict) -> list[str]:
    sales = snapshot.get("sales", {})
    buckets = sales.get("buckets", {})
    referrals = snapshot.get("referrals", {})
    checker = snapshot.get("checker", {})
    resellers = snapshot.get("resellers", {})
    output = ["💰 **Business**"]
    output.append(f"Today: {_format_orders(buckets.get('today', _empty_order_bucket()))}")
    output.append(f"This Month: {_format_orders(buckets.get('month', _empty_order_bucket()))}")
    output.append(f"Last 30 Days: {_format_orders(buckets.get('last30', _empty_order_bucket()))}")
    output.append(f"All Time: {_format_orders(buckets.get('all', _empty_order_bucket()))}")
    output.append(f"AOV: ${sales.get('aov', {}).get('all', 0):,.2f} all • ${sales.get('aov', {}).get('last30', 0):,.2f} 30d")
    output.append(f"Open Account Base: {_format_toman_amount(checker.get('open_account_total', 0))} Tomans")
    output.append(
        f"Checker Balance ({_format_share_percent(checker.get('share_percent', 0))}%): "
        f"{_format_toman_amount(checker.get('unpaid_total', 0))} Tomans"
    )
    output.append(f"💸 Outstanding Debt: ${_format_usd_amount(resellers.get('outstanding_debt', 0))}")
    pending = buckets.get('all', {}).get('pending', 0)
    if pending:
        output.append(f"⚠️ Pending Payments: {pending}")
    output.append(f"Referral Rewards: ${referrals.get('total_rewards', 0):,.2f}")
    all_revenue = buckets.get('all', {}).get('revenue', 0)
    if all_revenue > 0:
        output.append(f"Referral Share: {(referrals.get('total_rewards', 0) / all_revenue) * 100:.1f}%")

    output.append("")
    output.append("📆 **Last 7 Days Sales**")
    for day in sales.get("daily_sales", []):
        output.append(f"{day['label']}: ${day['revenue']:,.2f} • {day['paid']} paid")

    if sales.get("top_plans_revenue") or sales.get("top_plans_orders"):
        output.append("")
        output.append("🏷️ **Top Plans**")
        if sales.get("top_plans_revenue"):
            revenue_parts = [f"{plan}: ${amount:,.2f}" for plan, amount in sales.get("top_plans_revenue", [])]
            output.append("Revenue: " + " • ".join(revenue_parts))
        if sales.get("top_plans_orders"):
            order_parts = [f"{plan}: {count}" for plan, count in sales.get("top_plans_orders", [])]
            output.append("Orders: " + " • ".join(order_parts))
    return output


def _format_customers_section(snapshot: dict) -> list[str]:
    customers = snapshot.get("customers", {})
    traffic = snapshot.get("traffic", {})
    languages = snapshot.get("languages", {})
    direct_traffic = traffic.get("direct", {})
    reseller_traffic = traffic.get("reseller", {})
    output = ["📈 **Customers**"]
    output.append(f"New Paying Customers: {customers.get('new_today', 0)} today • {customers.get('new_7d', 0)} 7d • {customers.get('new_30d', 0)} 30d")
    output.append(f"Active Paying Customers 30d: {customers.get('active_30d', 0)}")
    output.append(f"Returning Customers 30d: {customers.get('returning_30d', 0)}")
    output.append(f"All-Time Paying Customers: {customers.get('all_time_paying_customers', 0)}")
    output.append(f"Regular Paid Orders: {customers.get('regular_paid_orders', 0)}")
    if customers.get("paid_orders_without_user_id"):
        output.append(f"Paid Orders Without User ID: {customers.get('paid_orders_without_user_id')}")
    output.append("")
    output.append("👥 **Segments**")
    output.append(f"Current Direct Configs: {direct_traffic.get('matched_configs', 0)} live")
    output.append(f"Current Reseller Configs: {reseller_traffic.get('matched_configs', 0)} live")
    output.append("")
    output.append("🌐 **Languages**")
    if languages.get("languages"):
        for lang in languages["languages"][:5]:
            output.append(f"{lang['name']}: {lang['percent']:.1f}% ({lang['count']})")
    else:
        output.append("No language data available.")
    return output


def _format_tech_section(snapshot: dict) -> list[str]:
    system = snapshot.get("system", {})
    online = snapshot.get("online", {})
    vpn = snapshot.get("vpn", {})
    output = ["🖥️ **Tech**"]
    output.append(f"CPU: {system.get('cpu_percent', 0)}%")
    output.append(f"RAM: {system.get('ram_percent', 0)}% ({system.get('ram_used_mb', 0)}MB/{system.get('ram_total_mb', 0)}MB)")
    output.append(f"Disk: {system.get('disk_percent', 0)}% ({system.get('disk_used_gb', 0)}GB/{system.get('disk_total_gb', 0)}GB)")
    output.append(f"Online Users: {_online_text(online)}")
    if online.get("status") not in (None, "ok"):
        output.append(f"Online Check: {online.get('status')} ({online.get('error')})")
    output.append("")
    output.append("⚖️ **VPN**")
    output.append(
        f"Servers: {vpn.get('configured', 0)} configured • {vpn.get('enabled', 0)} enabled • "
        f"{vpn.get('healthy', 0)} healthy • {vpn.get('unhealthy', 0)} unhealthy"
    )
    output.append(
        f"Configs: {vpn.get('started_configs', 0)} started • {vpn.get('online_configs', 0)} online • "
        f"{vpn.get('offline_configs', 0)} offline • {vpn.get('hold_configs', 0)} Hold • "
        f"{vpn.get('blocked_expired_configs', 0)} blocked • {vpn.get('unknown_configs', 0)} unknown"
    )
    output.append(f"Allocated Capacity: {vpn.get('allocated_configs', 0)}")
    for server in _notable_servers(vpn):
        health = "healthy" if server.get("healthy") else "unhealthy"
        load_ratio = server.get("load_ratio")
        load_text = f"{load_ratio:.2f}" if load_ratio is not None else "N/A"
        output.append(
            f"- {server.get('name')}: {health} • started {server.get('started_count', 'N/A')} • "
            f"online {server.get('online_count', 'N/A')} • offline {server.get('offline_count', 'N/A')} • "
            f"Hold {server.get('hold_count', 'N/A')} • blocked {server.get('blocked_count', 'N/A')} • "
            f"unknown {server.get('unknown_count', 'N/A')} • load {load_text}"
        )
    if vpn.get("error"):
        output.append(f"VPN Check: error ({vpn.get('error')})")
    return output


def _format_traffic_section(snapshot: dict) -> list[str]:
    traffic = snapshot.get("traffic", {})
    total_traffic = traffic.get("total", {})
    direct_traffic = traffic.get("direct", {})
    reseller_traffic = traffic.get("reseller", {})
    unattributed_traffic = traffic.get("unattributed", {})
    output = ["🚦 **Traffic**"]
    output.append(
        f"Current Sold Footprint: {_format_bytes(total_traffic.get('used_bytes', 0))} served / "
        f"{_format_bytes(total_traffic.get('sold_bytes', 0))} allocated{_traffic_usage_text(total_traffic)}"
    )
    output.append(
        f"Direct: {_format_bytes(direct_traffic.get('used_bytes', 0))} / "
        f"{_format_bytes(direct_traffic.get('sold_bytes', 0))} • "
        f"{direct_traffic.get('matched_configs', 0)} configs"
    )
    output.append(
        f"Reseller: {_format_bytes(reseller_traffic.get('used_bytes', 0))} / "
        f"{_format_bytes(reseller_traffic.get('sold_bytes', 0))} • "
        f"{reseller_traffic.get('matched_configs', 0)} configs"
    )
    if unattributed_traffic.get("matched_configs"):
        output.append(
            f"Unattributed: {_format_bytes(unattributed_traffic.get('used_bytes', 0))} / "
            f"{_format_bytes(unattributed_traffic.get('sold_bytes', 0))} • "
            f"{unattributed_traffic.get('matched_configs', 0)} configs"
        )
    if traffic.get("missing_configs"):
        output.append(f"Historical Local Configs Missing From VPN: {traffic.get('missing_configs')}")
    if traffic.get("ambiguous_configs"):
        output.append(f"Ambiguous Current Identities: {traffic.get('ambiguous_configs')}")
    if traffic.get("skipped_no_username"):
        output.append(f"Sold Records Without Username: {traffic.get('skipped_no_username')}")
    if traffic.get("unavailable_servers"):
        output.append(f"Unavailable Servers For Traffic: {traffic.get('unavailable_servers')}")
        output.append("Traffic Footprint: partial")
    return output


def _format_alerts_section(snapshot: dict) -> list[str]:
    output = ["⚠️ **Alerts**"]
    alerts = _build_server_info_alerts(snapshot)
    if alerts:
        output.extend(alerts)
    else:
        output.append("No active alerts.")
    return output


def _format_overview_section(snapshot: dict) -> list[str]:
    sales = snapshot.get("sales", {})
    buckets = sales.get("buckets", {})
    customers = snapshot.get("customers", {})
    online = snapshot.get("online", {})
    vpn = snapshot.get("vpn", {})
    alerts = _build_server_info_alerts(snapshot)
    today_bucket = buckets.get("today", _empty_order_bucket())
    last30_bucket = buckets.get("last30", _empty_order_bucket())
    output = ["📌 **Overview**"]
    output.append(f"Status: {_dashboard_status(snapshot)}")
    output.append(f"Today Revenue: ${today_bucket.get('revenue', 0):,.2f} • {today_bucket.get('paid', 0)} paid")
    output.append(f"30d Revenue: ${last30_bucket.get('revenue', 0):,.2f} • {last30_bucket.get('paid', 0)} paid")
    output.append(f"Online Users: {_online_text(online)}")
    output.append(
        f"Configs: {vpn.get('started_configs', 0)} started • {vpn.get('online_configs', 0)} online • "
        f"{vpn.get('offline_configs', 0)} offline • {vpn.get('hold_configs', 0)} Hold • "
        f"{vpn.get('blocked_expired_configs', 0)} blocked • {vpn.get('unknown_configs', 0)} unknown"
    )
    output.append(f"New Customers: {customers.get('new_today', 0)} today • {customers.get('new_7d', 0)} 7d • {customers.get('new_30d', 0)} 30d")
    output.append(f"Returning Customers 30d: {customers.get('returning_30d', 0)}")
    output.append(f"Top Alert: {alerts[0] if alerts else 'No active alerts.'}")
    return output


def format_server_info_section(snapshot: dict, section: str = "overview") -> str:
    normalized = str(section or "overview").lower()
    if normalized not in SERVER_INFO_SECTIONS:
        normalized = "overview"
    if normalized == "full":
        return format_server_info(snapshot)

    formatters = {
        "overview": _format_overview_section,
        "business": _format_business_section,
        "customers": _format_customers_section,
        "tech": _format_tech_section,
        "traffic": _format_traffic_section,
        "alerts": _format_alerts_section,
    }
    output = formatters[normalized](snapshot)
    generated_at = snapshot.get("generated_at")
    if isinstance(generated_at, datetime):
        output.append("")
        output.append(f"Updated: {generated_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return "\n".join(output)


def format_server_info(snapshot: dict) -> str:
    '''Formats a server information snapshot for Telegram/CLI output.'''
    system = snapshot.get("system", {})
    online = snapshot.get("online", {})
    vpn = snapshot.get("vpn", {})
    traffic = snapshot.get("traffic", {})
    sales = snapshot.get("sales", {})
    buckets = sales.get("buckets", {})
    referrals = snapshot.get("referrals", {})
    languages = snapshot.get("languages", {})

    online_text = _online_text(online)
    total_traffic = traffic.get("total", {})
    direct_traffic = traffic.get("direct", {})
    reseller_traffic = traffic.get("reseller", {})
    unattributed_traffic = traffic.get("unattributed", {})
    usage_text = _traffic_usage_text(total_traffic)

    output = []
    output.append("📊 **Server Info**")
    output.append(f"Status: {_dashboard_status(snapshot)}")
    output.append("")
    output.append("🖥️ **System**")
    output.append(f"CPU: {system.get('cpu_percent', 0)}% • RAM: {system.get('ram_percent', 0)}% ({system.get('ram_used_mb', 0)}MB/{system.get('ram_total_mb', 0)}MB)")
    output.append(f"Disk: {system.get('disk_percent', 0)}% ({system.get('disk_used_gb', 0)}GB/{system.get('disk_total_gb', 0)}GB)")
    output.append(f"Online Users: {online_text}")
    if online.get("status") not in (None, "ok"):
        output.append(f"Online Check: {online.get('status')} ({online.get('error')})")
    output.append("")
    output.append("⚖️ **VPN**")
    output.append(
        f"Servers: {vpn.get('configured', 0)} configured • {vpn.get('enabled', 0)} enabled • "
        f"{vpn.get('healthy', 0)} healthy • {vpn.get('unhealthy', 0)} unhealthy"
    )
    output.append(
        f"Configs: {vpn.get('started_configs', 0)} started • {vpn.get('online_configs', 0)} online • "
        f"{vpn.get('offline_configs', 0)} offline • {vpn.get('hold_configs', 0)} Hold • "
        f"{vpn.get('blocked_expired_configs', 0)} blocked • {vpn.get('unknown_configs', 0)} unknown"
    )
    output.append(f"Allocated Capacity: {vpn.get('allocated_configs', 0)}")
    for server in _notable_servers(vpn):
        health = "healthy" if server.get("healthy") else "unhealthy"
        load_ratio = server.get("load_ratio")
        load_text = f"{load_ratio:.2f}" if load_ratio is not None else "N/A"
        output.append(
            f"- {server.get('name')}: {health} • started {server.get('started_count', 'N/A')} • "
            f"online {server.get('online_count', 'N/A')} • offline {server.get('offline_count', 'N/A')} • "
            f"Hold {server.get('hold_count', 'N/A')} • blocked {server.get('blocked_count', 'N/A')} • "
            f"unknown {server.get('unknown_count', 'N/A')} • load {load_text}"
        )
    if vpn.get("error"):
        output.append(f"VPN Check: error ({vpn.get('error')})")
    output.append("")
    output.append("🚦 **Traffic**")
    output.append(
        f"Current Sold Footprint: {_format_bytes(total_traffic.get('used_bytes', 0))} served / "
        f"{_format_bytes(total_traffic.get('sold_bytes', 0))} allocated{usage_text}"
    )
    output.append(
        f"Direct: {_format_bytes(direct_traffic.get('used_bytes', 0))} / "
        f"{_format_bytes(direct_traffic.get('sold_bytes', 0))} • "
        f"{direct_traffic.get('matched_configs', 0)} configs"
    )
    output.append(
        f"Reseller: {_format_bytes(reseller_traffic.get('used_bytes', 0))} / "
        f"{_format_bytes(reseller_traffic.get('sold_bytes', 0))} • "
        f"{reseller_traffic.get('matched_configs', 0)} configs"
    )
    if unattributed_traffic.get("matched_configs"):
        output.append(
            f"Unattributed: {_format_bytes(unattributed_traffic.get('used_bytes', 0))} / "
            f"{_format_bytes(unattributed_traffic.get('sold_bytes', 0))} • "
            f"{unattributed_traffic.get('matched_configs', 0)} configs"
        )
    if traffic.get("missing_configs"):
        output.append(f"Historical Local Configs Missing From VPN: {traffic.get('missing_configs')}")
    if traffic.get("ambiguous_configs"):
        output.append(f"Ambiguous Current Identities: {traffic.get('ambiguous_configs')}")
    if traffic.get("skipped_no_username"):
        output.append(f"Sold Records Without Username: {traffic.get('skipped_no_username')}")
    if traffic.get("unavailable_servers"):
        output.append(f"Unavailable Servers For Traffic: {traffic.get('unavailable_servers')}")
        output.append("Traffic Footprint: partial")
    output.append("")
    output.append("💰 **Sales**")
    output.append(f"Today: {_format_orders(buckets.get('today', _empty_order_bucket()))}")
    output.append(f"This Month: {_format_orders(buckets.get('month', _empty_order_bucket()))}")
    output.append(f"Last 30 Days: {_format_orders(buckets.get('last30', _empty_order_bucket()))}")
    output.append(f"All Time: {_format_orders(buckets.get('all', _empty_order_bucket()))}")
    output.append(f"AOV: ${sales.get('aov', {}).get('all', 0):,.2f} all • ${sales.get('aov', {}).get('last30', 0):,.2f} 30d")
    pending = buckets.get('all', {}).get('pending', 0)
    if pending:
        output.append(f"⚠️ Pending Payments: {pending}")
    output.append(f"Referral Rewards: ${referrals.get('total_rewards', 0):,.2f}")
    all_revenue = buckets.get('all', {}).get('revenue', 0)
    if all_revenue > 0:
        output.append(f"Referral Share: {(referrals.get('total_rewards', 0) / all_revenue) * 100:.1f}%")
    output.append("")
    output.append("📆 **Last 7 Days Sales**")
    for day in sales.get("daily_sales", []):
        output.append(f"{day['label']}: ${day['revenue']:,.2f} • {day['paid']} paid")

    if sales.get("top_plans_revenue") or sales.get("top_plans_orders"):
        output.append("")
        output.append("🏷️ **Top Plans**")
        if sales.get("top_plans_revenue"):
            revenue_parts = [f"{plan}: ${amount:,.2f}" for plan, amount in sales.get("top_plans_revenue", [])]
            output.append("Revenue: " + " • ".join(revenue_parts))
        if sales.get("top_plans_orders"):
            order_parts = [f"{plan}: {count}" for plan, count in sales.get("top_plans_orders", [])]
            output.append("Orders: " + " • ".join(order_parts))

    output.append("")
    output.append("🌐 **Languages**")
    if languages.get("languages"):
        for lang in languages["languages"][:5]:
            output.append(f"{lang['name']}: {lang['percent']:.1f}% ({lang['count']})")
    else:
        output.append("No language data available.")

    output.append("")
    output.extend(_format_alerts_section(snapshot))
    generated_at = snapshot.get("generated_at")
    if isinstance(generated_at, datetime):
        output.append("")
        output.append(
            f"Updated: {generated_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

    return "\n".join(output)


def server_info(section: str = "full") -> str | None:
    '''Retrieves server information.'''
    try:
        snapshot = build_server_info_snapshot()
        if str(section or "full").lower() == "full":
            return format_server_info(snapshot)
        return format_server_info_section(snapshot, section)
    except Exception as e:
        return f"Error generating server info: {str(e)}"


def start_telegram_bot(token: str, adminid: str, api_url: str, api_key: str, servers=None):
    '''Starts the Telegram bot.'''
    if not token or not adminid:
        raise InvalidInputError('Error: token and adminid are required for the start action.')
    command_servers = None
    if servers:
        parsed_servers = []
        for item in servers:
            if '=' not in item or ',' not in item:
                raise InvalidInputError('Error: --server must use id=url,token format.')
            server_id, rest = item.split('=', 1)
            parts = rest.split(',')
            if len(parts) < 2:
                raise InvalidInputError('Error: --server must use id=url,token format.')
            server_url, server_token = parts[0], parts[1]
            weight = 1
            enabled = True
            panel = 'blitz'
            default_inbound_ids = []
            default_limit_ip = 0
            if len(parts) >= 3 and parts[2].strip():
                try:
                    weight = float(parts[2].strip())
                except ValueError:
                    raise InvalidInputError('Error: --server weight must be a number.')
                if not math.isfinite(weight) or weight < 0:
                    raise InvalidInputError('Error: --server weight must be a finite non-negative number.')
                weight = 0.0 if weight == 0 else weight
            if len(parts) >= 4 and parts[3].strip():
                enabled = parts[3].strip().lower() not in ('0', 'false', 'no', 'disabled')
            if len(parts) >= 5 and parts[4].strip():
                panel_value = parts[4].strip().lower().replace('_', '-')
                if panel_value in ('3x', '3xui', 'xui', 'x-ui'):
                    panel_value = '3x-ui'
                if panel_value not in ('blitz', '3x-ui'):
                    raise InvalidInputError('Error: --server panel must be blitz or 3x-ui.')
                panel = panel_value
            if len(parts) >= 6 and parts[5].strip():
                try:
                    default_inbound_ids = [int(value) for value in parts[5].split('|') if value.strip()]
                except ValueError:
                    raise InvalidInputError('Error: --server inbound IDs must be positive integers separated by |.')
                if not default_inbound_ids or any(value <= 0 for value in default_inbound_ids):
                    raise InvalidInputError('Error: --server inbound IDs must be positive integers separated by |.')
                default_inbound_ids = list(dict.fromkeys(default_inbound_ids))
            if len(parts) >= 7 and parts[6].strip():
                try:
                    default_limit_ip = int(parts[6].strip())
                except ValueError:
                    raise InvalidInputError('Error: --server IP limit must be a non-negative integer.')
                if default_limit_ip < 0:
                    raise InvalidInputError('Error: --server IP limit must be a non-negative integer.')
            if panel == '3x-ui' and enabled and weight > 0 and not default_inbound_ids:
                raise InvalidInputError('Error: enabled 3x-ui servers require default inbound IDs.')
            server_id = server_id.strip()
            server_url = server_url.strip()
            server_token = server_token.strip()
            if not server_id or not server_url or not server_token:
                raise InvalidInputError('Error: --server must include non-empty id, url, and token.')
            parsed_servers.append({
                'id': server_id,
                'name': server_id,
                'url': server_url,
                'token': server_token,
                'enabled': enabled,
                'weight': weight,
                'panel': panel,
                'default_inbound_ids': default_inbound_ids,
                'default_limit_ip': default_limit_ip,
            })
        if parsed_servers and (not api_url or not api_key):
            api_url = parsed_servers[0]['url']
            api_key = parsed_servers[0]['token']
        command_servers = json.dumps(parsed_servers, separators=(',', ':'))
    if not api_url or not api_key:
        raise InvalidInputError('Error: api_url and api_key are required when no --server is provided.')
    command = ['bash', Command.INSTALL_TELEGRAMBOT.value, 'start', token, adminid, api_url, api_key]
    if command_servers:
        command.append(command_servers)
    run_cmd(command)


def stop_telegram_bot():
    '''Stops the Telegram bot.'''
    run_cmd(['bash', Command.INSTALL_TELEGRAMBOT.value, 'stop'])


def get_services_status() -> dict[str, bool] | None:
    '''Gets the status of all project services.'''
    if res := run_cmd(['bash', Command.SERVICES_STATUS.value]):
        return json.loads(res)

def show_version() -> str | None:
    """Displays the currently installed version of the panel."""
    return run_cmd(['python3', Command.VERSION.value, 'show-version'])


def check_version() -> str | None:
    """Checks if the current version is up-to-date and displays changelog if not."""
    return run_cmd(['python3', Command.VERSION.value, 'check-version'])
# endregion
