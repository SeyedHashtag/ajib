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
- Host customer-facing Telegram bots for approved resellers on the same VPN infrastructure
- Show customers their configurations and QR codes
- Broadcast messages and provide operational dashboards to administrators
- Persist transactional bot state in SQLite and create versioned backups

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

Upgrades preserve `.env`, static configuration, hosted receipt assets, and the
SQLite state database in `/etc/ajib/core/scripts/telegrambot`. Existing JSON
installations are migrated automatically before the service restarts.

## Runtime layout

- `core/scripts/telegrambot/tbot.py`: bot entry point
- `core/scripts/telegrambot/supervisor.py`: primary and hosted reseller bot supervisor
- `core/scripts/telegrambot/hosted_worker.py`: isolated reseller storefront worker
- `core/scripts/telegrambot/ajib.db`: private SQLite runtime state
- `core/scripts/telegrambot/migrate_state.py`: idempotent legacy-state importer
- `core/scripts/telegrambot/utils/`: Telegram handlers and external API client
- `core/scripts/telegrambot/.env`: bot credentials and VPN server definitions
- `core/cli.py`: bot lifecycle, backup, version, and dashboard commands
- `menu.sh`: interactive bot configuration and service manager

Mutable state is stored in `ajib.db`; `plans.json` and `support_info.json`
remain static configuration. Do not commit `.env`, the database, API tokens,
payment details, receipts, logs, or generated backup archives.

## Hosted reseller bots

An approved reseller can open **Reseller Panel → My Hosted Bot** and submit a
BotFather token. The token is validated and stored separately from public bot
metadata. The systemd supervisor starts one isolated polling worker per active
reseller bot (up to 50 per installation). Resellers configure their markup
over catalog prices, visible wholesale plans, card details, optional operator crypto checkout,
support text, referrals, and earnings inside their own bot's owner panel.

Wholesale costs follow the reseller's six-level loyalty discount (20% at
Level 1 through 25% at Level 6). Higher levels increase reseller margin while
the customer-facing catalog price and configured markup remain stable.

Hosted bots use the operator's VPN servers and never expose panel credentials.
New hosted bots start with crypto disabled. The existing 5% crypto discount is
deducted from reseller margin when crypto is enabled.

## API compatibility

The endpoints consumed by the bot are documented in [docs/api.md](docs/api.md).

## Testing

```bash
python -m pytest -q tests
```

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
