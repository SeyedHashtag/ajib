#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
BACKUP_FILE="$BACKUP_DIR/ajib_bot_backup_$(date +%Y%m%d_%H%M%S)_$$.zip"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ARCHIVE_HELPER="$SCRIPT_DIR/../telegrambot/state_archive.py"
PYTHON_BIN=${AJIB_PYTHON_BIN:-python3}

if [ -x "$INSTALL_DIR/ajib_venv/bin/python" ]; then
    PYTHON_BIN="$INSTALL_DIR/ajib_venv/bin/python"
fi

mkdir -p "$BACKUP_DIR"
"$PYTHON_BIN" "$ARCHIVE_HELPER" backup \
    --install-dir "$INSTALL_DIR" \
    --output "$BACKUP_FILE"
