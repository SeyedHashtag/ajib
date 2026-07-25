#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
BACKUP_FILE=${1:-}
RESTORE_DIR=$(mktemp -d)

cleanup() {
    rm -rf "$RESTORE_DIR"
}
trap cleanup EXIT

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    echo "A valid backup ZIP file is required." >&2
    exit 1
fi

python3 - "$BACKUP_FILE" "$RESTORE_DIR" <<'PY'
import io
import json
import pathlib
import sys
import zipfile

archive_path = pathlib.Path(sys.argv[1])
restore_dir = pathlib.Path(sys.argv[2]).resolve()
prefix = "core/scripts/telegrambot/"

with zipfile.ZipFile(archive_path) as archive:
    members = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = pathlib.PurePosixPath(name)
        if info.is_dir():
            continue
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"Unsafe backup entry: {name}")
        if not name.startswith(prefix):
            raise SystemExit(f"Unsupported backup entry: {name}")
        relative = pathlib.PurePosixPath(name[len(prefix):])
        top_level = len(relative.parts) == 1 and (relative.name == ".env" or relative.suffix == ".json")
        hosted = len(relative.parts) >= 2 and relative.parts[0] == "hosted_bots" and relative.suffix.lower() in {".json", ".jpg", ".jpeg", ".png"}
        if not (top_level or hosted):
            raise SystemExit(f"Unsupported bot state file: {name}")
        members.append(info)
    if not members:
        raise SystemExit("Backup contains no Telegram bot state files.")
    for info in members:
        if pathlib.PurePosixPath(info.filename).suffix.lower() == ".json":
            try:
                with archive.open(info) as raw:
                    with io.TextIOWrapper(raw, encoding="utf-8") as handle:
                        json.load(handle)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SystemExit(f"Invalid JSON backup entry: {info.filename}: {error}")
        archive.extract(info, restore_dir)
PY

timestamp=$(date +%Y%m%d_%H%M%S)
pre_restore_dir="$BACKUP_DIR/restore_pre_backup_$timestamp"
mkdir -p "$pre_restore_dir" "$BOT_DIR"

shopt -s nullglob dotglob
current_files=("$BOT_DIR"/*.env "$BOT_DIR"/*.json)
restored_files=("$RESTORE_DIR/core/scripts/telegrambot"/*.env "$RESTORE_DIR/core/scripts/telegrambot"/*.json)
shopt -u nullglob dotglob

for file in "${current_files[@]}"; do
    cp -p "$file" "$pre_restore_dir/"
done
if [ -d "$BOT_DIR/hosted_bots" ]; then
    cp -a "$BOT_DIR/hosted_bots" "$pre_restore_dir/"
fi
for file in "${restored_files[@]}"; do
    cp -p "$file" "$BOT_DIR/"
done
if [ -d "$RESTORE_DIR/core/scripts/telegrambot/hosted_bots" ]; then
    cp -a "$RESTORE_DIR/core/scripts/telegrambot/hosted_bots" "$BOT_DIR/"
fi
chmod 600 "$BOT_DIR/hosted_bot_tokens.json" "$BOT_DIR/hosted_bots.json" 2>/dev/null || true

if [ "${AJIB_SKIP_SERVICE_RESTART:-0}" != "1" ] && systemctl is-active --quiet ajib-telegram-bot.service; then
    systemctl restart ajib-telegram-bot.service
fi

echo "Telegram bot state restored successfully."
