#!/bin/bash

source /etc/ajib/core/scripts/utils.sh
source /etc/ajib/core/scripts/path.sh

ajib_upgrade(){
    bash <(curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/upgrade.sh)
}

run_ajib_cli() {
    if [ ! -x "$AJIB_PYTHON" ]; then
        echo "ajib Python environment is missing at $AJIB_PYTHON. Run the ajib installer or upgrade to recreate it." >&2
        return 1
    fi

    "$AJIB_PYTHON" "$CLI_PATH" "$@"
}

telegram_env_value() {
    local key=$1
    if [ ! -f "$TELEGRAM_ENV" ]; then
        return
    fi

    grep -E "^${key}=" "$TELEGRAM_ENV" | tail -n 1 | cut -d '=' -f 2-
}

load_existing_telegram_servers() {
    existing_server_ids=()
    existing_server_urls=()
    existing_server_tokens=()
    existing_server_weights=()
    existing_server_enabled=()
    existing_server_panels=()
    existing_server_inbound_ids=()
    existing_server_limit_ips=()

    if [ ! -f "$TELEGRAM_ENV" ]; then
        return
    fi

    while IFS=$'\t' read -r server_id server_url server_token server_weight server_enabled server_panel server_inbound_ids server_limit_ip; do
        [ -z "$server_id" ] && continue
        existing_server_ids+=("$server_id")
        existing_server_urls+=("$server_url")
        existing_server_tokens+=("$server_token")
        existing_server_weights+=("$server_weight")
        existing_server_enabled+=("$server_enabled")
        existing_server_panels+=("$server_panel")
        [ "$server_inbound_ids" = "-" ] && server_inbound_ids=""
        existing_server_inbound_ids+=("$server_inbound_ids")
        existing_server_limit_ips+=("$server_limit_ip")
    done < <(python3 - "$TELEGRAM_ENV" <<'PY'
import json
import sys

env_path = sys.argv[1]
values = {}
try:
    with open(env_path, "r") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
except FileNotFoundError:
    sys.exit(0)

servers = []
raw_servers = values.get("SERVERS_JSON", "")
if raw_servers:
    try:
        parsed = json.loads(raw_servers)
        if isinstance(parsed, list):
            servers = parsed
    except json.JSONDecodeError:
        servers = []

if not servers and values.get("URL") and values.get("TOKEN"):
    servers = [{
        "id": "primary",
        "url": values.get("URL"),
        "token": values.get("TOKEN"),
        "weight": 1,
        "enabled": True,
    }]

for index, server in enumerate(servers):
    if not isinstance(server, dict):
        continue
    server_id = str(server.get("id") or server.get("name") or f"server{index + 1}").strip()
    server_url = str(server.get("url") or server.get("URL") or "").strip()
    server_token = str(server.get("token") or server.get("TOKEN") or "").strip()
    if not server_id or not server_url or not server_token:
        continue
    weight = server.get("weight", 1)
    enabled = server.get("enabled", True)
    enabled_text = "true" if enabled else "false"
    panel = str(server.get("panel") or "blitz").strip().lower()
    inbound_ids = "|".join(str(value) for value in (server.get("default_inbound_ids") or [])) or "-"
    limit_ip = server.get("default_limit_ip", 0)
    print(f"{server_id}\t{server_url}\t{server_token}\t{weight}\t{enabled_text}\t{panel}\t{inbound_ids}\t{limit_ip}")
PY
)
}

