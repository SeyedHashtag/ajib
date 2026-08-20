#!/bin/bash

set -euo pipefail

INSTALL_DIR="/etc/ajib"
REPOSITORY="https://github.com/SeyedHashtag/ajib"
LATEST_RELEASE_URL="https://api.github.com/repos/SeyedHashtag/ajib/releases/latest"
CHANNEL=${AJIB_CHANNEL:-stable}
TARGET_VERSION=${AJIB_VERSION:-}
install_complete=false
install_started=false

usage() {
    echo "Usage: install.sh [--channel stable|main] [--version TAG]"
}

resolve_latest_release() {
    curl -fsSL -H 'Accept: application/vnd.github+json' "$LATEST_RELEASE_URL" |
        python3 -c 'import json,sys; value=json.load(sys.stdin).get("tag_name", "").strip(); print(value) if value else sys.exit(1)'
}

validate_release_tag() {
    [[ "$1" =~ ^v?[0-9]+([.][0-9]+){2}([.-][A-Za-z0-9.-]+)?$ ]]
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --channel)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            CHANNEL=$2
            shift 2
            ;;
        --version)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            TARGET_VERSION=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown installer option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$CHANNEL" != "stable" ] && [ "$CHANNEL" != "main" ]; then
    echo "Install channel must be stable or main." >&2
    exit 2
fi

cleanup() {
    status=$?
    if [ "$install_started" = true ] && [ "$install_complete" != true ]; then
        resolved_install=$(readlink -f -- "$INSTALL_DIR" 2>/dev/null || true)
        if [ "$resolved_install" = "/etc/ajib" ]; then
            rm -rf -- "$resolved_install"
        else
            echo "Refusing to clean up unexpected install target: ${resolved_install:-unresolved}" >&2
        fi
        rm -f /usr/local/sbin/ajib
    fi
    exit "$status"
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must run as root. Try: curl -fsSL <installer-url> | sudo bash" >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "Unable to determine the operating system." >&2
    exit 1
fi

. /etc/os-release
major_version=${VERSION_ID%%.*}
if { [ "$ID" != "ubuntu" ] || [ "$major_version" -lt 22 ]; } && \
   { [ "$ID" != "debian" ] || [ "$major_version" -lt 12 ]; }; then
    echo "ajib supports Ubuntu 22+ and Debian 12+ (Python 3.10 or newer)." >&2
    exit 1
fi

echo "[1/6] Checking system packages..."
required_packages=(curl git python3 python3-venv procps)
missing_packages=()
for package in "${required_packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "ok installed"; then
        missing_packages+=("$package")
    fi
done
if [ ${#missing_packages[@]} -gt 0 ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing_packages[@]}"
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer is required; found $(python3 --version 2>&1)." >&2
    exit 1
fi

if [ -e "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR already exists. Run 'ajib upgrade' for an existing installation." >&2
    exit 1
fi

if [ -n "$TARGET_VERSION" ]; then
    validate_release_tag "$TARGET_VERSION" || { echo "Invalid release tag: $TARGET_VERSION" >&2; exit 2; }
    clone_args=(--branch "$TARGET_VERSION" --depth 1)
    release_label=$TARGET_VERSION
elif [ "$CHANNEL" = "main" ]; then
    clone_args=(--branch main --depth 1)
    release_label=main
else
    TARGET_VERSION=$(resolve_latest_release) || {
        echo "The latest stable GitHub release could not be resolved." >&2
        exit 1
    }
    TARGET_VERSION=${TARGET_VERSION//$'\r'/}
    TARGET_VERSION=${TARGET_VERSION//$'\n'/}
    if ! validate_release_tag "$TARGET_VERSION"; then
        echo "The stable release version could not be resolved safely." >&2
        exit 1
    fi
    clone_args=(--branch "$TARGET_VERSION" --depth 1)
    release_label=$TARGET_VERSION
fi

echo "[2/6] Downloading ajib $release_label..."
install_started=true
if ! git clone "${clone_args[@]}" "$REPOSITORY" "$INSTALL_DIR"; then
    echo "Could not download $release_label. Stable installs never fall back to main." >&2
    echo "Use --channel main only if you intentionally want development code." >&2
    exit 1
fi

echo "[3/6] Creating the private Python environment..."
python3 -m venv "$INSTALL_DIR/ajib_venv"
"$INSTALL_DIR/ajib_venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

echo "[4/6] Preparing protected state storage..."
AJIB_BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot" \
AJIB_DB_PATH="$INSTALL_DIR/core/scripts/telegrambot/ajib.db" \
"$INSTALL_DIR/ajib_venv/bin/python" \
    "$INSTALL_DIR/core/scripts/telegrambot/migrate_state.py" \
    --legacy-root "$INSTALL_DIR/core/scripts/telegrambot" \
    --database "$INSTALL_DIR/core/scripts/telegrambot/ajib.db" \
    --archive-dir "/opt/ajib-backups" >/dev/null
chmod 700 "$INSTALL_DIR/core/scripts/telegrambot"
chmod 600 "$INSTALL_DIR/core/scripts/telegrambot/ajib.db"

echo "[5/6] Installing the ajib command..."
chmod +x "$INSTALL_DIR/menu.sh" "$INSTALL_DIR/ajib.sh" "$INSTALL_DIR/core/scripts/telegrambot/runbot.sh"
install -m 755 "$INSTALL_DIR/ajib.sh" /usr/local/sbin/ajib

install_complete=true
echo "[6/6] ajib $release_label installed successfully."

if [ -t 0 ] && [ -t 1 ]; then
    echo "Starting secure setup. Press Ctrl-C to leave configuration for later."
    /usr/local/sbin/ajib setup || setup_status=$?
    if [ "${setup_status:-0}" -eq 2 ]; then
        echo "Setup was saved with warnings. Run 'ajib doctor' for details."
    elif [ "${setup_status:-0}" -ne 0 ]; then
        echo "Installation is complete, but setup did not finish. Run 'ajib setup' to retry." >&2
    fi
else
    echo "No terminal detected. Finish configuration with: sudo ajib setup"
fi
