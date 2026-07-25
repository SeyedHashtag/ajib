#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
BACKUP_FILE="$BACKUP_DIR/ajib_bot_backup_$(date +%Y%m%d_%H%M%S).zip"

mkdir -p "$BACKUP_DIR"

# Keep the established top-level state patterns while the Python archiver also
# includes nested hosted-bot state.
shopt -s nullglob dotglob
top_level_state_files=("$BOT_DIR"/*.env "$BOT_DIR"/*.json)
shopt -u nullglob dotglob

python3 - "$BACKUP_FILE" "$INSTALL_DIR" <<'PY'
import pathlib
import json
import sys
import zipfile

backup_path = pathlib.Path(sys.argv[1])
install_dir = pathlib.Path(sys.argv[2])
bot_dir = install_dir / "core/scripts/telegrambot"
files = []
for path in bot_dir.rglob("*"):
    if not path.is_file() or path.name.endswith((".lock", ".tmp")):
        continue
    relative_to_bot = path.relative_to(bot_dir)
    is_top_level_state = len(relative_to_bot.parts) == 1 and (path.name == ".env" or path.suffix == ".json")
    is_hosted_state = relative_to_bot.parts[0] == "hosted_bots" and path.suffix.lower() in {".json", ".jpg", ".jpeg", ".png"}
    if is_top_level_state or is_hosted_state:
        files.append(path)

if not files:
    raise SystemExit("Backup failed: no Telegram bot state files were found.")

for path in files:
    if path.suffix.lower() != ".json":
        continue
    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        relative = path.relative_to(install_dir).as_posix()
        raise SystemExit(f"Backup failed: invalid JSON state file: {relative}: {error}")

with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, path.relative_to(install_dir).as_posix())
PY

echo "$BACKUP_FILE"