mask_secret() {
    local value=$1
    local length=${#value}
    if [ "$length" -le 8 ]; then
        echo "********"
    else
        echo "${value:0:4}...${value: -4}"
    fi
}

read_non_empty() {
    local prompt=$1
    local default_value=$2
    local value

    while true; do
        if [ -n "$default_value" ]; then
            read -e -p "$prompt [$default_value]: " value
            value=${value:-$default_value}
        else
            read -e -p "$prompt: " value
        fi

        if [ -n "$value" ]; then
            echo "$value"
            return
        fi
        echo "Value cannot be empty. Please try again." >&2
    done
}

read_secret_value() {
    local prompt=$1
    local default_value=$2
    local value

    while true; do
        if [ -n "$default_value" ]; then
            read -e -p "$prompt [leave blank to keep current]: " value
            value=${value:-$default_value}
        else
            read -e -p "$prompt: " value
        fi

        if [ -n "$value" ]; then
            echo "$value"
            return
        fi
        echo "Value cannot be empty. Please try again." >&2
    done
}

read_positive_number() {
    local prompt=$1
    local default_value=$2
    local value

    while true; do
        read -e -p "$prompt [$default_value]: " value
        value=${value:-$default_value}
        if [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk "BEGIN {exit !($value > 0)}"; then
            echo "$value"
            return
        fi
        echo "Value must be a positive number. Please try again." >&2
    done
}

read_yes_no_bool() {
    local prompt=$1
    local default_value=$2
    local default_label="y"
    local value

    [ "$default_value" = "false" ] && default_label="n"

    while true; do
        read -e -p "$prompt [y/n, default $default_label]: " value
        value=${value:-$default_label}
        case "$value" in
            y|Y|yes|YES) echo "true"; return ;;
            n|N|no|NO) echo "false"; return ;;
            *) echo "Please answer y or n." >&2 ;;
        esac
    done
}

read_panel_type() {
    local default_value=${1:-blitz}
    local value
    while true; do
        read -e -p "Panel type [blitz/3x-ui, default $default_value]: " value
        value=${value:-$default_value}
        case "${value,,}" in
            blitz) echo "blitz"; return ;;
            3x-ui|3xui|3x|x-ui|xui) echo "3x-ui"; return ;;
            *) echo "Panel type must be blitz or 3x-ui." >&2 ;;
        esac
    done
}

read_inbound_ids() {
    local default_value=$1
    local required=$2
    local value
    while true; do
        read -e -p "Default inbound IDs (pipe-separated)${default_value:+ [$default_value]}: " value
        value=${value:-$default_value}
        if [ -z "$value" ] && [ "$required" != "true" ]; then
            echo ""
            return
        fi
        if [[ "$value" =~ ^[1-9][0-9]*(\|[1-9][0-9]*)*$ ]]; then
            echo "$value"
            return
        fi
        echo "Use positive inbound IDs separated by |. Enabled 3x-ui servers require at least one." >&2
    done
}

read_nonnegative_integer() {
    local prompt=$1
    local default_value=${2:-0}
    local value
    while true; do
        read -e -p "$prompt [$default_value]: " value
        value=${value:-$default_value}
        if [[ "$value" =~ ^[0-9]+$ ]]; then
            echo "$value"
            return
        fi
        echo "Value must be a non-negative integer." >&2
    done
}

