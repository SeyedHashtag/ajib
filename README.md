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

- Ubuntu 22.04+ or Debian 12+
- Python 3.10 or newer
- Root access for installation and the systemd service
- A Telegram bot token from BotFather and at least one numeric Telegram administrator user ID
- At least one compatible VPN panel API URL and authorization token

ajib manages accounts on existing VPN panels; it does not install a VPN
server. Blitz and 3x-ui panels are supported. Every 3x-ui server needs one or
more default Hysteria2 inbound IDs. Find the numeric IDs in the 3x-ui inbound
list before setup.

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SeyedHashtag/ajib/main/install.sh)
```

The installer selects the latest published GitHub release, creates `/etc/ajib`,
installs a private Python environment, and installs the real root command at
`/usr/local/sbin/ajib`. A stable install never falls back to development code.
To deliberately install the development branch, download the installer and run
it with `--channel main`. An exact release can be selected with `--version TAG`.

On a terminal, installation opens secure setup. Tokens are hidden while typed.
Run `ajib` later for the menu, or use `ajib --help` for automation. The menu is
only available on a TTY; Ctrl-D cleanly closes it.

## Secure setup

Interactive setup:

```bash
sudo ajib setup
```

For automation, use a root-readable mode-600 JSON file or stdin. Avoid shell
arguments and environment variables for secrets.

```json
{
  "schema_version": 1,
  "telegram": {
    "token": "BOTFATHER_TOKEN",
    "admin_ids": [123456789]
  },
  "servers": [{
    "id": "primary",
    "url": "https://panel.example.com",
    "token": "PANEL_API_TOKEN",
    "panel": "blitz",
    "weight": 1,
    "enabled": true,
    "default_inbound_ids": [],
    "default_limit_ip": 0
  }]
}
```

```bash
sudo chmod 600 /root/ajib-config.json
sudo ajib setup --config /root/ajib-config.json --yes
# or
sudo ajib setup --config - --yes < /root/ajib-config.json
```

Setup validates local data, shows Added/Changed/Removed changes with masked
credentials, and performs read-only Telegram and VPN checks. Network failures
are advisory. Interactive setup asks whether to continue; automation must add
`--allow-unverified`. Exit status is `0` when ready, `2` when applied but
unverified/degraded, and `1` on failure or rollback.

The canonical configuration is `/etc/ajib/core/scripts/telegrambot/.env`. It is
written atomically under a lock with mode `0600`; unrelated values are
preserved. The prior copy is `/etc/ajib/core/scripts/telegrambot/.env.previous`.
Do not paste `.env`, BotFather tokens, panel tokens, payment credentials, or
backup archives into issue reports.

## VPN server lifecycle

```bash
sudo ajib server list --live
sudo ajib server show primary --live
sudo ajib server test --json
sudo ajib server add edge
sudo ajib server edit edge
sudo ajib server weight edge 0 --yes
sudo ajib server disable edge --yes
sudo ajib server manage
```

Server IDs are permanent database identities. Rotate a URL or API token with
`server edit`, but do not rename an ID. To replace an identity, add the new
server, migrate accounts and records, then remove the old server.

`enabled=false` is an administrative disable. `weight=0` leaves the server
enabled and reachable but pauses new automatic placement. `list`, `show`, and
`test` display masked tokens, placement/readiness state, latency, panel health,
and live account counts without exposing credentials.

For 3x-ui, configure `panel` as `3x-ui` and include positive
`default_inbound_ids`. `default_limit_ip` is `0` for unlimited or a positive
client IP limit. Panel-type changes on referenced servers require account
migration or the explicit edit `--force` escape hatch.

## Migration and safe removal

The existing durable bulk-transfer engine powers CLI copy and migration jobs:

```bash
sudo ajib server migrate old new --mode copy
sudo ajib server migrate old new --mode migrate --notify deferred
sudo ajib server migration status --json
sudo ajib server migration export --output /root/migration.csv
sudo ajib server migration cancel JOB_ID
sudo ajib server migration resume JOB_ID
```

A non-interactive migration requires `--notify send|deferred|disabled`; copy
mode always disables notifications. Destination collisions and incompatible
panels are detected before a job is queued. Migrations verify destination
accounts, rehome direct-customer, reseller, renewal, test, cleanup, and payment
records, verify source deletion, and can resume after partial failures.

`ajib server remove ID` first checks the live panel, all local references, and
active transfer jobs, then creates a safety backup. It refuses an unavailable
panel because emptiness cannot be proven. It also refuses live accounts,
references, active migrations, and the final configured server.

Break-glass removal is intentionally difficult: disable the server, set its
weight to zero, ensure no migration is active, then use `server remove ID
--force`. A safety backup must succeed, and interactive use requires typing the
exact ID (`--yes` is required for automation). The final server can never be
force-removed.

## Operations and recovery

```bash
sudo ajib status
sudo ajib status --json
sudo ajib doctor
sudo ajib doctor --json
sudo ajib logs --lines 200
sudo ajib restart
sudo ajib stop
sudo ajib backup
sudo ajib restore                 # latest backup, with confirmation
sudo ajib restore /path/file.zip  # selected backup
sudo ajib rollback-config
sudo ajib version --check
```

Readiness is tied to the exact stored configuration and is only recorded after
SQLite bootstrap and Telegram authentication. A filesystem or systemd failure
during setup automatically restores the prior configuration. If the service
starts but never becomes ready, interactive setup offers to restore the last
working copy by default. On a first install, the entered configuration is kept
for repair and the unready service is stopped.

Backups are private versioned ZIP archives in `/opt/ajib-backups`. Restore
validates an archive and creates a rollback backup before changing state.
`rollback-config` only restores `.env.previous`; it does not roll back database
state.

## Upgrade

```bash
sudo ajib upgrade
# deliberate development upgrade
sudo ajib upgrade --channel main
```

Stable upgrades use the latest published GitHub release, require confirmation
unless `--yes` is supplied, create and validate a safety backup, and roll the
installation back if the switch fails. Stable upgrades do not fall back to
`main`. Upgrades preserve `.env`, `.env.previous`, static configuration, hosted
receipt assets, and SQLite state, and remove only the exact legacy Bash alias.

Legacy commands and the old secret-bearing `telegram` flags remain available
with deprecation warnings for pre-2.3 automation. New scripts should use
`setup`, `server`, `restart`, and `stop`; ajib itself never forwards secrets in
subprocess arguments.

## Runtime layout

- `core/scripts/telegrambot/tbot.py`: bot entry point
- `core/scripts/telegrambot/supervisor.py`: primary and hosted reseller bot supervisor
- `core/scripts/telegrambot/hosted_worker.py`: isolated reseller storefront worker
- `core/scripts/telegrambot/ajib.db`: private SQLite runtime state
- `core/scripts/telegrambot/migrate_state.py`: idempotent legacy-state importer
- `core/scripts/telegrambot/utils/`: Telegram handlers and external API client
- `core/scripts/telegrambot/.env`: bot credentials and VPN server definitions
- `core/ajib_operator.py`: validation, locked atomic configuration, preflight, readiness, and removal safety
- `core/cli.py`: secure setup, server lifecycle, diagnostics, recovery, and compatibility commands
- `menu.sh`: small TTY-only menu that delegates to the Python CLI

Mutable state is stored in `ajib.db`; `plans.json` and `support_info.json`
remain static configuration. Do not commit `.env`, the database, API tokens,
payment details, receipts, logs, or generated backup archives.

## Hosted reseller bots

An approved reseller can open **Reseller Panel → My Hosted Bot** and submit a
BotFather token. The token is validated and stored separately from public bot
metadata. The systemd supervisor starts one isolated polling worker per active
reseller bot (up to 50 per installation). Resellers configure the customer
price increase over catalog prices, visible wholesale plans, card details,
optional operator crypto checkout, support text, referrals, and earnings inside
their own bot's owner panel.

The main bot provides localized connection status and a direct link into the
hosted bot's owner setup. Inside the hosted bot, an advisory checklist guides
owners through pricing, payments, and visible plans, while grouped menus keep
store setup, customer operations, and payouts separate. Existing storefronts
infer setup progress from their saved configuration and continue selling while
owners review any remaining steps.

The localized **Pricing & Profit** view breaks down every visible plan into its
catalog price, the owner's level-discounted cost, the card and crypto customer
prices, gross profit, referral payout, and the amount the owner keeps. Card
revenue reaches the owner's payment card while wholesale cost becomes reseller
debt. Operator-collected crypto sales credit gross profit to hosted-bot
earnings, and referral payouts remain a separate liability.

Wholesale costs follow the reseller's six-level loyalty discount (20% at
Level 1 through 25% at Level 6). Higher levels increase reseller margin while
the catalog price, configured customer price increase, and resulting customer
price remain stable.

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
