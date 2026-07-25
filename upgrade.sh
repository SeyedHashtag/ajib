#!/bin/bash

set -euo pipefail

INSTALL_DIR="/etc/ajib"
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
REPOSITORY="https://github.com/SeyedHashtag/ajib"
TEMP_DIR=$(mktemp -d)
STATE_DIR="$TEMP_DIR/state"
NEW_DIR="$TEMP_DIR/new"
OLD_DIR="$TEMP_DIR/old"
was_active=false
service_stopped=false
switched=false
upgrade_succeeded=false

cleanup() {
    status=$?
    set +e
    if [ "$switched" = true ] && [ "$upgrade_succeeded" != true ]; then
        rm -rf "$INSTALL_DIR"
        mv "$OLD_DIR" "$INSTALL_DIR"
        systemctl daemon-reload
    fi
    if [ "$service_stopped" = true ] && [ "$upgrade_succeeded" != true ]; then
        systemctl start ajib-telegram-bot.service >/dev/null 2>&1 || true
    fi
    rm -rf "$TEMP_DIR"
    exit "$status"
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root."
    exit 1
fi

if [ ! -d "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR does not exist. Run install.sh first."
    exit 1
fi

git clone "$REPOSITORY" "$NEW_DIR"

if systemctl is-active --quiet ajib-telegram-bot.service; then
    was_active=true
    systemctl stop ajib-telegram-bot.service
    service_stopped=true
fi

mkdir -p "$BACKUP_DIR" "$STATE_DIR"
SAFETY_BACKUP=$(
    AJIB_INSTALL_DIR="$INSTALL_DIR" AJIB_BACKUP_DIR="$BACKUP_DIR" \
        bash "$INSTALL_DIR/core/scripts/ajib/backup.sh"
)

python3 "$NEW_DIR/core/scripts/telegrambot/state_archive.py" prepare-restore \
    --archive "$SAFETY_BACKUP" \
    --staging-dir "$STATE_DIR" >/dev/null

mv "$INSTALL_DIR" "$OLD_DIR"
switched=true
mv "$NEW_DIR" "$INSTALL_DIR"

STAGED_BOT_DIR="$STATE_DIR/core/scripts/telegrambot"
mkdir -p "$BOT_DIR"
for name in .env plans.json support_info.json ajib.db; do
    if [ -f "$STAGED_BOT_DIR/$name" ]; then
        cp -p "$STAGED_BOT_DIR/$name" "$BOT_DIR/$name"
    fi
done
if [ -d "$STAGED_BOT_DIR/hosted_bots" ]; then
    cp -a "$STAGED_BOT_DIR/hosted_bots" "$BOT_DIR/"
fi
chmod 700 "$BOT_DIR"
chmod 600 "$BOT_DIR/ajib.db"

python3 -m venv "$INSTALL_DIR/ajib_venv"
"$INSTALL_DIR/ajib_venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/menu.sh"

service_file="/etc/systemd/system/ajib-telegram-bot.service"
if [ -f "$service_file" ]; then
    sed -i 's#/etc/ajib/core/scripts/telegrambot/tbot.py#/etc/ajib/core/scripts/telegrambot/supervisor.py#g' "$service_file"
    sed -i "s#^ExecStart=/bin/bash -c 'source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/ajib_venv/bin/python /etc/ajib/core/scripts/telegrambot/supervisor.py'#ExecStart=/etc/ajib/ajib_venv/bin/python /etc/ajib/core/scripts/telegrambot/supervisor.py#" "$service_file"
    sed -i 's/^After=network.target$/Wants=network-online.target\nAfter=network-online.target/' "$service_file"
    grep -q '^RestartSec=' "$service_file" || sed -i '/^Restart=/a RestartSec=5s' "$service_file"
    grep -q '^TimeoutStopSec=' "$service_file" || sed -i '/^RestartSec=/a TimeoutStopSec=30s' "$service_file"
    grep -q '^KillMode=' "$service_file" || sed -i '/^TimeoutStopSec=/a KillMode=control-group' "$service_file"
    grep -q '^Environment=PYTHONUNBUFFERED=1$' "$service_file" || sed -i '/^KillMode=/a Environment=PYTHONUNBUFFERED=1' "$service_file"
    grep -q '^UMask=' "$service_file" || sed -i '/^Environment=PYTHONUNBUFFERED=1$/a UMask=0077' "$service_file"
fi

systemctl daemon-reload
if [ "$was_active" = true ]; then
    systemctl start ajib-telegram-bot.service
    systemctl is-active --quiet ajib-telegram-bot.service
fi

upgrade_succeeded=true
echo "ajib Telegram bot upgraded successfully."
