source /etc/ajib/core/scripts/path.sh

# Function to define colors
define_colors() {
    green='\033[0;32m'
    cyan='\033[0;36m'
    red='\033[0;31m'
    yellow='\033[0;33m'
    LPurple='\033[1;35m'
    NC='\033[0m' # No Color
}

get_system_info() {
    if [ -r /etc/os-release ]; then
        . /etc/os-release
        OS=${PRETTY_NAME:-$NAME}
    else
        OS=$(uname -s)
    fi
    ARCH=$(uname -m)
    CPU=$(top -bn1 | grep "Cpu(s)" | awk '{print $2 + $4 "%"}')
    RAM=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }')
}

version_greater_equal() {
    IFS='.' read -r -a local_version_parts <<< "$1"
    IFS='.' read -r -a latest_version_parts <<< "$2"

    for ((i=0; i<${#local_version_parts[@]}; i++)); do
        if [[ -z ${latest_version_parts[i]} ]]; then
            latest_version_parts[i]=0
        fi

        if ((10#${local_version_parts[i]} > 10#${latest_version_parts[i]})); then
            return 0
        elif ((10#${local_version_parts[i]} < 10#${latest_version_parts[i]})); then
            return 1
        fi
    done

    return 0
}

check_version() {
    local_version=$(cat "$LOCALVERSION")
    latest_version=$(curl -fsSL "$LATESTVERSION" 2>/dev/null || true)
    latest_changelog=$(curl -fsSL "$LASTESTCHANGE" 2>/dev/null | awk '/^## v/ {count++; if (count > 2) exit} {print}')

    if [ -z "$latest_version" ]; then
        echo -e "Bot Version: ${cyan}$local_version${NC}"
        return
    fi

    if version_greater_equal "$local_version" "$latest_version"; then
        echo -e "Bot Version: ${cyan}$local_version${NC}"
    else
        echo -e "Bot Version: ${cyan}$local_version${NC}"
        echo -e "Latest Version: ${cyan}$latest_version${NC}"
        echo -e "${yellow}$latest_version Version Change Log:${NC}"
        echo -e "${cyan}$latest_changelog ${NC}"
    fi
}
