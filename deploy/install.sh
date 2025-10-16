#!/usr/bin/env bash
# AJIB Installer — Ubuntu
# Sets up the AJIB Telegram bot as a systemd service with a simple CLI.
# Prompts only for:
#   1) Telegram Bot API token
#   2) Admin numeric user ID
#
# Usage (recommended):
#   curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/ajib/deploy/install.sh | sudo bash
#
# Non-interactive (CI) usage:
#   TELEGRAM_BOT_TOKEN="XXXXX" TELEGRAM_ADMIN_ID="123456789" bash install.sh

set -euo pipefail

# -----------------------------
# Constants
# -----------------------------
SERVICE_NAME="ajib-bot"
SERVICE_USER="ajib"
SERVICE_GROUP="ajib"

INSTALL_DIR="/opt/ajib"
APP_DIR="${INSTALL_DIR}/app"
VENV_DIR="${INSTALL_DIR}/venv"
ENV_FILE="${INSTALL_DIR}/.env"
BACKUPS_DIR="${INSTALL_DIR}/backups"

CLI_DST="/usr/local/bin/ajib"
REPO_URL="${AJIB_REPO_URL:-https://github.com/SeyedHashtag/ajib.git}"
REPO_BRANCH="${AJIB_REPO_BRANCH:-main}"

# -----------------------------
# Helpers
# -----------------------------
log() { printf "%s\n" "$*"; }
info() { printf "\033[34m[i]\033[0m %s\n" "$*"; }
ok() { printf "\033[32m[✓]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[!]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[x]\033[0m %s\n" "$*" >&2; }

need_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    err "This installer must be run as root. Re-run with: sudo bash install.sh"
    exit 1
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    err "Missing required command: $1"
    exit 1
  }
}

apt_install() {
  # Install packages if not present
  local pkgs=("$@")
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends "${pkgs[@]}"
}

# Replace or append KEY=VALUE (quoted) in ENV_FILE
set_env_kv() {
  local key="$1"; shift
  local raw="$*"
  # Escape single quotes for shell-safe single-quoted value
  local val="${raw//\'/\047}"
  local line="${key}='${val}'"

  if [ ! -f "${ENV_FILE}" ]; then
    umask 077
    touch "${ENV_FILE}"
  fi

  if grep -E -q "^${key}=" "${ENV_FILE}"; then
    sed -i -E "s|^${key}=.*|${line}|" "${ENV_FILE}"
  else
    printf "%s\n" "${line}" >> "${ENV_FILE}"
  fi
}

ensure_service_user() {
  if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    info "Creating group: ${SERVICE_GROUP}"
    groupadd --system "${SERVICE_GROUP}"
  fi
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    info "Creating user: ${SERVICE_USER}"
    useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin --comment "AJIB Bot Service" --gid "${SERVICE_GROUP}" "${SERVICE_USER}"
  fi
}

clone_or_update_repo() {
  if [ -d "${APP_DIR}/.git" ]; then
    info "Updating repository in ${APP_DIR}"
    git -C "${APP_DIR}" fetch --all --tags
    git -C "${APP_DIR}" checkout "${REPO_BRANCH}"
    git -C "${APP_DIR}" pull --ff-only origin "${REPO_BRANCH}"
  else
    info "Cloning ${REPO_URL} (branch: ${REPO_BRANCH}) into ${APP_DIR}"
    rm -rf "${APP_DIR}"
    mkdir -p "${APP_DIR}"
    git clone --depth 1 -b "${REPO_BRANCH}" "${REPO_URL}" "${APP_DIR}"
  fi
}

create_venv_and_install() {
  if [ ! -x "${VENV_DIR}/bin/python" ]; then
    info "Creating virtualenv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
  fi
  info "Installing Python dependencies"
  "${VENV_DIR}/bin/pip" install --upgrade pip wheel
  if [ -f "${APP_DIR}/ajib/requirements.txt" ]; then
    "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/ajib/requirements.txt"
  elif [ -f "${APP_DIR}/requirements.txt" ]; then
    "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"
  else
    warn "requirements.txt not found; skipping dependency install"
  fi
}

