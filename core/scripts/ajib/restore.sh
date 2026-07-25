#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
BACKUP_FILE=${1:-}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ARCHIVE_HELPER="$SCRIPT_DIR/../telegrambot/state_archive.py"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
PYTHON_BIN=${AJIB_PYTHON_BIN:-python3}
RESTORE_DIR=$(mktemp -d)
was_active=false
restore_succeeded=false
state_changed=false
safety_backup=

if [ -x "$INSTALL_DIR/ajib_venv/bin/python" ]; then
    PYTHON_BIN="$INSTALL_DIR/ajib_venv/bin/python"
fi

install_prepared_state() {
    source_bot_dir=$1
    source_database="$source_bot_dir/ajib.db"
    if [ ! -f "$source_database" ]; then
        echo "Prepared restore does not contain ajib.db." >&2
        return 1
    fi

    mkdir -p "$BOT_DIR"
    for name in .env plans.json support_info.json; do
        if [ -f "$source_bot_dir/$name" ]; then
            cp -p "$source_bot_dir/$name" "$BOT_DIR/$name"
        fi
    done

    database_temp="$BOT_DIR/ajib.db.restore.$$"
    cp -p "$source_database" "$database_temp"
    chmod 600 "$database_temp"
    rm -f "$BOT_DIR/ajib.db-wal" "$BOT_DIR/ajib.db-shm"
    mv -f "$database_temp" "$BOT_DIR/ajib.db"

    # Mutable JSON is represented by SQLite. Images are restored as an exact
    # snapshot, while application logs and reports remain untouched.
    for name in \
        payments.json resellers.json hosted_bots.json hosted_bot_tokens.json \
        referrals.json checker_settlements.json user_languages.json \
        test_configs.json test_settings.json waiting_test_users.json \
        traffic_alerts.json expired_user_cleanup.json \
        expired_cleanup_schedule.json broadcast_failed_users.json; do
        rm -f "$BOT_DIR/$name"
    done
    if [ -d "$BOT_DIR/hosted_bots" ]; then
        find "$BOT_DIR/hosted_bots" -type f \
            \( -name payments.json -o -name settings.json -o -name ledger.json \
            -o -name referrals.json -o -name languages.json \
            -o -name renewal_tokens.json -o -name notifications.json \
            -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
            -o -iname '*.webp' \) \
            -delete
    fi
    if [ -d "$source_bot_dir/hosted_bots" ]; then
        mkdir -p "$BOT_DIR/hosted_bots"
        cp -a "$source_bot_dir/hosted_bots/." "$BOT_DIR/hosted_bots/"
    fi

    chmod 700 "$BOT_DIR"
    chmod 600 "$BOT_DIR/ajib.db"
    chmod 600 "$BOT_DIR/.env" 2>/dev/null || true
    if [ -d "$BOT_DIR/hosted_bots" ]; then
        find "$BOT_DIR/hosted_bots" -type d -exec chmod 700 {} +
        find "$BOT_DIR/hosted_bots" -type f -exec chmod 600 {} +
    fi
}

cleanup() {
    status=$?
    set +e
    if [ "$restore_succeeded" != true ] && \
        [ "$state_changed" = true ] && [ -f "$safety_backup" ]; then
        rollback_dir="$RESTORE_DIR/rollback"
        mkdir -p "$rollback_dir"
        if "$PYTHON_BIN" "$ARCHIVE_HELPER" prepare-restore \
            --archive "$safety_backup" \
            --staging-dir "$rollback_dir" >/dev/null; then
            install_prepared_state \
                "$rollback_dir/core/scripts/telegrambot" >/dev/null
        else
            echo "Warning: automatic restore rollback validation failed." >&2
        fi
    fi
    rm -rf "$RESTORE_DIR"
    if [ "$restore_succeeded" != true ] && [ "$was_active" = true ]; then
        systemctl start ajib-telegram-bot.service >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    echo "A valid backup ZIP file is required." >&2
    exit 1
fi

"$PYTHON_BIN" "$ARCHIVE_HELPER" prepare-restore \
    --archive "$BACKUP_FILE" \
    --staging-dir "$RESTORE_DIR" >/dev/null

if [ "${AJIB_SKIP_SERVICE_RESTART:-0}" != "1" ] && \
    systemctl is-active --quiet ajib-telegram-bot.service; then
    was_active=true
    systemctl stop ajib-telegram-bot.service
fi

mkdir -p "$BACKUP_DIR" "$BOT_DIR"
safety_backup=$(bash "$BACKUP_SCRIPT")

STAGED_BOT_DIR="$RESTORE_DIR/core/scripts/telegrambot"
state_changed=true
install_prepared_state "$STAGED_BOT_DIR"

if [ "$was_active" = true ]; then
    systemctl start ajib-telegram-bot.service
fi
restore_succeeded=true
echo "Telegram bot state restored successfully."
