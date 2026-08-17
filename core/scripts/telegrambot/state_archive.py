#!/usr/bin/env python3
"""Create and prepare versioned ajib state archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath


os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")
BOT_SOURCE_DIR = Path(__file__).resolve().parent
if str(BOT_SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_SOURCE_DIR))

from migrate_state import migrate_legacy_state
from utils import database
from utils.time_utils import format_utc_timestamp


FORMAT_VERSION = 2
PREFIX = PurePosixPath("core/scripts/telegrambot")
MANIFEST_NAME = (PREFIX / "backup_manifest.json").as_posix()
STATIC_FILES = {".env", "plans.json", "support_info.json"}
HOSTED_ASSET_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class StateArchiveError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_name(relative: PurePosixPath | str) -> str:
    return (PREFIX / PurePosixPath(relative)).as_posix()


def _state_files(bot_dir: Path):
    for name in sorted(STATIC_FILES):
        path = bot_dir / name
        if path.is_file():
            yield path, _archive_name(name)
    hosted = bot_dir / "hosted_bots"
    if hosted.is_dir():
        for path in sorted(hosted.rglob("*")):
            if path.is_file() and path.suffix.lower() in HOSTED_ASSET_SUFFIXES:
                yield path, _archive_name(path.relative_to(bot_dir).as_posix())


def create_backup(
    install_dir: str | os.PathLike[str],
    output: str | os.PathLike[str],
) -> str:
    install_root = Path(install_dir).resolve()
    bot_dir = install_root / "core/scripts/telegrambot"
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ajib-backup-") as temp_name:
        temp = Path(temp_name)
        snapshot = temp / database.DATABASE_NAME
        live_database = bot_dir / database.DATABASE_NAME
        if live_database.is_file():
            database.backup_database(snapshot, live_database)
        else:
            import_database = temp / "legacy-import.db"
            migrate_legacy_state(
                bot_dir,
                import_database,
                archive_dir=None,
                remove_legacy=False,
            )
            database.backup_database(snapshot, import_database)
            database.reset_connection(import_database)

        database.reset_connection(snapshot)
        with closing(sqlite3.connect(snapshot)) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise StateArchiveError(f"Database snapshot quick_check failed: {check}")
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            schema_version = int(row[0])

        files = list(_state_files(bot_dir))
        files.append((snapshot, _archive_name(database.DATABASE_NAME)))
        checksums = {archive_name: _sha256(path) for path, archive_name in files}
        manifest = {
            "format_version": FORMAT_VERSION,
            "schema_version": schema_version,
            "created_at": format_utc_timestamp(),
            "files": checksums,
        }

        temporary_output = output_path.with_name(
            f".{output_path.name}.{os.getpid()}.tmp"
        )
        try:
            with zipfile.ZipFile(
                temporary_output,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for path, archive_name in files:
                    archive.write(path, archive_name)
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                )
            with zipfile.ZipFile(temporary_output) as archive:
                damaged = archive.testzip()
                if damaged is not None:
                    raise StateArchiveError(f"Created backup is corrupt at {damaged}")
            os.replace(temporary_output, output_path)
        finally:
            if temporary_output.exists():
                temporary_output.unlink()
    os.chmod(output_path, 0o600)
    return str(output_path)


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if info.is_dir():
        return path
    if path.is_absolute() or ".." in path.parts:
        raise StateArchiveError(f"Unsafe backup entry: {name}")
    if not path.parts[: len(PREFIX.parts)] == PREFIX.parts:
        raise StateArchiveError(f"Unsupported backup entry: {name}")
    return path


def _write_member(archive, info, staging: Path):
    path = _safe_member(info)
    destination = staging.joinpath(*path.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)
    return destination


def _validate_database_file(path: Path, expected_schema=None):
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise StateArchiveError(f"Restored database quick_check failed: {check}")
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            version = int(row[0])
    except sqlite3.Error as error:
        raise StateArchiveError(f"Invalid SQLite backup: {error}") from error
    if version <= 0 or version > database.SCHEMA_VERSION:
        raise StateArchiveError(
            f"Unsupported SQLite schema version {version}; supported through {database.SCHEMA_VERSION}."
        )
    if expected_schema is not None and version != int(expected_schema):
        raise StateArchiveError(
            f"Manifest schema {expected_schema} does not match database schema {version}."
        )
    return version


def _prepare_v2(archive, staging: Path, manifest: dict):
    if manifest.get("format_version") != FORMAT_VERSION:
        raise StateArchiveError(
            f"Unsupported backup format version: {manifest.get('format_version')}"
        )
    checksums = manifest.get("files")
    if not isinstance(checksums, dict) or not checksums:
        raise StateArchiveError("Backup manifest contains no file checksums.")
    members = {
        _safe_member(info).as_posix(): info
        for info in archive.infolist()
        if not info.is_dir()
    }
    expected_names = set(checksums) | {MANIFEST_NAME}
    if set(members) != expected_names:
        raise StateArchiveError("Backup entries do not match the manifest.")
    for name, expected_hash in checksums.items():
        destination = _write_member(archive, members[name], staging)
        actual_hash = _sha256(destination)
        if actual_hash != str(expected_hash):
            raise StateArchiveError(f"Backup checksum mismatch: {name}")
    database_file = staging.joinpath(*PREFIX.parts, database.DATABASE_NAME)
    if not database_file.is_file():
        raise StateArchiveError("Backup does not contain ajib.db.")
    _validate_database_file(database_file, manifest.get("schema_version"))
    return "sqlite"


def _legacy_allowed(relative: PurePosixPath) -> bool:
    if len(relative.parts) == 1:
        return relative.name == ".env" or relative.suffix == ".json"
    return (
        len(relative.parts) >= 3
        and relative.parts[0] == "hosted_bots"
        and relative.parts[1].isdigit()
        and (
            relative.suffix.lower() == ".json"
            or relative.suffix.lower() in HOSTED_ASSET_SUFFIXES
        )
    )


def _prepare_legacy(archive, staging: Path):
    files = []
    for info in archive.infolist():
        path = _safe_member(info)
        if info.is_dir():
            continue
        relative = PurePosixPath(*path.parts[len(PREFIX.parts) :])
        if not _legacy_allowed(relative):
            raise StateArchiveError(f"Unsupported bot state file: {info.filename}")
        destination = _write_member(archive, info, staging)
        files.append((relative, destination))
    if not files:
        raise StateArchiveError("Backup contains no Telegram bot state files.")
    for relative, path in files:
        if relative.suffix.lower() != ".json":
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateArchiveError(f"Invalid JSON backup entry: {relative}: {error}") from error
    staged_bot = staging.joinpath(*PREFIX.parts)
    staged_database = staged_bot / database.DATABASE_NAME
    migrate_legacy_state(
        staged_bot,
        staged_database,
        archive_dir=None,
        remove_legacy=False,
    )
    connection = database.get_connection(staged_database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.reset_connection(staged_database)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{staged_database}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    _validate_database_file(staged_database, database.SCHEMA_VERSION)
    return "legacy"


def prepare_restore(
    archive_path: str | os.PathLike[str],
    staging_dir: str | os.PathLike[str],
) -> str:
    source = Path(archive_path).resolve()
    staging = Path(staging_dir).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        names = {
            _safe_member(info).as_posix(): info
            for info in archive.infolist()
            if not info.is_dir()
        }
        manifest_info = names.get(MANIFEST_NAME)
        if manifest_info is not None:
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StateArchiveError(f"Invalid backup manifest: {error}") from error
            kind = _prepare_v2(archive, staging, manifest)
        else:
            kind = _prepare_legacy(archive, staging)
    return kind


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--install-dir", required=True)
    backup_parser.add_argument("--output", required=True)
    restore_parser = subparsers.add_parser("prepare-restore")
    restore_parser.add_argument("--archive", required=True)
    restore_parser.add_argument("--staging-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.install_dir, args.output)
        else:
            result = prepare_restore(args.archive, args.staging_dir)
    except Exception as error:
        print(f"State archive operation failed: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
