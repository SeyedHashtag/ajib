#!/usr/bin/env bash
# AJIB Uninstall Script — Ubuntu
# Safely removes the AJIB Telegram bot installation.
#
# Features:
# - Stops and disables the systemd service
# - Removes service unit file
# - Removes installation directory (/opt/ajib by default)
# - Optionally preserves backups
# - Optionally removes the 'ajib' CLI and service user
#
# Usage:
#   bash uninstall.sh [--yes] [--keep-backups] [--purge-user] [--keep-cli]
#
# Options:
#   --yes           Non-interactive; do not prompt for confirmation
#   --keep-backups  Preserve backups by moving them to /var/backups/ajib-<timestamp>
#   --purge-user    Remove the 'ajib' service user and group after uninstall
#   --keep-cli      Do not remove /usr/local/bin/ajib
#
# Environment overrides:
#   AJIB_INSTALL_DIR     default: /opt/ajib
#   AJIB_SERVICE_NAME    default: ajib-bot
#   AJIB_SERVICE_USER    default: ajib
#   AJIB_SERVICE_GROUP   default: ajib
#   AJIB_CLI_PATH        default: /usr/local/bin/ajib
#
# Exit codes:
#   0 on success
#   1 on error

set -euo pipefail

# -----------------------------
# Defaults and constants
# -----------------------------
SERVICE_NAME="${AJIB_SERVICE_NAME:-ajib-bot}"
SERVICE_USER="${AJIB_SERVICE_USER:-ajib}"
SERVICE_GROUP="${AJIB_SERVICE_GROUP:-ajib}"

INSTALL_DIR="${AJIB_INSTALL_DIR:-/opt/ajib}"
APP_DIR="${INSTALL_DIR}/app"
VENV_DIR="${INSTALL_DIR}/venv"
ENV_FILE="${INSTALL_DIR}/.env"
BACKUPS_DIR="${INSTALL_DIR}/backups"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_PATH="${AJIB_CLI_PATH:-/usr/local/bin/ajib}"

# sudo helper
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

# Colors (optional)
BOLD="$(printf '\033[1m')"
BLUE="$(printf '\033[34m')"
GREEN="$(printf '\033[32m')"
YELLOW="$(printf '\033[33m')"
RED="$(printf '\033[31m')"
RESET="$(printf '\033[0m')"

info()    { printf "%b[i]%b %s\n" "${BLUE}" "${RESET}" "$*"; }
success() { printf "%b[✓]%b %s\n" "${GREEN}" "${RESET}" "$*"; }
warn()    { printf "%b[!]%b %s\n" "${YELLOW}" "${RESET}" "$*"; }
error()   { printf "%b[x]%b %s\n" "${RED}" "${RESET}" "$*" >&2; }

usage() {
  cat <<EOF
${BOLD}AJIB Uninstall Script${RESET}

Usage:
  bash uninstall.sh [--yes] [--keep-backups] [--purge-user] [--keep-cli]

Options:
  --yes           Non-interactive; do not prompt for confirmation
  --keep-backups  Preserve backups by moving them to /var/backups/ajib-<timestamp>
  --purge-user    Remove the '${SERVICE_USER}' service user and group after uninstall
  --keep-cli      Do not remove ${CLI_PATH}
  -h, --help      Show this help and exit

Environment:
  AJIB_INSTALL_DIR     Installation directory (default: ${INSTALL_DIR})
  AJIB_SERVICE_NAME    Systemd service name (default: ${SERVICE_NAME})
  AJIB_SERVICE_USER    Service user (default: ${SERVICE_USER})
  AJIB_SERVICE_GROUP   Service group (default: ${SERVICE_GROUP})
  AJIB_CLI_PATH        CLI path (default: ${CLI_PATH})
EOF
}

confirm() {
  # confirm "Message?" -> returns 0 if yes
  printf "%s [y/N]: " "$*"
  read -r ans
  case "${ans:-}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    error "Missing required command: $1"
    exit 1
  }
}

