#!/bin/bash

set -euo pipefail

INSTALL_DIR="/etc/ajib"
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
REPOSITORY="https://github.com/SeyedHashtag/ajib"
TEMP_DIR=$(mktemp -d)
STATE_DIR="$TEMP_DIR/state"
NEW_DIR="$TEMP_DIR/new"
OLD_DIR="$TEMP_DIR/old"
was_active=false
switched=false
upgrade_succeeded=false

cleanup() {
    status=$?
    set +e
    if [ "$switched" = true ] && [ "$upgrade_succeeded" != true ]; then
        rm -rf "$INSTALL_DIR"
        mv "$OLD_DIR" "$INSTALL_DIR"
        systemctl daemon-reload
        if [ "$was_active" = true ]; then
            systemctl start ajib-telegram-bot.service
        fi
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

mkdir -p "$STATE_DIR"
shopt -s nullglob dotglob
state_files=("$BOT_DIR"/*.env "$BOT_DIR"/*.json)
shopt -u nullglob dotglob
for file in "${state_files[@]}"; do
    cp -p "$file" "$STATE_DIR/"
done

git clone "$REPOSITORY" "$NEW_DIR"

if systemctl is-active --quiet ajib-telegram-bot.service; then
    was_active=true
    systemctl stop ajib-telegram-bot.service
fi

mv "$INSTALL_DIR" "$OLD_DIR"
switched=true
mv "$NEW_DIR" "$INSTALL_DIR"

mkdir -p "$BOT_DIR"
shopt -s nullglob dotglob
saved_files=("$STATE_DIR"/*)
shopt -u nullglob dotglob
for file in "${saved_files[@]}"; do
    cp -p "$file" "$BOT_DIR/"
done

python3 -m venv "$INSTALL_DIR/ajib_venv"
"$INSTALL_DIR/ajib_venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/menu.sh"

systemctl daemon-reload
if [ "$was_active" = true ]; then
    systemctl start ajib-telegram-bot.service
    systemctl is-active --quiet ajib-telegram-bot.service
fi

upgrade_succeeded=true
echo "ajib Telegram bot upgraded successfully."
