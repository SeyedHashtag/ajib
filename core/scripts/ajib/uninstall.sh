#!/bin/bash

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root."
    exit 1
fi

if [ "${1:-}" != "--yes" ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive uninstall requires --yes." >&2
        exit 2
    fi
    read -r -p "Type uninstall to remove ajib (backups are preserved): " confirm
    if [ "$confirm" != "uninstall" ]; then
        echo "Uninstall cancelled."
        exit 0
    fi
fi

systemctl stop ajib-telegram-bot.service 2>/dev/null || true
systemctl disable ajib-telegram-bot.service 2>/dev/null || true
rm -f /etc/systemd/system/ajib-telegram-bot.service
systemctl daemon-reload

install_target=$(readlink -f -- /etc/ajib 2>/dev/null || true)
if [ "$install_target" != "/etc/ajib" ]; then
    echo "Refusing to remove unexpected installation target: ${install_target:-unresolved}" >&2
    exit 1
fi
rm -rf -- "$install_target"
rm -f /usr/local/sbin/ajib
legacy_alias="alias ajib='source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/menu.sh'"
if [ -f "$HOME/.bashrc" ] && grep -Fqx "$legacy_alias" "$HOME/.bashrc"; then
    sed -i "\|^alias ajib='source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/menu.sh'$|d" "$HOME/.bashrc"
fi

echo "ajib Telegram bot uninstalled. Backups in /opt/ajib-backups were preserved."
