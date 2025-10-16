# AJIB — Modular Telegram Bot for VPN Customers, Resellers, and Admins

AJIB is a minimal, modular Telegram bot designed for VPN businesses with a focus on ease of deployment, maintainability, and a great UX for both customers and admins. It runs on Ubuntu, uses Python, and follows an installation and update workflow similar to Blitz (https://github.com/ReturnFI/Blitz). Payments are processed via Heleket crypto (https://doc.heleket.com/) and service provisioning integrates with your backend API (Blitz-compatible).

Repository: https://github.com/SeyedHashtag/ajib

Key goals:
- One-line install on Ubuntu via shell script
- Simple configuration via a dedicated CLI: ajib
- I18n-ready (English and Persian)
- Clean, minimal structure for easy maintenance


## Features

Customer-facing (via Telegram reply keyboard):
- View active configs
- Purchase service (crypto via Heleket)
- Receive a 1 GB test config
- Select OS and get client download links
- Contact support
- Switch language: English (default) and Persian

Admin-facing (via Telegram reply keyboard):
- Backup and restore bot data
- Broadcast messages (send to Active, Expired, Test, or All users)
- Add or edit pricing plans
- Same simple navigation as customer keyboard

System/DevOps:
- Ubuntu-friendly one-liner installation
- `ajib` CLI for update, uninstall, and configuration
- Systemd service for auto-start and supervision
- Minimal and modular Python codebase


## Architecture Overview

- Bot framework: Python (aiogram or equivalent Telegram bot framework)
- Back-end API: Blitz-compatible API client for provisioning and fetching configs
- Payments: Heleket crypto (invoice creation and payment verification)
- Persistence: Lightweight local DB (e.g., SQLite) and JSON/YAML configs
- Deployment: Systemd service, virtualenv, shell-based installer
- i18n: Locale files (English, Persian)


## Project Structure

This repository is intentionally minimal. The following directories are part of the intended structure:

- bot/
  - core/ 
    - configuration loading, env management, logging, db
  - handlers/ 
    - customer, admin, payments, shared middlewares
  - keyboards/
    - reply keyboards for customer/admin
  - services/
    - blitz_client.py, heleket_client.py, payment orchestrations
  - locales/
    - en.yml, fa.yml (translations and UI strings)
- cli/
  - ajib CLI implementation (installed to /usr/local/bin/ajib)
- deploy/
  - install.sh (one-line installer)
  - update.sh (pull and restart)
  - uninstall.sh (stop and remove, with optional backup)
  - ajib-bot.service (systemd unit template)
- scripts/
  - helper/maintenance scripts (e.g., migrations, data export)
- backups/
  - backup archives (created by CLI or admin bot command)

Notes:
- The actual files will be added incrementally. The README describes the intended structure and behaviors to keep the implementation aligned and maintainable.


## Quick Start (Ubuntu 22.04+)

1) Create a Telegram Bot
- Use @BotFather to create a bot and get the bot token.

2) Find your Admin Numeric ID
- Use @userinfobot (or similar) to get your Telegram numeric user ID.

3) One-line Install (when install script is present)
- This will:
  - Create a system user
  - Install Python requirements in a virtualenv
  - Install the `ajib` CLI to /usr/local/bin/ajib
  - Set up systemd service
  - Ask only for:
    - Telegram Bot API token
    - Primary Admin Numeric ID
- Example:
  curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | bash

4) Start/Check the Bot
- The installer will start the bot automatically. To check status:
  sudo systemctl status ajib-bot
  journalctl -u ajib-bot -f

5) Set Backend & Payment Credentials (post-install)
- By design, the installer only asks for the bot token and admin ID. Set other credentials via the CLI:
  ajib config set BLITZ_API_BASE https://your-blitz-backend.example.com
  ajib config set BLITZ_API_KEY  your_blitz_api_key_here
  ajib config set HELEKET_API_KEY your_heleket_api_key_here
  # Optional: Heleket network/coin settings (refer to Heleket docs)
  ajib config set HELEKET_NETWORK mainnet

- Restart after changes:
  ajib restart


## The ajib CLI

The CLI is installed as /usr/local/bin/ajib and manages the service, updates, and configuration.

Common commands:
- ajib status
  - Show systemd unit status.
- ajib logs
  - Tail logs with journalctl.
- ajib restart
  - Restart the bot service.

- ajib update
  - Pull the latest code (main) and restart the service.
- ajib uninstall
  - Stop service, remove installation. Offers a prompt to keep or remove backups.

- ajib config get KEY
- ajib config set KEY VALUE
- ajib config unset KEY
  - Manage environment and config keys (stored in /opt/ajib/.env or equivalent).

- ajib backup create
- ajib backup list
- ajib backup restore /path/to/backup.tar.gz
  - Create/list/restore backups including .env, local DB, and state.

Notes:
- The CLI mirrors the simplicity of Blitz: explicit, scriptable, safe defaults.


## Environment Variables and Config Keys

The installer/CLI manage a .env file, typically at /opt/ajib/.env. Some keys:

