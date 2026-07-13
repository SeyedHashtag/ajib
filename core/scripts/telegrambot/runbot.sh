#!/bin/bash
source /etc/ajib/core/scripts/utils.sh
define_colors

update_env_file() {
    local api_token=$1
    local admin_user_ids=$2
    local api_url=$3
    local api_key=$4
    local servers_json=$5
    local env_file="/etc/ajib/core/scripts/telegrambot/.env"
    local tmp_file
    tmp_file=$(mktemp)

    cat <<EOL > "$tmp_file"
API_TOKEN=$api_token
ADMIN_USER_IDS=[$admin_user_ids]
URL=$api_url
TOKEN=$api_key
EOL
    if [ -n "$servers_json" ]; then
        printf 'SERVERS_JSON=%s\n' "$servers_json" >> "$tmp_file"
    fi
    if [ -f "$env_file" ]; then
        grep -vE '^(API_TOKEN|ADMIN_USER_IDS|URL|TOKEN|SERVERS_JSON)=' "$env_file" >> "$tmp_file"
    fi
    mv "$tmp_file" "$env_file"
}

create_service_file() {
    cat <<EOL > /etc/systemd/system/ajib-telegram-bot.service
[Unit]
Description=ajib Telegram Bot
After=network.target

[Service]
ExecStart=/bin/bash -c 'source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/ajib_venv/bin/python /etc/ajib/core/scripts/telegrambot/supervisor.py'
WorkingDirectory=/etc/ajib/core/scripts/telegrambot
Restart=always

[Install]
WantedBy=multi-user.target
EOL
}

start_service() {
    local api_token=$1
    local admin_user_ids=$2
    local api_url=$3
    local api_key=$4
    local servers_json=$5

    if systemctl is-active --quiet ajib-telegram-bot.service; then
        update_env_file "$api_token" "$admin_user_ids" "$api_url" "$api_key" "$servers_json"
        systemctl restart ajib-telegram-bot.service > /dev/null 2>&1
        echo "The ajib-telegram-bot.service is already running. Configuration updated and service restarted."
        return
    fi

    update_env_file "$api_token" "$admin_user_ids" "$api_url" "$api_key" "$servers_json"
    create_service_file

    systemctl daemon-reload
    systemctl enable ajib-telegram-bot.service > /dev/null 2>&1
    systemctl start ajib-telegram-bot.service > /dev/null 2>&1

    if systemctl is-active --quiet ajib-telegram-bot.service; then
        echo -e "${green}ajib bot setup completed. The service is now running. ${NC}"
        echo -e "\n\n"
    else
        echo "ajib bot setup completed. The service failed to start."
    fi
}

stop_service() {
    systemctl stop ajib-telegram-bot.service > /dev/null 2>&1
    systemctl disable ajib-telegram-bot.service > /dev/null 2>&1

    echo -e "\n"

    echo "ajib bot service stopped and disabled. Configuration preserved."
}

case "$1" in
    start)
        start_service "$2" "$3" "$4" "$5" "$6"
        ;;
    stop)
        stop_service
        ;;
    *)
        echo "Usage: $0 {start|stop} <API_TOKEN> <ADMIN_USER_IDS> <API_URL> <API_KEY> [SERVERS_JSON]"
        exit 1
        ;;
esac

define_colors
