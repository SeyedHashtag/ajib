<div align="center">

# 🚀 AJIB

### Modular Telegram Bot for VPN Businesses

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![aiogram 3.x](https://img.shields.io/badge/aiogram-3.x-blue.svg)](https://docs.aiogram.dev/)

**Simple to deploy · Easy to manage · Built for scale**

[Features](#-features) • [Quick Start](#-quick-start) • [Documentation](#-documentation) • [CLI](#-the-ajib-cli)

</div>

---

## 📖 Overview

AJIB is a production-ready Telegram bot for VPN businesses, designed to serve **customers**, **resellers**, and **admins** through an intuitive interface. Built with modern Python async patterns and focusing on operational simplicity.

### Why AJIB?

- 🎯 **One-line installation** on Ubuntu - up and running in minutes
- 🛠️ **Powerful CLI** for all management tasks - no manual config editing
- 🌍 **Built-in i18n** - English and Persian out of the box
- 📦 **Minimal dependencies** - clean, maintainable codebase
- 🔄 **Easy updates** - `ajib update` and you're done
- 💾 **Smart backups** - automated backup and restore
- 🔐 **Secure by default** - systemd hardening and proper secret management

### Tech Stack

| Component | Technology |
|-----------|------------|
| **Bot Framework** | [aiogram 3.x](https://aiogram.dev) (async, type-safe) |
| **Backend API** | Blitz-compatible client |
| **Payments** | [Heleket](https://doc.heleket.com/) crypto payments |
| **Database** | SQLite with async support |
| **Deployment** | Systemd service + shell installer |
| **Languages** | English, Persian (extensible) |

---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### 👥 **Customer Features**

- ✅ View active VPN configurations
- 💳 Purchase plans with crypto payments
- 🎁 Request 1GB test config
- 📱 Get client downloads by platform
- 💬 Contact support
- 🌐 Switch language (EN/FA)

</td>
<td width="50%" valign="top">

### 👨‍💼 **Admin Features**

- 🧰 Backup & restore bot data
- 📣 Broadcast to user segments
- 💲 Manage pricing plans
- 📊 View user statistics (planned)
- ⚙️ Configure via Telegram
- 🔧 Full CLI access

</td>
</tr>
</table>

### 🖥️ **System/DevOps**

- 🐧 Ubuntu-optimized one-liner installation
- 🎮 Powerful `ajib` CLI for all operations
- 🔄 Auto-start via systemd
- 📝 Structured logging to journald
- 🔒 Security hardening out of the box

---

## 🚀 Quick Start

### Prerequisites

- Ubuntu 22.04+ (or similar Debian-based system)
- Root or sudo access
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID from [@userinfobot](https://t.me/userinfobot)

### Installation

**One command to rule them all:**

```bash
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

The installer will:
1. ✅ Install system dependencies (git, python3, python3-venv)
2. ✅ Create a dedicated service user
3. ✅ Set up Python virtual environment
4. ✅ Prompt for your Bot Token and Admin ID
5. ✅ Install the `ajib` CLI
6. ✅ Start the systemd service

**That's it!** Your bot is now running. 🎉

### Post-Install Configuration

Configure your backend and payment APIs:

```bash
# Blitz Backend API
ajib config set BLITZ_API_BASE https://your-backend.example.com
ajib config set BLITZ_API_KEY your_api_key_here

# Heleket Payments
ajib config set HELEKET_API_KEY your_heleket_key_here
ajib config set HELEKET_NETWORK mainnet

# Client Download Links (optional)
ajib config set CLIENT_DL_URL_ANDROID https://example.com/vpn-android.apk
ajib config set CLIENT_DL_URL_WINDOWS https://example.com/vpn-windows.exe

# Support Contact (optional)
ajib config set SUPPORT_CONTACT @your_support_bot

# Restart to apply changes
ajib restart
```

### Verify Installation

```bash
# Check service status
ajib status

# View live logs
ajib logs -f

# Test the CLI
ajib doctor
```

---

## 🎮 The AJIB CLI

Your one-stop tool for managing the bot:

### Service Management

```bash
ajib status              # Check service status
ajib start               # Start the service
ajib stop                # Stop the service
ajib restart             # Restart the service
ajib logs                # View logs
ajib logs -f             # Follow logs in real-time
```

### Configuration

```bash
ajib config path                         # Show .env file location
ajib config get TELEGRAM_BOT_TOKEN       # Get a config value
ajib config set KEY VALUE                # Set a config value
ajib config unset KEY                    # Remove a config value
```

### Backups

```bash
ajib backup create                       # Create a backup
ajib backup list                         # List all backups
ajib backup restore /path/to/backup.tar.gz  # Restore from backup
```

### Updates & Maintenance

```bash
ajib update                              # Update to latest version
ajib update --branch develop             # Update from specific branch
ajib doctor                              # System diagnostics
ajib uninstall                           # Uninstall (with prompts)
ajib uninstall --yes --keep-backups      # Non-interactive uninstall
```

---

## 📁 Project Structure

```
ajib/
├── ajib/                      # Main Python package
│   ├── bot/                   # Telegram bot code
│   │   ├── core/              # Core: config, database, i18n
│   │   ├── handlers/          # Message handlers (customer/admin)
│   │   ├── services/          # External API clients (Blitz/Heleket)
│   │   └── locales/           # Translations (en.yml, fa.yml)
│   ├── cli/                   # CLI tool implementation
│   └── deploy/                # Deployment scripts
│       ├── install.sh         # One-line installer
│       ├── update.sh          # Update script
│       ├── uninstall.sh       # Uninstall script
│       └── ajib-bot.service   # Systemd unit file
├── .env.example               # Configuration template
├── requirements.txt           # Python dependencies
├── pyproject.toml            # Package metadata
└── README.md                  # This file
```

---

## 🔧 Configuration

### Environment Variables

All configuration is stored in `/opt/ajib/.env`:

#### **Required**
- `TELEGRAM_BOT_TOKEN` - Your bot token from @BotFather
- `TELEGRAM_ADMIN_ID` or `TELEGRAM_ADMIN_IDS` - Admin user IDs (comma-separated)

#### **Backend API (Blitz)**
- `BLITZ_API_BASE` - Backend API URL
- `BLITZ_API_KEY` - API authentication key

#### **Payments (Heleket)**
- `HELEKET_API_KEY` - Heleket API key
- `HELEKET_NETWORK` - Network (mainnet/testnet)
- `HELEKET_WEBHOOK_SECRET` - Webhook verification secret

#### **Optional**
- `CLIENT_DL_URL_*` - Client download URLs by platform
- `SUPPORT_CONTACT` - Support contact (username/URL/email)
- `LOG_LEVEL` - Logging level (DEBUG/INFO/WARNING/ERROR)
- `LOCALE_DEFAULT` - Default language (en/fa)

See [.env.example](.env.example) for all options.

---

## 🌍 Internationalization

AJIB supports multiple languages out of the box:

- 🇬🇧 **English** (default)
- 🇮🇷 **Persian (فارسی)**

Users can switch languages from the bot menu. All text is stored in YAML locale files:
- `bot/locales/en.yml`
- `bot/locales/fa.yml`

### Adding More Languages

1. Copy `bot/locales/en.yml` to `bot/locales/{lang_code}.yml`
2. Translate all strings
3. Add language code to `SUPPORTED_LOCALES` in `bot/__init__.py`
4. Add language selection button in handlers

---

## 💾 Backup & Restore

### Automated Backups

Create backups manually or via cron:

```bash
# Manual backup
ajib backup create

# Schedule daily backups (crontab)
0 2 * * * /usr/local/bin/ajib backup create
```

Backups include:
- `.env` file (credentials)
- SQLite database
- Plans configuration

### Restore

```bash
ajib backup restore /opt/ajib/backups/ajib-backup-20250116-120000.tar.gz
```

---

## 🔄 Updates

Keep your bot up to date:

```bash
# Update to latest stable
ajib update

# Update from specific branch
ajib update --branch develop

# Update from custom fork
ajib update --repo https://github.com/your-fork/ajib.git --branch custom
```

The update process:
1. Pulls latest code
2. Updates Python dependencies
3. Restarts the service

**Zero downtime updates coming soon!**

---

## 🏗️ Architecture

### Backend Integration (Blitz)

AJIB integrates with [Blitz](https://github.com/ReturnFI/Blitz)-compatible backends:

- Fetch user configurations
- Create orders and purchases
- Provision test configs
- Manage subscription plans

### Payment Flow (Heleket)

Two payment modes:

**1. Invoice + Polling** (simple)
- Bot creates invoice via Heleket API
- Returns payment link to user
- Polls payment status until confirmed

**2. Webhook** (recommended)
- Set up webhook endpoint
- Heleket sends real-time notifications
- Instant payment confirmation

See [Heleket documentation](https://doc.heleket.com/) for details.

---

## 📊 Current Status

### ✅ **Completed** (v0.1.0)

- ✅ Core bot framework with aiogram 3.x
- ✅ Configuration management system
- ✅ SQLite database with async support
- ✅ Internationalization (EN/FA)
- ✅ Blitz & Heleket API clients
- ✅ Customer & admin handlers
- ✅ CLI tool with full management
- ✅ One-line installation
- ✅ Backup & restore functionality
- ✅ Systemd service integration

### 🚧 **In Progress**

- 🔄 Full Blitz backend integration
- 🔄 Complete payment flow with Heleket
- 🔄 Test config provisioning
- 🔄 Purchase workflow with plan selection
- 🔄 User status tracking & expiration

### 🎯 **Roadmap**

- Webhook receiver for payments
- Config renewal tracking
- Health checks & metrics
- Comprehensive test suite
- CI/CD pipeline
- Monitoring & alerting
- Reseller functionality

---

## 🛠️ Development

### Local Setup

```bash
# Clone repository
git clone https://github.com/SeyedHashtag/ajib.git
cd ajib

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# Edit .env with your credentials

# Run bot
python -m ajib.bot.core.bot
```

### Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## 📚 Documentation

- 📘 **[SETUP.md](SETUP.md)** - Detailed setup and configuration guide
- 📗 **[CONTRIBUTING.md](CONTRIBUTING.md)** - How to contribute
- 📙 **[PROJECT_STATUS.md](PROJECT_STATUS.md)** - Implementation status and roadmap

---

## 🐛 Troubleshooting

### Bot not responding?

```bash
# Check service status
ajib status

# View recent errors
ajib logs | tail -n 50

# Verify bot token
ajib config get TELEGRAM_BOT_TOKEN

# Test API connectivity
curl https://api.telegram.org/bot$(ajib config get TELEGRAM_BOT_TOKEN | tr -d "'")/getMe
```

### Database issues?

```bash
# Check database file
ls -lh /opt/ajib/ajib.sqlite3

# Restore from backup
ajib backup restore /path/to/backup.tar.gz
```

### Need to start fresh?

```bash
# Create backup first
ajib backup create

# Uninstall
ajib uninstall --yes --keep-backups

# Reinstall
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

More troubleshooting in [SETUP.md](SETUP.md).

---

## 📜 License

This project is licensed under the MIT License - see [LICENSE](LICENSE) file for details.

---

## 🤝 Support

- 💬 **Issues**: [GitHub Issues](https://github.com/SeyedHashtag/ajib/issues)
- 📖 **Documentation**: [Wiki](https://github.com/SeyedHashtag/ajib/wiki) (coming soon)
- 🐛 **Bug Reports**: Open an issue with the `bug` label
- 💡 **Feature Requests**: Open an issue with the `enhancement` label

---

## 🙏 Acknowledgments

- [aiogram](https://aiogram.dev) - Modern Telegram Bot API framework
- [Blitz](https://github.com/ReturnFI/Blitz) - Inspiration for deployment workflow
- [Heleket](https://heleket.com) - Crypto payment processing

---

<div align="center">

**Built with ❤️ for the VPN community**

⭐ Star us on GitHub — it helps!

[Report Bug](https://github.com/SeyedHashtag/ajib/issues) · [Request Feature](https://github.com/SeyedHashtag/ajib/issues) · [Read the Docs](SETUP.md)

</div>