- TELEGRAM_BOT_TOKEN (required at install)
- TELEGRAM_ADMIN_IDS (comma-separated numeric admin IDs; the installer sets the primary)
- BLITZ_API_BASE (e.g., https://your-blitz-backend.example.com)
- BLITZ_API_KEY
- HELEKET_API_KEY
- HELEKET_NETWORK (e.g., mainnet, testnet)
- HELEKET_WEBHOOK_SECRET (optional if using webhooks)
- DATABASE_URL (optional; defaults to SQLite path if unset)
- CLIENT_DL_URL_ANDROID, CLIENT_DL_URL_IOS, CLIENT_DL_URL_WINDOWS, CLIENT_DL_URL_MACOS, CLIENT_DL_URL_LINUX
  - URLs returned to users when they choose their OS in the bot.

After changing config, run:
- ajib restart


## Payment Flow (Heleket)

Two supported patterns:
1) Invoice + Polling:
- Bot creates an invoice via Heleket API and returns a payment link to the user.
- Bot periodically checks the invoice/payment status until confirmed, then provisions service.

2) Webhook (optional):
- Expose a lightweight webhook endpoint (via a small HTTP server and reverse proxy).
- Set HELEKET_WEBHOOK_SECRET and configure Heleket to send notifications.
- On webhook confirmation, the bot marks payment as confirmed and provisions service.

Refer to Heleket docs: https://doc.heleket.com/


## Backend Integration (Blitz-Compatible)

- The bot uses a Blitz-compatible API client to:
  - Fetch and display active configs for the authenticated Telegram user
  - Create orders/purchases and obtain one-time or subscription configs
  - Fulfill 1 GB test config
  - Manage plans (admin side): add/edit pricing plans mapped to backend plan IDs

You can configure:
- BLITZ_API_BASE
- BLITZ_API_KEY

The bot maps Telegram user IDs to backend accounts (exact mapping depends on your backend’s user model and endpoints).


## Internationalization (English & Persian)

- Default language: English
- Users can switch to Persian (fa) via the bot menu
- Locale files are kept under bot/locales/
  - en.yml
  - fa.yml
- All user-facing strings should be loaded from locale files to keep text out of code and make translations simple.


## Backups and Restore

Two ways to backup:
- From the bot (admin menu): triggers a backup and sends the archive to admin or stores it under backups/
- From CLI:
  - ajib backup create
  - ajib backup list
  - ajib backup restore /path/to/backup.tar.gz

What is backed up:
- .env
- Local database (e.g., SQLite file)
- State files as needed (e.g., broadcast logs, cache as applicable)


## Broadcast (Admin)

- Admin can broadcast a message to:
  - Active users
  - Expired users
  - Test users
  - All users
- The bot provides a flow to select the audience and send confirmation before sending.
- Broadcast logs are stored locally to track failures and retries (implementation detail).


## Pricing Plans (Admin)

- Admin can add/edit plans from within the bot:
  - Define price, duration, data limits, and plan identifier
  - Map to Blitz backend plan IDs
- Plans are cached locally and synced with the backend on demand.


## Systemd Service

- Service name: ajib-bot
- Manage via CLI or systemctl directly:
  - sudo systemctl status ajib-bot
  - sudo systemctl restart ajib-bot
  - journalctl -u ajib-bot -f


## Uninstall

Use the CLI:
- ajib uninstall
  - Stops the service, removes files, and optionally keeps backups.
- To completely remove everything, including backups, confirm when prompted.


## Contributing

- Keep modules small and focused.
- All user-facing text must be in locale files.
- Prefer pure functions, explicit dependencies, and typed code where possible.
- Keep the install/update/uninstall UX rock-solid and explicit.


## License

This repository includes a LICENSE file. See it for details.


## Current Implementation Status

✅ **Completed:**
- Core bot framework with aiogram 3.x
- Configuration management (environment-based)
- Modular handler structure (customer & admin)
- Internationalization support (English & Persian)
- Database layer with SQLite (async via aiosqlite)
- User management and subscription tracking
- Blitz API client (async, minimal, extensible)
- Heleket payment client (async, webhook-ready)
- Reply keyboard-based UX for customers and admins
- CLI tool (`ajib`) for service management
- One-line installation script for Ubuntu
- Update and uninstall scripts
- Backup and restore functionality
- Systemd service integration
- Plan management (admin side)
- Broadcast functionality (admin side)

🚧 **In Progress / TODO:**
- Full integration testing with real Blitz backend
- Payment flow integration with Heleket
- Test config provisioning workflow
- Purchase flow with plan selection
- User status tracking and expiration logic
- Enhanced error handling and retry logic
- Webhook receiver for Heleket payments
- Comprehensive test suite
- CI/CD workflows

## Roadmap

- Complete payment flow integration with Heleket
- Add webhook receiver endpoint for payment notifications
- Implement config renewal and expiration tracking
- Add health checks and metrics command for admins
- Expand broadcast filters and segmentation
- Add unit/integration tests and CI workflows
- Add monitoring and alerting capabilities
- Multi-language support expansion


## Support

Open issues on GitHub or reach out via the support contact configured in the bot.
