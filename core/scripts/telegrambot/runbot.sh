#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
SYSTEMD_DIR=${AJIB_SYSTEMD_DIR:-/etc/systemd/system}
SYSTEMCTL=${AJIB_SYSTEMCTL:-systemctl}
JOURNALCTL=${AJIB_JOURNALCTL:-journalctl}

# shellcheck source=core/scripts/utils.sh
source "$INSTALL_DIR/core/scripts/utils.sh"
define_colors

SERVICE_NAME="ajib-telegram-bot.service"
SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME"
ENV_FILE="$INSTALL_DIR/core/scripts/telegrambot/.env"

create_service_file() {
    local temporary
    temporary=$(mktemp)
    cat <<EOL > "$temporary"
[Unit]
Description=ajib Telegram Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/ajib_venv/bin/python $INSTALL_DIR/core/scripts/telegrambot/supervisor.py
WorkingDirectory=$INSTALL_DIR/core/scripts/telegrambot
Restart=always
RestartSec=5s
TimeoutStopSec=30s
KillMode=control-group
Environment=PYTHONUNBUFFERED=1
RuntimeDirectory=ajib
RuntimeDirectoryMode=0700
UMask=0077

[Install]
WantedBy=multi-user.target
EOL
    chmod 644 "$temporary"
    mv "$temporary" "$SERVICE_FILE"
}

require_configuration() {
    if [ ! -s "$ENV_FILE" ]; then
        echo "Telegram configuration is missing. Run 'ajib setup' first." >&2
        return 1
    fi
    chmod 600 "$ENV_FILE"
}

start_service() {
    require_configuration
    create_service_file
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" enable "$SERVICE_NAME" >/dev/null

    if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        "$SYSTEMCTL" restart "$SERVICE_NAME"
    else
        "$SYSTEMCTL" start "$SERVICE_NAME"
    fi

    if ! "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        echo "The systemd service did not become active." >&2
        "$JOURNALCTL" -u "$SERVICE_NAME" -n 20 --no-pager >&2 || true
        return 1
    fi
    echo "ajib Telegram service is active; waiting for bot readiness."
}

restart_service() {
    require_configuration
    create_service_file
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" enable "$SERVICE_NAME" >/dev/null
    if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        "$SYSTEMCTL" restart "$SERVICE_NAME"
    else
        "$SYSTEMCTL" start "$SERVICE_NAME"
    fi
    if ! "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        echo "The ajib Telegram service failed to restart." >&2
        "$JOURNALCTL" -u "$SERVICE_NAME" -n 20 --no-pager >&2 || true
        return 1
    fi
    echo "ajib Telegram service restarted; waiting for bot readiness."
}

stop_service() {
    if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        "$SYSTEMCTL" stop "$SERVICE_NAME"
    fi
    "$SYSTEMCTL" disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    echo "ajib Telegram service stopped and disabled. Configuration preserved."
}

case "${1:-}" in
    start)
        start_service
        ;;
    restart)
        restart_service
        ;;
    stop)
        stop_service
        ;;
    *)
        echo "Usage: $0 {start|restart|stop}" >&2
        exit 2
        ;;
esac