install_cli() {
  local cli_src=""
  if [ -f "${APP_DIR}/ajib/cli/ajib" ]; then
    cli_src="${APP_DIR}/ajib/cli/ajib"
  elif [ -f "${APP_DIR}/cli/ajib" ]; then
    cli_src="${APP_DIR}/cli/ajib"
  fi

  if [ -z "${cli_src}" ]; then
    warn "CLI file not found in repository; creating a minimal placeholder at ${CLI_DST}"
    cat >/usr/local/bin/ajib <<'EOF'
#!/usr/bin/env bash
echo "AJIB CLI placeholder. The repository CLI could not be found during install."
echo "Try updating the app: sudo -E ajib update"
EOF
    chmod +x /usr/local/bin/ajib
  else
    install -m 0755 "${cli_src}" "${CLI_DST}"
  fi
  ok "Installed CLI: ${CLI_DST}"
}

install_service_unit() {
  local src=""
  if [ -f "${APP_DIR}/ajib/deploy/ajib-bot.service" ]; then
    src="${APP_DIR}/ajib/deploy/ajib-bot.service"
  elif [ -f "${APP_DIR}/deploy/ajib-bot.service" ]; then
    src="${APP_DIR}/deploy/ajib-bot.service"
  fi

  if [ -n "${src}" ]; then
    install -m 0644 "${src}" "/etc/systemd/system/${SERVICE_NAME}.service"
  else
    warn "Service unit not found in repository; writing a default unit."
    cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=AJIB Telegram Bot (VPN customers/resellers/admin)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python -m ajib.bot.core.bot
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  fi
  ok "Installed systemd unit: /etc/systemd/system/${SERVICE_NAME}.service"

  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}"
}

# -----------------------------
# Prompts
# -----------------------------
prompt_bot_credentials() {
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local admin_id="${TELEGRAM_ADMIN_ID:-}"

  if [ -z "${token}" ]; then
    printf "Enter your Telegram Bot API token: "
    read -r token
  fi
  if [ -z "${admin_id}" ]; then
    printf "Enter the primary Admin numeric ID: "
    read -r admin_id
  fi

  if [ -z "${token}" ]; then
    err "Telegram Bot API token is required."
    exit 1
  fi
  if ! printf "%s" "${admin_id}" | grep -Eq '^[0-9]+$'; then
    err "Admin ID must be numeric."
    exit 1
  fi

  # Write to .env
  umask 077
  touch "${ENV_FILE}"
  set_env_kv "TELEGRAM_BOT_TOKEN" "${token}"
  set_env_kv "TELEGRAM_ADMIN_ID" "${admin_id}"

  # Sensible defaults (no prompts)
  set_env_kv "LOG_LEVEL" "INFO"
  set_env_kv "LOCALE_DEFAULT" "en"

  ok "Saved credentials to ${ENV_FILE}"
}

# -----------------------------
# Main
# -----------------------------
main() {
  need_root

  info "Checking base dependencies (git, python3, venv, curl, ca-certificates)"
  # Install base deps if missing
  for cmd in git python3 curl; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      MISSING=1
      break
    fi
  done
  if [ "${MISSING:-0}" -eq 1 ] || ! python3 -m venv --help >/dev/null 2>&1; then
    apt_install git python3 python3-venv python3-pip ca-certificates
  fi

  need_cmd git
  need_cmd python3
  # Check venv support
  python3 -m venv --help >/dev/null 2>&1 || {
    err "python3-venv is not installed properly."
    exit 1
  }

  info "Preparing directories"
  mkdir -p "${INSTALL_DIR}" "${BACKUPS_DIR}"
  ensure_service_user

  info "Fetching AJIB repository"
  clone_or_update_repo

  info "Setting up Python environment"
  create_venv_and_install

  info "Setting up environment file"
  prompt_bot_credentials
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"
  chmod 600 "${ENV_FILE}" || true

  info "Installing CLI"
  install_cli

  info "Installing systemd service"
  install_service_unit

  ok "AJIB installed and started."
  cat <<EOF

Next steps:
- View status:     sudo systemctl status ${SERVICE_NAME}
- Tail logs:       sudo journalctl -u ${SERVICE_NAME} -f
- Manage via CLI:  ajib status | ajib logs -f | ajib update | ajib config set KEY VALUE

You can set additional credentials later. Examples:
  ajib config set BLITZ_API_BASE https://your-blitz-backend.example.com
  ajib config set BLITZ_API_KEY your_blitz_api_key_here
  ajib config set HELEKET_API_KEY your_heleket_api_key_here
  ajib restart

EOF
}

main "$@"
