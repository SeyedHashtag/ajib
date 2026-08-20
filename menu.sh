#!/bin/bash

set -u

# shellcheck source=core/scripts/utils.sh
source /etc/ajib/core/scripts/utils.sh
# shellcheck source=core/scripts/path.sh
source /etc/ajib/core/scripts/path.sh

run_ajib_cli() {
    if [ ! -x "$AJIB_PYTHON" ]; then
        echo "ajib Python environment is missing at $AJIB_PYTHON. Run the ajib installer or upgrade to recreate it." >&2
        return 1
    fi
    "$AJIB_PYTHON" "$CLI_PATH" "$@"
}

require_terminal() {
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        echo "The interactive ajib menu requires a terminal. Use 'ajib --help' for non-interactive commands." >&2
        return 1
    fi
}

pause_menu() {
    if ! read -r -p "Press Enter to continue..."; then
        echo
        return 1
    fi
}

show_header() {
    clear 2>/dev/null || true
    get_system_info
    tput setaf 7 2>/dev/null || true
    tput setab 4 2>/dev/null || true
    tput bold 2>/dev/null || true
    echo "             ajib Bot Manager"
    tput sgr0 2>/dev/null || true
    echo "OS: $OS | ARCH: $ARCH | CPU: $CPU | RAM: $RAM"
    echo
    run_ajib_cli status || true
    echo
}

main_menu() {
    require_terminal || return 1
    define_colors
    while true; do
        show_header
        echo "1. Secure setup / reconfigure"
        echo "2. Manage VPN servers"
        echo "3. Run diagnostics"
        echo "4. Restart bot"
        echo "5. Stop bot"
        echo "6. Show recent logs"
        echo "7. Upgrade ajib"
        echo "0. Exit"
        if ! read -r -p "Choose an option: " choice; then
            echo
            echo "Menu closed."
            return 0
        fi
        case "$choice" in
            1) run_ajib_cli setup || true ;;
            2) run_ajib_cli server manage || true ;;
            3) run_ajib_cli doctor || true ;;
            4) run_ajib_cli restart || true ;;
            5) run_ajib_cli stop || true ;;
            6) run_ajib_cli logs --lines 100 || true ;;
            7) run_ajib_cli upgrade || true ;;
            0) return 0 ;;
            *) echo "Invalid option. Please try again." ;;
        esac
        echo
        pause_menu || return 0
    done
}

case "${1:-menu}" in
    menu) main_menu ;;
    setup) shift; run_ajib_cli setup "$@" ;;
    servers|server)
        shift
        if [ "$#" -eq 0 ]; then
            set -- manage
        fi
        run_ajib_cli server "$@"
        ;;
    *) run_ajib_cli "$@" ;;
esac