systemctl_do() {
  if command -v systemctl >/dev/null 2>&1; then
    ${SUDO} systemctl "$@" || true
  fi
}

# -----------------------------
# Parse arguments
# -----------------------------
YES="no"
KEEP_BACKUPS="no"
PURGE_USER="no"
KEEP_CLI="no"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES="yes"; shift ;;
    --keep-backups) KEEP_BACKUPS="yes"; shift ;;
    --purge-user) PURGE_USER="yes"; shift ;;
    --keep-cli) KEEP_CLI="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      error "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

# -----------------------------
# Confirm
# -----------------------------
if [ "${YES}" != "yes" ]; then
  info "This will stop and remove AJIB from ${INSTALL_DIR} and remove the systemd service (${SERVICE_NAME})."
  confirm "Continue with uninstall?" || { warn "Aborted."; exit 0; }
fi

# -----------------------------
# Stop and disable service
# -----------------------------
info "Stopping and disabling service: ${SERVICE_NAME}"
systemctl_do stop "${SERVICE_NAME}"
systemctl_do disable "${SERVICE_NAME}"

# -----------------------------
# Remove systemd unit
# -----------------------------
if [ -f "${SERVICE_FILE}" ]; then
  info "Removing systemd unit: ${SERVICE_FILE}"
  ${SUDO} rm -f "${SERVICE_FILE}"
  systemctl_do daemon-reload
else
  warn "Service unit not found: ${SERVICE_FILE}"
fi

# -----------------------------
# Preserve backups (optional)
# -----------------------------
if [ "${KEEP_BACKUPS}" = "yes" ] && [ -d "${BACKUPS_DIR}" ]; then
  TS="$(date -u +%Y%m%d-%H%M%S)"
  DEST="/var/backups/ajib-${TS}"
  info "Preserving backups to ${DEST}"
  ${SUDO} mkdir -p "${DEST}"
  ${SUDO} cp -a "${BACKUPS_DIR}/." "${DEST}/" || true
  success "Backups preserved in ${DEST}"
fi

# -----------------------------
# Remove installation directory
# -----------------------------
if [ -d "${INSTALL_DIR}" ]; then
  info "Removing installation directory: ${INSTALL_DIR}"
  ${SUDO} rm -rf "${INSTALL_DIR}"
else
  warn "Installation directory not found: ${INSTALL_DIR}"
fi

# -----------------------------
# Remove CLI (optional)
# -----------------------------
if [ "${KEEP_CLI}" = "yes" ]; then
  info "Keeping CLI as requested: ${CLI_PATH}"
else
  if [ -f "${CLI_PATH}" ]; then
    info "Removing CLI: ${CLI_PATH}"
    ${SUDO} rm -f "${CLI_PATH}"
  else
    warn "CLI not found: ${CLI_PATH}"
  fi
fi

# -----------------------------
# Remove service user/group (optional)
# -----------------------------
if [ "${PURGE_USER}" = "yes" ]; then
  info "Removing service user and group: ${SERVICE_USER}:${SERVICE_GROUP}"
  if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    ${SUDO} userdel -r "${SERVICE_USER}" >/dev/null 2>&1 || ${SUDO} userdel "${SERVICE_USER}" || true
  else
    warn "User not found: ${SERVICE_USER}"
  fi
  if getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    ${SUDO} groupdel "${SERVICE_GROUP}" || true
  else
    warn "Group not found: ${SERVICE_GROUP}"
  fi
fi

success "AJIB has been uninstalled."

# Post-uninstall hints
cat <<EOF

Hints:
- If you preserved backups, you can find them under /var/backups (e.g., /var/backups/ajib-YYYYMMDD-HHMMSS).
- To reinstall later, use the installer:
    curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/ajib/deploy/install.sh | sudo bash

EOF
