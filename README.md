<div align="center">

[![Latest Release](https://img.shields.io/badge/Release-Latest-brightgreen?logo=github)](https://github.com/SeyedHashtag/ajib/releases)
[![License](https://img.shields.io/badge/License-GPLv3-blueviolet?logo=gnu)](LICENSE)

</div>

# ajib Telegram VPN Bot

ajib is a Telegram sales and administration bot for external VPN servers. It
does not install or run a VPN server. The bot connects to one or more deployed
VPN panels through their HTTP APIs.

## Features

- Create, edit, reset, renew, and delete VPN users through external APIs
- Balance new accounts across multiple VPN servers
- Sell plans through manual and cryptocurrency payment flows
- Manage resellers, referrals, test accounts, and expired accounts
- Show customers their configurations and QR codes
- Broadcast messages and provide operational dashboards to administrators
- Back up bot configuration and JSON state

## Requirements

- Ubuntu 22+ or Debian 11+
- Root access for installation and the systemd service
- A Telegram bot token and at least one administrator user ID
- At least one compatible VPN panel API URL and authorization token

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/install.sh)
```

The installer creates `/etc/ajib`, installs the Python environment, and opens
the bot configuration menu. Run `ajib` later to reopen that menu.

## Upgrade

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/upgrade.sh)
```

Upgrades preserve `.env` and JSON state files in
`/etc/ajib/core/scripts/telegrambot` and restart the bot only if it was already
running.

## Runtime layout

- `core/scripts/telegrambot/tbot.py`: bot entry point
- `core/scripts/telegrambot/utils/`: Telegram handlers and external API client
- `core/scripts/telegrambot/.env`: bot credentials and VPN server definitions
- `core/cli.py`: bot lifecycle, backup, version, and dashboard commands
- `menu.sh`: interactive bot configuration and service manager

Do not commit `.env`, API tokens, payment details, or generated JSON state.

## API compatibility

The endpoints consumed by the bot are documented in [docs/api.md](docs/api.md).

## Testing

```bash
python -m pytest -q tests
```

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
