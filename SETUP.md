# AJIB Setup Guide

This guide walks you through setting up AJIB on Ubuntu 22.04+ for production use.

## Prerequisites

- Ubuntu 22.04 or later
- Root or sudo access
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (use [@userinfobot](https://t.me/userinfobot))
- A Blitz-compatible backend API (optional, can be configured post-install)
- Heleket API credentials (optional, can be configured post-install)

## Quick Install

Run the one-line installer as root or with sudo:

```bash
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

The installer will:
1. Install system dependencies (git, python3, python3-venv)
2. Create a service user (`ajib`)
3. Clone the repository to `/opt/ajib/app`
4. Set up a Python virtual environment at `/opt/ajib/venv`
5. Prompt for your Telegram Bot Token and Admin ID
6. Install the `ajib` CLI to `/usr/local/bin/ajib`
7. Create and start the systemd service (`ajib-bot`)

## Post-Install Configuration

After installation, configure your backend and payment credentials:

```bash
# Blitz API configuration
ajib config set BLITZ_API_BASE https://your-blitz-backend.example.com
ajib config set BLITZ_API_KEY your_blitz_api_key_here

# Heleket payment configuration
ajib config set HELEKET_API_BASE https://api.heleket.com
ajib config set HELEKET_API_KEY your_heleket_api_key_here
ajib config set HELEKET_NETWORK mainnet

# Optional: Client download URLs
ajib config set CLIENT_DL_URL_ANDROID https://example.com/vpn-android.apk
ajib config set CLIENT_DL_URL_IOS https://apps.apple.com/app/your-vpn
ajib config set CLIENT_DL_URL_WINDOWS https://example.com/vpn-windows.exe
ajib config set CLIENT_DL_URL_MACOS https://example.com/vpn-macos.dmg
ajib config set CLIENT_DL_URL_LINUX https://example.com/vpn-linux.deb

# Optional: Support contact
ajib config set SUPPORT_CONTACT @your_support_username

# Restart after changes
ajib restart
```

## Managing the Service

### Status and Logs

```bash
# Check service status
ajib status

# View logs (last 200 lines)
ajib logs

# Follow logs in real-time
ajib logs -f
```

### Start/Stop/Restart

```bash
ajib start
ajib stop
ajib restart
```

### Enable/Disable Auto-start

```bash
# Enable auto-start on boot
ajib enable

# Disable auto-start
ajib disable
```

## Configuration Management

### View Configuration

```bash
# Show path to .env file
ajib config path

# Get a specific value
ajib config get TELEGRAM_BOT_TOKEN
ajib config get BLITZ_API_BASE
```

### Set Configuration

```bash
# Set a value
ajib config set KEY VALUE

# Examples:
ajib config set LOG_LEVEL DEBUG
ajib config set LOCALE_DEFAULT fa
```

### Remove Configuration

```bash
# Remove a key
ajib config unset KEY
```

## Backup and Restore

### Create Backup

```bash
ajib backup create
```

This creates a tarball containing:
- `.env` file (with credentials)
- SQLite database
- Plans configuration

Backups are saved to `/opt/ajib/backups/`

### List Backups

```bash
ajib backup list
```

### Restore from Backup

```bash
ajib backup restore /path/to/backup.tar.gz
```

The restore process will:
1. Extract and restore `.env`, database, and plans
2. Restart the service

## Updating

### Update to Latest Version

```bash
ajib update
```

This will:
1. Pull the latest code from the main branch
2. Update Python dependencies
3. Restart the service

### Update from Specific Branch

```bash
ajib update --branch develop
```

### Update from Different Repository

```bash
ajib update --repo https://github.com/your-fork/ajib.git --branch custom-branch
```

## Uninstalling

### Basic Uninstall

```bash
ajib uninstall
```

This will prompt for confirmation and then:
1. Stop and disable the service
2. Remove the systemd unit
3. Delete `/opt/ajib`
4. Remove the CLI

### Uninstall with Options

```bash
# Non-interactive (no prompt)
ajib uninstall --yes

# Keep backups
ajib uninstall --yes --keep-backups
```

## Troubleshooting

### Bot Not Responding

1. Check service status:
   ```bash
   ajib status
   ```

2. View recent logs:
   ```bash
   ajib logs
   ```

3. Verify bot token:
   ```bash
   ajib config get TELEGRAM_BOT_TOKEN
   ```

4. Test connectivity:
   ```bash
   curl -s https://api.telegram.org/bot$(ajib config get TELEGRAM_BOT_TOKEN | tr -d "'")/getMe
   ```

### Database Issues

1. Check if database file exists:
   ```bash
   ls -lh /opt/ajib/ajib.sqlite3
   ```

2. View database-related errors in logs:
   ```bash
   ajib logs | grep -i "database\|sqlite"
   ```

### Permission Issues

Ensure the service user has proper permissions:

```bash
sudo chown -R ajib:ajib /opt/ajib
sudo chmod 600 /opt/ajib/.env
```

### Restart from Scratch

If things go wrong, you can uninstall and reinstall:

```bash
# Backup first!
ajib backup create

# Uninstall (keeping backups)
ajib uninstall --yes --keep-backups

# Reinstall
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash

# Restore backup
ajib backup restore /opt/ajib/backups/ajib-backup-YYYYMMDD-HHMMSS.tar.gz
```

## Advanced Configuration

### Custom Install Directory

Set environment variables before running the installer:

```bash
export AJIB_INSTALL_DIR=/custom/path
export AJIB_REPO_URL=https://github.com/your-fork/ajib.git
export AJIB_REPO_BRANCH=custom-branch

curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

### Non-Interactive Installation

For CI/CD or automation:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
export TELEGRAM_ADMIN_ID="123456789"

curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

### Multiple Admin Users

Add multiple admin IDs (comma-separated):

```bash
ajib config set TELEGRAM_ADMIN_IDS "123456789,987654321,555555555"
```

### Custom Database Location

```bash
ajib config set DATABASE_URL "sqlite:////custom/path/ajib.db"
ajib restart
```

### Logging Configuration

```bash
# Set log level (DEBUG, INFO, WARNING, ERROR)
ajib config set LOG_LEVEL DEBUG

# Enable JSON logging
ajib config set LOG_JSON true

ajib restart
```

## Security Best Practices

1. **Protect the .env file**: It contains sensitive credentials
   ```bash
   sudo chmod 600 /opt/ajib/.env
   ```

2. **Regular backups**: Schedule periodic backups
   ```bash
   # Add to crontab
   0 2 * * * /usr/local/bin/ajib backup create
   ```

3. **Keep updated**: Regularly update to get security fixes
   ```bash
   ajib update
   ```

4. **Monitor logs**: Watch for suspicious activity
   ```bash
   ajib logs -f
   ```

5. **Use strong API keys**: Ensure your backend and payment API keys are strong and rotated periodically

## Getting Help

- GitHub Issues: https://github.com/SeyedHashtag/ajib/issues
- Check logs: `ajib logs -f`
- Doctor command: `ajib doctor` (shows system info and paths)