read_admin_ids() {
    local default_value=$1
    local value

    while true; do
        if [ -n "$default_value" ]; then
            read -e -p "Enter the admin IDs (comma-separated) [$default_value]: " value
            value=${value:-$default_value}
        else
            read -e -p "Enter the admin IDs (comma-separated): " value
        fi

        if [[ "$value" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
            echo "$value"
            return
        fi
        echo "Admin IDs must be comma-separated numbers with no spaces. Please try again." >&2
    done
}

read_server_id() {
    local prompt=$1
    local default_value=$2
    local value

    while true; do
        value=$(read_non_empty "$prompt" "$default_value")
        if [[ "$value" =~ [,=[:space:]] ]]; then
            echo "Server ID cannot contain spaces, comma, or equals sign. Please try again." >&2
            continue
        fi
        echo "$value"
        return
    done
}

server_id_exists() {
    local needle=$1
    local count=${#server_ids[@]}
    local index

    for ((index=0; index<count; index++)); do
        if [ "${server_ids[$index]}" = "$needle" ]; then
            return 0
        fi
    done
    return 1
}

show_configured_telegram_servers() {
    load_existing_telegram_servers
    echo "======================================"
    echo "        Configured VPN Servers        "
    echo "======================================"

    if [ "${#existing_server_ids[@]}" -eq 0 ]; then
        echo "No VPN servers are configured yet."
    else
        local index
        for ((index=0; index<${#existing_server_ids[@]}; index++)); do
            echo "$((index + 1)). ${existing_server_ids[$index]}"
            echo "   URL: ${existing_server_urls[$index]}"
            echo "   API key: $(mask_secret "${existing_server_tokens[$index]}")"
            echo "   Weight: ${existing_server_weights[$index]}"
            echo "   Enabled for new configs: ${existing_server_enabled[$index]}"
            echo "   Panel: ${existing_server_panels[$index]}"
            echo "   Default inbound IDs: ${existing_server_inbound_ids[$index]:-(none)}"
            echo "   Default 3x-ui IP limit: ${existing_server_limit_ips[$index]:-0}"
        done
    fi
    echo "======================================"
}

configure_telegram_bot() {
    load_existing_telegram_servers

    local existing_token existing_admin_ids default_server_count
    existing_token=$(telegram_env_value "API_TOKEN")
    existing_admin_ids=$(telegram_env_value "ADMIN_USER_IDS" | tr -d '[][:space:]')
    default_server_count=${#existing_server_ids[@]}
    [ "$default_server_count" -eq 0 ] && default_server_count=1

    local token admin_ids server_count
    token=$(read_secret_value "Enter the Telegram bot token" "$existing_token")
    admin_ids=$(read_admin_ids "$existing_admin_ids")

    while true; do
        read -e -p "How many VPN servers do you want to configure? [$default_server_count]: " server_count
        server_count=${server_count:-$default_server_count}
        if [[ "$server_count" =~ ^[0-9]+$ ]] && [ "$server_count" -ge 1 ]; then
            break
        fi
        echo "Server count must be a positive number. Please try again."
    done

    server_ids=()
    local server_urls=()
    local server_tokens=()
    local server_weights=()
    local server_enabled=()
    local server_panels=()
    local server_inbound_ids=()
    local server_limit_ips=()
    local i default_id default_url default_token default_weight default_enabled default_panel default_inbound_ids default_limit_ip
    local current_id current_url current_token current_weight current_enabled current_panel current_inbound_ids current_limit_ip

    for ((i=1; i<=server_count; i++)); do
        default_id="${existing_server_ids[$((i - 1))]}"
        default_url="${existing_server_urls[$((i - 1))]}"
        default_token="${existing_server_tokens[$((i - 1))]}"
        default_weight="${existing_server_weights[$((i - 1))]}"
        default_enabled="${existing_server_enabled[$((i - 1))]}"
        default_panel="${existing_server_panels[$((i - 1))]}"
        default_inbound_ids="${existing_server_inbound_ids[$((i - 1))]}"
        default_limit_ip="${existing_server_limit_ips[$((i - 1))]}"

        if [ -z "$default_id" ]; then
            default_id="server$i"
            [ "$i" -eq 1 ] && default_id="primary"
        fi
        [ -z "$default_weight" ] && default_weight="1"
        [ -z "$default_enabled" ] && default_enabled="true"
        [ -z "$default_panel" ] && default_panel="blitz"
        [ -z "$default_limit_ip" ] && default_limit_ip="0"

        echo "--------------------------------------"
        echo "VPN server $i"

        while true; do
            current_id=$(read_server_id "Server ID" "$default_id")
            if server_id_exists "$current_id"; then
                echo "Server ID '$current_id' is already used. Please choose another ID."
            else
                break
            fi
        done

        while true; do
            current_url=$(read_non_empty "API URL (e.g., http://example.com)" "$default_url")
            if [[ "$current_url" =~ [,[:space:]] ]]; then
                echo "API URLs with commas or spaces are not supported by the current CLI format."
            else
                break
            fi
        done

        current_token=$(read_secret_value "API key" "$default_token")
        while [[ "$current_token" =~ [,[:space:]] ]]; do
            echo "API keys with commas or spaces are not supported by the current CLI format."
            current_token=$(read_secret_value "API key" "")
        done

        current_weight=$(read_positive_number "Balancing weight" "$default_weight")
        current_enabled=$(read_yes_no_bool "Enable this server for new configs?" "$default_enabled")
        current_panel=$(read_panel_type "$default_panel")
        current_inbound_ids=""
        current_limit_ip="0"
        if [ "$current_panel" = "3x-ui" ]; then
            current_inbound_ids=$(read_inbound_ids "$default_inbound_ids" "$current_enabled")
            current_limit_ip=$(read_nonnegative_integer "Default client IP limit (0 = unlimited)" "$default_limit_ip")
        fi

        server_ids+=("$current_id")
        server_urls+=("$current_url")
        server_tokens+=("$current_token")
        server_weights+=("$current_weight")
        server_enabled+=("$current_enabled")
        server_panels+=("$current_panel")
        server_inbound_ids+=("$current_inbound_ids")
        server_limit_ips+=("$current_limit_ip")
    done

    echo "======================================"
    echo "Telegram bot setup summary"
    echo "Bot token: $(mask_secret "$token")"
    echo "Admin IDs: $admin_ids"
    for ((i=0; i<server_count; i++)); do
        echo "$((i + 1)). ${server_ids[$i]} | ${server_urls[$i]} | panel ${server_panels[$i]} | inbounds ${server_inbound_ids[$i]:-(none)} | weight ${server_weights[$i]} | enabled ${server_enabled[$i]}"
    done
    echo "======================================"

    local confirm
    read -e -p "Apply this setup now? [y/n, default y]: " confirm
    confirm=${confirm:-y}
    case "$confirm" in
        y|Y|yes|YES) ;;
        *) echo "Telegram bot setup cancelled."; return ;;
    esac

    local api_url="${server_urls[0]}"
    local api_key="${server_tokens[0]}"
    local server_args=()
    for ((i=0; i<server_count; i++)); do
        server_args+=(--server "${server_ids[$i]}=${server_urls[$i]},${server_tokens[$i]},${server_weights[$i]},${server_enabled[$i]},${server_panels[$i]},${server_inbound_ids[$i]},${server_limit_ips[$i]}")
    done

    run_ajib_cli telegram -a start -t "$token" -aid "$admin_ids" -u "$api_url" -k "$api_key" "${server_args[@]}"
}

add_telegram_vpn_server() {
    load_existing_telegram_servers

    local token admin_ids
    token=$(telegram_env_value "API_TOKEN")
    admin_ids=$(telegram_env_value "ADMIN_USER_IDS" | tr -d '[][:space:]')

    if [ -z "$token" ] || [ -z "$admin_ids" ]; then
        echo "Telegram bot is not configured yet. Run setup/reconfigure first."
        return
    fi

    server_ids=("${existing_server_ids[@]}")
    local server_urls=("${existing_server_urls[@]}")
    local server_tokens=("${existing_server_tokens[@]}")
    local server_weights=("${existing_server_weights[@]}")
    local server_enabled=("${existing_server_enabled[@]}")
    local server_panels=("${existing_server_panels[@]}")
    local server_inbound_ids=("${existing_server_inbound_ids[@]}")
    local server_limit_ips=("${existing_server_limit_ips[@]}")

    local default_id="server$((${#server_ids[@]} + 1))"
    local current_id current_url current_token current_weight current_enabled current_panel current_inbound_ids current_limit_ip

    echo "--------------------------------------"
    echo "Add VPN server for balancing"

    while true; do
        current_id=$(read_server_id "Server ID" "$default_id")
        if server_id_exists "$current_id"; then
            echo "Server ID '$current_id' is already used. Please choose another ID."
        else
            break
        fi
    done

    while true; do
        current_url=$(read_non_empty "API URL (e.g., http://example.com)" "")
        if [[ "$current_url" =~ [,[:space:]] ]]; then
            echo "API URLs with commas or spaces are not supported by the current CLI format."
        else
            break
        fi
    done

    current_token=$(read_secret_value "API key" "")
    while [[ "$current_token" =~ [,[:space:]] ]]; do
        echo "API keys with commas or spaces are not supported by the current CLI format."
        current_token=$(read_secret_value "API key" "")
    done

    current_weight=$(read_positive_number "Balancing weight" "1")
    current_enabled=$(read_yes_no_bool "Enable this server for new configs?" "true")
    current_panel=$(read_panel_type "blitz")
    current_inbound_ids=""
    current_limit_ip="0"
    if [ "$current_panel" = "3x-ui" ]; then
        current_inbound_ids=$(read_inbound_ids "" "$current_enabled")
        current_limit_ip=$(read_nonnegative_integer "Default client IP limit (0 = unlimited)" "0")
    fi

    server_ids+=("$current_id")
    server_urls+=("$current_url")
    server_tokens+=("$current_token")
    server_weights+=("$current_weight")
    server_enabled+=("$current_enabled")
    server_panels+=("$current_panel")
    server_inbound_ids+=("$current_inbound_ids")
    server_limit_ips+=("$current_limit_ip")

    echo "======================================"
    echo "Updated VPN server setup"
    echo "Bot token: $(mask_secret "$token")"
    echo "Admin IDs: $admin_ids"
    local i server_count
    server_count=${#server_ids[@]}
    for ((i=0; i<server_count; i++)); do
        echo "$((i + 1)). ${server_ids[$i]} | ${server_urls[$i]} | panel ${server_panels[$i]} | inbounds ${server_inbound_ids[$i]:-(none)} | weight ${server_weights[$i]} | enabled ${server_enabled[$i]}"
    done
    echo "======================================"

    local confirm
    read -e -p "Add this server and restart/start the bot now? [y/n, default y]: " confirm
    confirm=${confirm:-y}
    case "$confirm" in
        y|Y|yes|YES) ;;
        *) echo "Add VPN server cancelled."; return ;;
    esac

    local api_url="${server_urls[0]}"
    local api_key="${server_tokens[0]}"
    local server_args=()
    for ((i=0; i<server_count; i++)); do
        server_args+=(--server "${server_ids[$i]}=${server_urls[$i]},${server_tokens[$i]},${server_weights[$i]},${server_enabled[$i]},${server_panels[$i]},${server_inbound_ids[$i]},${server_limit_ips[$i]}")
    done

    run_ajib_cli telegram -a start -t "$token" -aid "$admin_ids" -u "$api_url" -k "$api_key" "${server_args[@]}"
}

restart_telegram_bot() {
    if systemctl is-active --quiet ajib-telegram-bot.service; then
        systemctl restart ajib-telegram-bot.service
        echo "Telegram bot service restarted."
    else
        echo "Telegram bot service is not active. Use setup/start first."
    fi
}

telegram_bot_handler() {
    while true; do
        echo "======================================"
        echo "          Telegram Bot Manager        "
        echo "======================================"
        if systemctl is-active --quiet ajib-telegram-bot.service; then
            echo -e "Service: ${green}Active${NC}"
        else
            echo -e "Service: ${red}Inactive${NC}"
        fi
        echo -e "${cyan}1.${NC} Add VPN server for balancing"
        echo -e "${cyan}2.${NC} Setup / reconfigure bot and VPN servers"
        echo -e "${cyan}3.${NC} Show configured VPN servers"
        echo -e "${cyan}4.${NC} Restart Telegram bot service"
        echo -e "${red}5.${NC} Stop Telegram bot service"
        echo "0. Back"
        read -p "Choose an option: " option

        case $option in
            1)
                add_telegram_vpn_server
                ;;
            2)
                configure_telegram_bot
                ;;
            3)
                show_configured_telegram_servers
                ;;
            4)
                restart_telegram_bot
                ;;
            5)
                run_ajib_cli telegram -a stop
                ;;
            0)
                break
                ;;
            *)
                echo "Invalid option. Please try again."
                ;;
        esac
    done
}

