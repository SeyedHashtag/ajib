#!/bin/bash

set -euo pipefail

INSTALL_DIR="/etc/ajib"
BOT_DIR="$INSTALL_DIR/core/scripts/telegrambot"
BACKUP_DIR=${AJIB_BACKUP_DIR:-/opt/ajib-backups}
REPOSITORY="https://github.com/SeyedHashtag/ajib"
LATEST_RELEASE_URL="https://api.github.com/repos/SeyedHashtag/ajib/releases/latest"
CHANNEL=${AJIB_CHANNEL:-stable}
TARGET_VERSION=${AJIB_VERSION:-}
ASSUME_YES=false
SYSTEM_PYTHON=${AJIB_SYSTEM_PYTHON:-/usr/bin/python3}

usage() {
    echo "Usage: upgrade.sh [--channel stable|main] [--version TAG] [--yes]"
}

resolve_latest_release() {
    curl -fsSL -H 'Accept: application/vnd.github+json' "$LATEST_RELEASE_URL" |
        "$SYSTEM_PYTHON" -c 'import json,sys; value=json.load(sys.stdin).get("tag_name", "").strip(); print(value) if value else sys.exit(1)'
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
        --yes)
            ASSUME_YES=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown upgrade option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$CHANNEL" != "stable" ] && [ "$CHANNEL" != "main" ]; then
    echo "Upgrade channel must be stable or main." >&2
    exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This upgrade must run as root." >&2
    exit 1
fi
if [ ! -d "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR does not exist. Run the installer first." >&2
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
if [ ! -x "$SYSTEM_PYTHON" ] || ! "$SYSTEM_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "A system Python 3.10 or newer interpreter is required for upgrades." >&2
    exit 1
fi

CURRENT_VERSION=$(tr -d '\r\n' < "$INSTALL_DIR/VERSION")
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
    if [ "${CURRENT_VERSION#v}" = "${TARGET_VERSION#v}" ]; then
        echo "ajib $CURRENT_VERSION is already the latest stable release."
        exit 0
    fi
fi

echo "Current version: $CURRENT_VERSION"
echo "Upgrade target: $release_label ($CHANNEL channel)"
if [ "$ASSUME_YES" != true ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive upgrades require --yes." >&2
        exit 2
    fi
    read -r -p "Create a safety backup and continue? [y/N]: " confirm
    case "$confirm" in
        y|Y|yes|YES) ;;
        *) echo "Upgrade cancelled."; exit 0 ;;
    esac
fi

TEMP_DIR=$(mktemp -d)
STATE_DIR="$TEMP_DIR/state"
NEW_DIR="$TEMP_DIR/new"
OLD_DIR="$TEMP_DIR/old"
PREVIOUS_ENV_COPY="$TEMP_DIR/env.previous"
was_active=false
service_stopped=false
switched=false
upgrade_succeeded=false

cleanup() {
    status=$?
    set +e
    if [ "$switched" = true ] && [ "$upgrade_succeeded" != true ]; then
        resolved_install=$(readlink -f -- "$INSTALL_DIR" 2>/dev/null || true)
        if [ "$resolved_install" != "/etc/ajib" ]; then
            echo "Refusing to roll back unexpected install target: ${resolved_install:-unresolved}" >&2
            exit 1
        fi
        rm -rf -- "$resolved_install"
        mv "$OLD_DIR" "$INSTALL_DIR"
        install -m 755 "$INSTALL_DIR/ajib.sh" /usr/local/sbin/ajib 2>/dev/null || true
        systemctl daemon-reload
    fi
    if [ "$service_stopped" = true ] && [ "$upgrade_succeeded" != true ]; then
        systemctl start ajib-telegram-bot.service >/dev/null 2>&1 || true
    fi
    resolved_temp=$(readlink -f -- "$TEMP_DIR" 2>/dev/null || true)
    if [ -n "$resolved_temp" ] && [ "$resolved_temp" != "/" ] && [ -d "$resolved_temp" ]; then
        rm -rf -- "$resolved_temp"
    fi
    exit "$status"
}
trap cleanup EXIT

echo "[1/5] Downloading $release_label..."
if ! git clone "${clone_args[@]}" "$REPOSITORY" "$NEW_DIR"; then
    echo "Could not download $release_label. Stable upgrades never fall back to main." >&2
    exit 1
fi

if systemctl is-active --quiet ajib-telegram-bot.service; then
    was_active=true
    systemctl stop ajib-telegram-bot.service
    service_stopped=true
fi

echo "[2/5] Creating and validating the safety backup..."
mkdir -p "$BACKUP_DIR" "$STATE_DIR"
SAFETY_BACKUP=$(
    AJIB_INSTALL_DIR="$INSTALL_DIR" AJIB_BACKUP_DIR="$BACKUP_DIR" \
        bash "$INSTALL_DIR/core/scripts/ajib/backup.sh"
)
"$SYSTEM_PYTHON" "$NEW_DIR/core/scripts/telegrambot/state_archive.py" prepare-restore \
    --archive "$SAFETY_BACKUP" --staging-dir "$STATE_DIR" >/dev/null
if [ -f "$BOT_DIR/.env.previous" ]; then
    cp -p "$BOT_DIR/.env.previous" "$PREVIOUS_ENV_COPY"
fi

echo "[3/5] Switching releases..."
mv "$INSTALL_DIR" "$OLD_DIR"
switched=true
mv "$NEW_DIR" "$INSTALL_DIR"

STAGED_BOT_DIR="$STATE_DIR/core/scripts/telegrambot"
mkdir -p "$BOT_DIR"
for name in .env plans.json support_info.json ajib.db; do
    if [ -f "$STAGED_BOT_DIR/$name" ]; then
        cp -p "$STAGED_BOT_DIR/$name" "$BOT_DIR/$name"
    fi
done
if [ -d "$STAGED_BOT_DIR/hosted_bots" ]; then
    cp -a "$STAGED_BOT_DIR/hosted_bots" "$BOT_DIR/"
fi
if [ -f "$PREVIOUS_ENV_COPY" ]; then
    cp -p "$PREVIOUS_ENV_COPY" "$BOT_DIR/.env.previous"
fi
chmod 700 "$BOT_DIR"
chmod 600 "$BOT_DIR/ajib.db"
chmod 600 "$BOT_DIR/.env" "$BOT_DIR/.env.previous" 2>/dev/null || true

echo "[4/5] Rebuilding the Python environment and command..."
"$SYSTEM_PYTHON" -m venv "$INSTALL_DIR/ajib_venv"
"$INSTALL_DIR/ajib_venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/menu.sh" "$INSTALL_DIR/ajib.sh" "$INSTALL_DIR/core/scripts/telegrambot/runbot.sh"
install -m 755 "$INSTALL_DIR/ajib.sh" /usr/local/sbin/ajib
legacy_alias="alias ajib='source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/menu.sh'"
if [ -f "$HOME/.bashrc" ] && grep -Fqx "$legacy_alias" "$HOME/.bashrc"; then
    sed -i "\|^alias ajib='source /etc/ajib/ajib_venv/bin/activate && /etc/ajib/menu.sh'$|d" "$HOME/.bashrc"
fi

echo "[5/5] Restoring service state..."
systemctl daemon-reload
if [ "$was_active" = true ]; then
    bash "$INSTALL_DIR/core/scripts/telegrambot/runbot.sh" start
fi

upgrade_succeeded=true
echo "ajib upgraded from $CURRENT_VERSION to $release_label."
echo "Safety backup: $SAFETY_BACKUP"
