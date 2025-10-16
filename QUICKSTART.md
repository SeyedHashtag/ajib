# AJIB Quick Start Guide

Get your VPN bot running in under 5 minutes! ⚡

## Prerequisites Checklist

- [ ] Ubuntu 22.04+ server with root/sudo access
- [ ] Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- [ ] Your Telegram User ID from [@userinfobot](https://t.me/userinfobot)

## Installation (Choose One)

### 🎯 Method 1: Interactive (Most Common)

```bash
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo bash
```

Enter your credentials when prompted. Done! ✅

### 🤖 Method 2: Automated (CI/CD)

```bash
export TELEGRAM_BOT_TOKEN="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
export TELEGRAM_ADMIN_ID="123456789"
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo -E bash
```

No prompts, fully automated! ✅

### 📥 Method 3: Download First

```bash
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh -o install.sh
sudo bash install.sh
```

Review before running! ✅

## Post-Install Configuration

```bash
# Configure backend API
ajib config set BLITZ_API_BASE https://your-backend.example.com
ajib config set BLITZ_API_KEY your_api_key

# Configure payments
ajib config set HELEKET_API_KEY your_heleket_key

# Restart to apply
ajib restart
```

## Essential Commands

```bash
ajib status          # Check if bot is running
ajib logs -f         # Watch live logs
ajib restart         # Restart the bot
ajib update          # Update to latest version
ajib backup create   # Create backup
```

## Verify Installation

```bash
# Check service status
ajib status

# Should show: "active (running)"

# Test bot in Telegram
# Send /start to your bot
```

## Common First Steps

### 1. Add Client Download Links

```bash
ajib config set CLIENT_DL_URL_ANDROID https://example.com/vpn-android.apk
ajib config set CLIENT_DL_URL_IOS https://apps.apple.com/app/your-vpn
ajib config set CLIENT_DL_URL_WINDOWS https://example.com/vpn-windows.exe
ajib restart
```

### 2. Set Support Contact

```bash
ajib config set SUPPORT_CONTACT @your_support_username
ajib restart
```

### 3. Add Multiple Admins

```bash
ajib config set TELEGRAM_ADMIN_IDS "123456789,987654321,555555555"
ajib restart
```

## Troubleshooting Quick Fixes

### Bot Not Responding?

```bash
ajib logs | tail -n 20  # Check recent logs
ajib restart            # Try restarting
```

### Installation Hangs?

```bash
# Use non-interactive mode instead:
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_ADMIN_ID="your_id"
curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/deploy/install.sh | sudo -E bash
```

### Wrong Credentials?

```bash
ajib config set TELEGRAM_BOT_TOKEN "correct_token"
ajib restart
```

## Daily Operations

### View Logs
```bash
ajib logs              # Last 200 lines
ajib logs -f           # Follow in real-time
```

### Backup
```bash
ajib backup create     # Creates timestamped backup
ajib backup list       # List all backups
```

### Update
```bash
ajib update            # Pull latest and restart
```

## Uninstall

```bash
ajib backup create            # Backup first!
ajib uninstall --yes --keep-backups  # Uninstall but keep backups
```

## File Locations

| What | Where |
|------|-------|
| Installation | `/opt/ajib/` |
| Config file | `/opt/ajib/.env` |
| Database | `/opt/ajib/ajib.sqlite3` |
| Backups | `/opt/ajib/backups/` |
| CLI tool | `/usr/local/bin/ajib` |
| Service | `systemd: ajib-bot` |
| Logs | `journalctl -u ajib-bot` |

## Next Steps

1. ✅ Bot is running
2. ⚙️ Configure backend API
3. 💳 Set up payment API
4. 🔗 Add client download links
5. 👥 Add more admins (optional)
6. 📊 Test customer flows
7. 🧰 Test admin features
8. 💾 Schedule regular backups

## Need Help?

- 📖 **Detailed Guide**: [SETUP.md](SETUP.md)
- 🐛 **Troubleshooting**: [SETUP.md#troubleshooting](SETUP.md#troubleshooting)
- 💬 **Issues**: [GitHub Issues](https://github.com/SeyedHashtag/ajib/issues)
- 🤝 **Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md)

## Useful One-Liners

```bash
# Complete health check
ajib status && ajib logs | tail -n 5

# Quick backup before changes
ajib backup create && echo "Backup done: $(ajib backup list | tail -n1)"

# View all config (redacted)
cat /opt/ajib/.env | grep -v TOKEN | grep -v KEY | grep -v SECRET

# Restart and watch logs
ajib restart && sleep 2 && ajib logs -f

# Update and backup
ajib backup create && ajib update
```

---

**That's it!** Your VPN bot is ready to serve customers. 🚀

For detailed configuration options, see [SETUP.md](SETUP.md).
