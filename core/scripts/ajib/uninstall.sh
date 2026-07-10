#!/bin/bash

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root."
    exit 1
fi

systemctl stop ajib-telegram-bot.service 2>/dev/null || true
systemctl disable ajib-telegram-bot.service 2>/dev/null || true
rm -f /etc/systemd/system/ajib-telegram-bot.service
systemctl daemon-reload

rm -rf /etc/ajib
sed -i '\|alias ajib=.*\/etc\/ajib\/menu.sh|d' "$HOME/.bashrc" 2>/dev/null || true

echo "ajib Telegram bot uninstalled. Backups in /opt/ajib-backups were preserved."
