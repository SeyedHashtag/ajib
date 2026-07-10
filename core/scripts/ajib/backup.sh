#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
BACKUP_FILE="$BACKUP_DIR/ajib_bot_backup_$(date +%Y%m%d_%H%M%S).zip"

mkdir -p "$BACKUP_DIR"

shopt -s nullglob dotglob
state_files=("$BOT_DIR"/*.env "$BOT_DIR"/*.json)
shopt -u nullglob dotglob

if [ ${#state_files[@]} -eq 0 ]; then
    echo "Backup failed: no Telegram bot state files were found." >&2
    exit 1
fi

relative_files=()
for file in "${state_files[@]}"; do
    relative_files+=("${file#$INSTALL_DIR/}")
done

python3 - "$BACKUP_FILE" "$INSTALL_DIR" "${relative_files[@]}" <<'PY'
import pathlib
import sys
import zipfile

backup_path = pathlib.Path(sys.argv[1])
install_dir = pathlib.Path(sys.argv[2])
relative_files = sys.argv[3:]

with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for relative_file in relative_files:
        archive.write(install_dir / relative_file, relative_file)
PY

echo "$BACKUP_FILE"
