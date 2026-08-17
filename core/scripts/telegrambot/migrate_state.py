#!/usr/bin/env python3
"""Import legacy ajib JSON state into SQLite exactly once."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import zipfile
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path


os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")
BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from utils import database, state_store
from utils.time_utils import format_utc_filename, format_utc_timestamp


IMPORT_MARKER = "legacy_import_v1"


class LegacyMigrationError(RuntimeError):
    pass


def _recognized_files(root: Path) -> list[tuple[Path, state_store.StateDescriptor]]:
    files = []
    for name in state_store.TOP_LEVEL_STATE:
        path = root / name
        if path.is_file():
            descriptor = state_store.describe_path(path, legacy_root=root, force=True)
            if descriptor is not None:
                files.append((path, descriptor))
    hosted_root = root / "hosted_bots"
    if hosted_root.is_dir():
        for tenant in sorted(hosted_root.iterdir(), key=lambda item: item.name):
            if not tenant.is_dir() or not tenant.name.isdigit():
                continue
            for name in state_store.HOSTED_STATE:
                path = tenant / name
                if not path.is_file():
                    continue
                descriptor = state_store.describe_path(path, legacy_root=root, force=True)
                if descriptor is not None:
                    files.append((path, descriptor))
    priority = {
        "resellers": 0,
        "hosted_registry": 1,
        "hosted_secrets": 2,
        "hosted_settings": 3,
        "payments": 4,
        "ledger": 5,
        "referrals": 6,
    }
    files.sort(key=lambda item: (priority.get(item[1].kind, 10), item[0].as_posix()))
    return files


def _walk_validate(value, path: Path, location="$", field_name=""):
    if isinstance(value, float) and not math.isfinite(value):
        raise LegacyMigrationError(
            f"Legacy state contains a non-finite number: {path}:{location}"
        )
    financial_field = any(
        fragment in str(field_name).lower()
        for fragment in (
            "amount",
            "price",
            "debt",
            "balance",
            "earnings",
            "margin",
            "liability",
            "total_paid",
            "wholesale",
            "retail",
            "settlement",
        )
    )
    if financial_field and isinstance(value, str):
        try:
            numeric = Decimal(value)
        except InvalidOperation:
            numeric = None
        if numeric is not None and not numeric.is_finite():
            raise LegacyMigrationError(
                f"Legacy financial state contains a non-finite number: {path}:{location}"
            )
    if isinstance(value, dict):
        for key, child in value.items():
            _walk_validate(child, path, f"{location}.{key}", str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_validate(child, path, f"{location}[{index}]", field_name)


def _validate_shape(path: Path, descriptor: state_store.StateDescriptor, value):
    list_kinds = {"checker_settlements", "kv_list"}
    expected = list if descriptor.kind in list_kinds else dict
    if not isinstance(value, expected):
        label = "array" if expected is list else "object"
        raise LegacyMigrationError(f"Legacy state must contain a JSON {label}: {path}")
    _walk_validate(value, path)


def _normalize_receipt_paths(value, root: Path):
    if isinstance(value, dict):
        normalized = {}
        for key, child in value.items():
            if key == "receipt_path" and isinstance(child, str) and child:
                raw = Path(child)
                if raw.is_absolute():
                    candidate = raw.resolve()
                    allowed_roots = (root.resolve(), Path(database.DEFAULT_BOT_DIR).resolve())
                    matched = next(
                        (
                            allowed
                            for allowed in allowed_roots
                            if candidate == allowed or allowed in candidate.parents
                        ),
                        None,
                    )
                    if matched is None:
                        raise LegacyMigrationError(
                            f"Hosted receipt path escapes the bot state directory: {child}"
                        )
                    normalized[key] = candidate.relative_to(matched).as_posix()
                    continue
                candidate = (root / raw).resolve()
                allowed = root.resolve()
                if candidate != allowed and allowed not in candidate.parents:
                    raise LegacyMigrationError(
                        f"Hosted receipt path escapes the bot state directory: {child}"
                    )
                normalized[key] = candidate.relative_to(allowed).as_posix()
                continue
            normalized[key] = _normalize_receipt_paths(child, root)
        return normalized
    if isinstance(value, list):
        return [_normalize_receipt_paths(child, root) for child in value]
    return value


def preflight_legacy_state(root: str | os.PathLike[str]):
    legacy_root = Path(root).resolve()
    prepared = []
    for path, descriptor in _recognized_files(legacy_root):
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LegacyMigrationError(f"Unable to read legacy state {path}: {error}") from error
        _validate_shape(path, descriptor, value)
        prepared.append(
            {
                "path": path,
                "relative": path.relative_to(legacy_root),
                "descriptor": descriptor,
                "value": _normalize_receipt_paths(deepcopy(value), legacy_root),
            }
        )
    return prepared


def _create_safety_archive(prepared, archive_dir: str | os.PathLike[str] | None):
    if not prepared or archive_dir is None:
        return None
    destination_dir = Path(archive_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = format_utc_filename()
    archive_path = destination_dir / f"ajib_pre_sqlite_{timestamp}_{os.getpid()}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in prepared:
            archive.write(item["path"], item["relative"].as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise LegacyMigrationError(f"Pre-migration safety archive is corrupt: {archive_path}")
        names = {info.filename for info in archive.infolist() if not info.is_dir()}
        expected = {item["relative"].as_posix() for item in prepared}
        if names != expected:
            raise LegacyMigrationError(
                f"Pre-migration safety archive is incomplete: {archive_path}"
            )
    os.chmod(archive_path, 0o600)
    return archive_path


def _record_count(value):
    return len(value) if isinstance(value, (dict, list)) else 0


def _validate_imported(connection, prepared):
    for item in prepared:
        loaded = state_store.load_descriptor(
            connection,
            item["descriptor"],
            [] if isinstance(item["value"], list) else {},
        )
        if _record_count(loaded) != _record_count(item["value"]):
            raise LegacyMigrationError(
                f"Imported record count mismatch for {item['relative']}: "
                f"{_record_count(item['value'])} source, {_record_count(loaded)} database"
            )
        descriptor = item["descriptor"]
        if descriptor.kind == "resellers":
            for reseller_id, source in item["value"].items():
                target = loaded.get(str(reseller_id), {})
                for field in ("debt", "total_paid"):
                    source_cents = state_store._money_cents(source.get(field, 0))
                    target_cents = state_store._money_cents(target.get(field, 0))
                    if source_cents != target_cents:
                        raise LegacyMigrationError(
                            f"Imported {field} mismatch for reseller {reseller_id}"
                        )
        elif descriptor.kind == "payments":
            for payment_id, source in item["value"].items():
                target = loaded.get(str(payment_id), {})
                if target.get("status") != source.get("status"):
                    raise LegacyMigrationError(
                        f"Imported payment status mismatch for "
                        f"{item['relative']}:{payment_id}"
                    )
        elif descriptor.kind == "ledger":
            for field in (
                "earnings_available",
                "earnings_reserved",
                "referral_liability",
            ):
                if state_store._money_cents(item["value"].get(field, 0)) != state_store._money_cents(
                    loaded.get(field, 0)
                ):
                    raise LegacyMigrationError(
                        f"Imported ledger balance mismatch for {item['relative']}:{field}"
                    )
        elif descriptor.kind == "referrals":
            source_stats = item["value"].get("stats", {})
            loaded_stats = loaded.get("stats", {})
            for user_id, source in source_stats.items():
                target = loaded_stats.get(str(user_id), loaded_stats.get(user_id, {}))
                for field in ("total_earnings", "available_balance"):
                    if state_store._money_cents(source.get(field, 0)) != state_store._money_cents(
                        target.get(field, 0)
                    ):
                        raise LegacyMigrationError(
                            f"Imported referral {field} mismatch for "
                            f"{item['relative']}:{user_id}"
                        )


def migrate_legacy_state(
    legacy_root: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    *,
    archive_dir: str | os.PathLike[str] | None = None,
    remove_legacy: bool = False,
):
    root = Path(legacy_root).resolve()
    target = Path(db_path).resolve()
    existing_marker = database.get_connection(target).execute(
        "SELECT value FROM state_metadata WHERE key=?",
        (IMPORT_MARKER,),
    ).fetchone()
    if existing_marker is not None:
        database.integrity_check(target, quick=True)
        return {
            "status": "already_migrated",
            "database": str(target),
            "files": 0,
            "archive": None,
        }
    prepared = preflight_legacy_state(root)
    archive_path = _create_safety_archive(prepared, archive_dir)

    with database.write_transaction(target, operation="legacy_state_import") as connection:
        marker = connection.execute(
            "SELECT value FROM state_metadata WHERE key=?",
            (IMPORT_MARKER,),
        ).fetchone()
        if marker is not None:
            return {
                "status": "already_migrated",
                "database": str(target),
                "files": len(prepared),
                "archive": str(archive_path) if archive_path else None,
            }
        count = database.user_table_row_count(target)
        if count:
            raise LegacyMigrationError(
                "SQLite contains application data without a legacy import marker; "
                "refusing to merge or overwrite it."
            )
        for item in prepared:
            try:
                state_store.save_descriptor(
                    connection,
                    item["descriptor"],
                    item["value"],
                )
            except (TypeError, ValueError, sqlite3.IntegrityError) as error:
                raise LegacyMigrationError(
                    f"Unable to import {item['path']}: {error}"
                ) from error
        _validate_imported(connection, prepared)
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_result = "\n".join(str(row[0]) for row in integrity_rows)
        if integrity_result.lower() != "ok":
            raise LegacyMigrationError(
                f"SQLite integrity_check failed before import completion: "
                f"{integrity_result}"
            )
        summary = {
            "schema_version": database.SCHEMA_VERSION,
            "source_files": len(prepared),
            "source_records": sum(_record_count(item["value"]) for item in prepared),
            "archive": str(archive_path) if archive_path else None,
            "imported_at": format_utc_timestamp(),
        }
        connection.execute(
            """
            INSERT INTO state_metadata(key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                IMPORT_MARKER,
                json.dumps(summary, sort_keys=True),
                format_utc_timestamp(),
            ),
        )

    database.integrity_check(target)
    if remove_legacy:
        if prepared and archive_path is None:
            raise LegacyMigrationError("Refusing to remove legacy state without a safety archive.")
        for item in prepared:
            item["path"].unlink()
    return {
        "status": "migrated",
        "database": str(target),
        "files": len(prepared),
        "records": sum(_record_count(item["value"]) for item in prepared),
        "archive": str(archive_path) if archive_path else None,
    }


def bootstrap_storage(
    root: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
):
    legacy_root = Path(root or database.bot_dir()).resolve()
    target = Path(db_path or database.database_path()).resolve()
    backup_root = os.getenv("AJIB_BACKUP_DIR", "/opt/ajib-backups")
    result = migrate_legacy_state(
        legacy_root,
        target,
        archive_dir=backup_root,
        remove_legacy=True,
    )
    database.integrity_check(target, quick=True)
    os.environ["AJIB_DB_PATH"] = str(target)
    os.environ["AJIB_SQLITE_ACTIVE"] = "1"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--archive-dir",
        default=os.getenv("AJIB_BACKUP_DIR", "/opt/ajib-backups"),
    )
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="Import successfully but leave legacy JSON files in place.",
    )
    args = parser.parse_args(argv)
    try:
        result = migrate_legacy_state(
            args.legacy_root,
            args.database,
            archive_dir=args.archive_dir,
            remove_legacy=not args.keep_legacy,
        )
    except Exception as error:
        print(f"SQLite state migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