# Function to display the main menu
display_main_menu() {
    clear
    tput setaf 7 ; tput setab 4 ; tput bold
    echo -e "◇────────────────🚀 Welcome To ajib Bot Manager 🚀────────────────◇"
    tput sgr0
    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"

    printf "\033[0;32m• OS:   \033[0m%-25s \033[0;32m• ARCH: \033[0m%-25s\n" "$OS" "$ARCH"
    printf "\033[0;32m• CPU:  \033[0m%-25s \033[0;32m• RAM:  \033[0m%-25s\n" "$CPU" "$RAM"

    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"
        check_version
    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"
    echo -e "${yellow}                   ☼ Bot Status ☼                   ${NC}"
    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"

        if systemctl is-active --quiet ajib-telegram-bot.service; then
            echo -e "Telegram Bot: ${green}Active${NC}"
        else
            echo -e "Telegram Bot: ${red}Inactive${NC}"
        fi
        
    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"
    echo -e "${yellow}                   ☼ Main Menu ☼                   ${NC}"

    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"
    echo -e "${cyan}[1] ${NC}↝ Telegram Bot"
    echo -e "${cyan}[2] ${NC}↝ Update Bot"
    echo -e "${red}[0] ${NC}↝ Exit"
    echo -e "${LPurple}◇──────────────────────────────────────────────────────────────────────◇${NC}"
    echo -ne "${yellow}➜ Enter your option: ${NC}"
}

# Function to handle main menu options
main_menu() {
    clear
    local choice
    while true; do
        get_system_info
        display_main_menu
        read -r choice
        case $choice in
            1) telegram_bot_handler ;;
            2) ajib_upgrade ;;
            0) exit 0 ;;
            *) echo "Invalid option. Please try again." ;;
        esac
        echo
        read -rp "Press Enter to continue..."
    done
}

# Main function to run the script
define_colors
main_menu
