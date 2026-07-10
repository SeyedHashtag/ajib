#!/bin/bash

set -euo pipefail

INSTALL_DIR="/etc/ajib"
REPOSITORY="https://github.com/SeyedHashtag/ajib"
install_complete=false
install_started=false

cleanup() {
    status=$?
    if [ "$install_started" = true ] && [ "$install_complete" != true ]; then
        rm -rf "$INSTALL_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root."
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "Unable to determine the operating system."
    exit 1
fi

. /etc/os-release
major_version=${VERSION_ID%%.*}
if { [ "$ID" != "ubuntu" ] || [ "$major_version" -lt 22 ]; } && \
   { [ "$ID" != "debian" ] || [ "$major_version" -lt 11 ]; }; then
    echo "This installer supports Ubuntu 22+ and Debian 11+."
    exit 1
fi

required_packages=(curl git python3 python3-venv procps)
missing_packages=()
for package in "${required_packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "ok installed"; then
        missing_packages+=("$package")
    fi
done

if [ ${#missing_packages[@]} -gt 0 ]; then
    apt-get update -qq
    apt-get install -y -qq "${missing_packages[@]}"
fi

if [ -e "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR already exists. Use upgrade.sh to update an existing installation."
    exit 1
fi

install_started=true
git clone "$REPOSITORY" "$INSTALL_DIR"
python3 -m venv "$INSTALL_DIR/ajib_venv"
"$INSTALL_DIR/ajib_venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/menu.sh"

alias_line="alias ajib='source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/menu.sh'"
if ! grep -Fqx "$alias_line" "$HOME/.bashrc" 2>/dev/null; then
    echo "$alias_line" >> "$HOME/.bashrc"
fi

install_complete=true
echo "ajib Telegram bot installed successfully."
"$INSTALL_DIR/menu.sh"
