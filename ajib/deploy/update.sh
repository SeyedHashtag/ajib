#!/usr/bin/env bash
# AJIB Update Script — Ubuntu
# Wrapper for updating the AJIB Telegram bot installation.
#
# This script prefers using the 'ajib' CLI if available:
#   ajib update [--repo URL] [--branch NAME]
#
# If the CLI is not available, it falls back to:
#   - git fetch/checkout/pull in /opt/ajib/app
#   - pip install -r requirements.txt in /opt/ajib/venv
#   - systemctl restart ajib-bot
#
# Usage:
#   bash update.sh [--repo URL] [--branch NAME] [--no-restart]
#
# Environment overrides:
#   AJIB_REPO_URL     default: https://github.com/SeyedHashtag/ajib.git
#   AJIB_REPO_BRANCH  default: main
#
# Exit codes:
#   0 on success
#   1 on error

set -euo pipefail

# -----------------------------
# Defaults and constants
# -----------------------------
SERVICE_NAME="ajib-bot"
SERVICE_USER="ajib"

INSTALL_DIR="/opt/ajib"
APP_DIR="${INSTALL_DIR}/app"
VENV_DIR="${INSTALL_DIR}/venv"

REPO_URL_DEFAULT="${AJIB_REPO_URL:-https://github.com/SeyedHashtag/ajib.git}"
REPO_BRANCH_DEFAULT="${AJIB_REPO_BRANCH:-main}"

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
${BOLD}AJIB Update Script${RESET}

Usage:
  bash update.sh [--repo URL] [--branch NAME] [--no-restart]

Options:
  --repo URL       Override repository URL (default: ${REPO_URL_DEFAULT})
  --branch NAME    Override branch name (default: ${REPO_BRANCH_DEFAULT})
  --no-restart     Do not restart the service after update
  -h, --help       Show this help and exit

Environment:
  AJIB_REPO_URL     Default repository URL
  AJIB_REPO_BRANCH  Default branch name
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    error "Missing required command: $1"
    exit 1
  }
}

# -----------------------------
# Parse arguments
# -----------------------------
REPO_URL="${REPO_URL_DEFAULT}"
REPO_BRANCH="${REPO_BRANCH_DEFAULT}"
NO_RESTART="no"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      REPO_URL="${2:-}"; [ -n "${REPO_URL}" ] || { error "--repo requires a value"; exit 1; }
      shift 2
      ;;
    --branch)
      REPO_BRANCH="${2:-}"; [ -n "${REPO_BRANCH}" ] || { error "--branch requires a value"; exit 1; }
      shift 2
      ;;
    --no-restart)
      NO_RESTART="yes"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      error "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

# -----------------------------
# Prefer the installed CLI
# -----------------------------
if command -v ajib >/dev/null 2>&1; then
  info "Detected 'ajib' CLI. Running CLI-based update..."
  set -x
  if [ "${REPO_URL}" != "${REPO_URL_DEFAULT}" ] || [ "${REPO_BRANCH}" != "${REPO_BRANCH_DEFAULT}" ]; then
    ajib update --repo "${REPO_URL}" --branch "${REPO_BRANCH}"
  else
    ajib update
  fi
  set +x

  if [ "${NO_RESTART}" = "yes" ]; then
    info "Skipping restart due to --no-restart"
  else
    # The CLI already restarts the service, but keep this for explicitness
    info "Ensuring ${SERVICE_NAME} is restarted"
    ${SUDO} systemctl restart "${SERVICE_NAME}" || true
  fi

  success "Update completed via CLI."
  exit 0
fi

# -----------------------------
# Fallback path (no CLI found)
# -----------------------------
warn "'ajib' CLI not found. Using fallback update routine."

need_cmd git
need_cmd python3
need_cmd systemctl

# Validate app paths
if [ ! -d "${APP_DIR}" ]; then
  error "App directory not found: ${APP_DIR}"
  exit 1
fi
if [ ! -x "${VENV_DIR}/bin/pip" ]; then
  error "Virtualenv not found: ${VENV_DIR}"
  exit 1
fi

# Git update as service user (if possible)
if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  RUN_AS="${SUDO} -u ${SERVICE_USER}"
else
  RUN_AS=""
  warn "Service user '${SERVICE_USER}' not found; running git as current user."
fi

info "Updating repository at ${APP_DIR} (repo: ${REPO_URL}, branch: ${REPO_BRANCH})"
set -x
# Ensure correct remote URL (best-effort)
if [ -d "${APP_DIR}/.git" ]; then
  ${RUN_AS} git -C "${APP_DIR}" remote set-url origin "${REPO_URL}" || true
  ${RUN_AS} git -C "${APP_DIR}" fetch --all --tags
  ${RUN_AS} git -C "${APP_DIR}" checkout "${REPO_BRANCH}"
  ${RUN_AS} git -C "${APP_DIR}" pull --ff-only origin "${REPO_BRANCH}"
else
  error "Directory does not appear to be a git repository: ${APP_DIR}"
  exit 1
fi
set +x

# Install/upgrade dependencies
REQ_FILE=""
if [ -f "${APP_DIR}/ajib/requirements.txt" ]; then
  REQ_FILE="${APP_DIR}/ajib/requirements.txt"
elif [ -f "${APP_DIR}/requirements.txt" ]; then
  REQ_FILE="${APP_DIR}/requirements.txt"
fi

if [ -n "${REQ_FILE}" ]; then
  info "Installing dependencies from ${REQ_FILE}"
  set -x
  "${VENV_DIR}/bin/pip" install --upgrade pip wheel
  "${VENV_DIR}/bin/pip" install -r "${REQ_FILE}"
  set +x
else
  warn "requirements.txt not found; skipping dependency installation."
fi

# Restart service unless suppressed
if [ "${NO_RESTART}" = "yes" ]; then
  info "Skipping restart due to --no-restart"
else
  info "Restarting service: ${SERVICE_NAME}"
  ${SUDO} systemctl restart "${SERVICE_NAME}"
fi

success "Update completed."
