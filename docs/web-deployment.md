# VPS installation and recovery

## Current pilot and CLI setup

The public informational site is deployed at `https://utility.jibijij.top`.
Portal access is restricted to configured administrators and financial writes
remain disabled. This is an informational release and a read-only pilot, not
the Stage 4 customer release. On September 12 the operator confirmed that browser
sign-in and the Mini App pilot both worked. Purchase/renewal journeys and broader
customer/device coverage remain separate release gates.

On an ajib installation containing this implementation:

```sh
ajib web setup --domain utility.example.com --dry-run
ajib web setup --domain utility.example.com --yes
ajib web status
ajib web doctor
ajib web logs --lines 100
ajib backup
```

The interactive menu also offers website setup and diagnostics. Setup detects
one existing Docker-based Traefik installation with an HTTP-01 resolver and adds
only a hostname-specific Docker route. It preserves the existing proxy and
other containers. On this VPS the existing resolver `mytlschallenge` manages
the new Let's Encrypt certificate automatically; no Traefik restart was needed.

Without Traefik, `--proxy standalone` uses a dedicated Nginx container on ports
80/443 and Let's Encrypt HTTP-01 through Certbot, with a renewal timer. Occupied
ports fail closed. Docker is installed on a fresh supported Debian/Ubuntu host
if absent; neither Traefik nor a local VPN panel is required. Standalone setup
has automated configuration tests but still needs a separate fresh-VPS integration
drill before being treated as a verified installation path.

The API and worker use systemd and a dedicated virtual environment. The Nginx
container reaches the API through a private Unix socket instead of a TCP port;
this keeps the API private while allowing reuse of an existing Docker proxy.
No Docker socket, database, or credentials are mounted into the Nginx container.
Cloudflare visitor headers are accepted only through the validated proxy/edge
chain. API responses are not cached. Keep Cloudflare proxying enabled and verify
Full (strict) mode for this hostname without changing unrelated domain settings.
The Cloudflare account's encryption-mode setting has not been inspected.

Persistent locations are `/etc/ajib-web` (configuration), `/opt/ajib-web/app`
(public code/build), `/opt/ajib-web/venv`, and `/var/lib/ajib-state/ajib.db`.
The bot's systemd drop-in sets the shared database path and `UMask=0007`.
The API unit preserves `/run/ajib-web` across stops/restarts so the Nginx bind
mount continues to see its socket. An API-only restart was exercised on the VPS
and public health recovered without restarting Traefik or the website proxy.
The database, WAL, and SHM files must all be group-writable by `ajib-state`.
`ajib` automatically loads the installed database path for backup/restore and
rejects a conflicting `AJIB_DB_PATH`. After applying bot configuration changes,
refresh the copied public catalogs and adapter settings with:

```sh
ajib web sync-config --dry-run
ajib web sync-config --yes
ajib web doctor
```

The preview prints changed setting names, never credential values. Sync requires
the bot to be running and ready with its saved configuration. It preserves website
access settings, drops removed adapter settings, and restarts only previously
running API/worker services. The main bot, hosted workers, Nginx, Traefik, and other
services are not restarted. No database snapshot is restored. This command currently
supports only the read-only pilot; token rotation and enabled financial writes
require the later coordinated maintenance implementation.

Sync stores private configuration history under `/etc/ajib-web/history` and an
in-progress journal at `/etc/ajib-web/config-sync.json`. If interrupted or unhealthy,
it attempts to stop both web services, preserves the journal, and reports failure.
Check `ajib web status` and `ajib web logs`, correct the bot configuration if needed,
then run `ajib web sync-config --recover --yes`. Recovery retries from current bot
settings and restores the original service activity states. It never restores an
older database or silently resumes stale permissions. Setup/restart cannot bypass
the pending recovery record. Avoid concurrent bot configuration edits during sync;
detected edits fail the sync and require recovery. Old bot handlers do not invoke
this synchronization automatically.

The command was installed and exercised on this VPS on September 12. The initial
preflight caught stale saved configuration checksum metadata before any service
changes. A verified state/configuration backup preceded a checksum-only repair and
an ajib supervisor restart. Sync then refreshed the divergent plans and passed
private/public health and Nginx checks. CLI rollback files and the repair backup
are under `/opt/ajib-backups/config-sync-cli-20260912`; private synchronization
history is retained under `/etc/ajib-web/history`. Other VPS services remained up.

`ajib backup` creates the normal state ZIP plus a private web-configuration TAR.
The companion includes the web environment and service configuration; restore
it only as part of a coordinated CLI recovery. The pilot's initial code/config
and database snapshots are in `/opt/ajib-backups/pre-web-20260911`, with the
relocation snapshot under `/opt/ajib-backups/web-install-*`.
The post-deployment state ZIP was prepared/restored into an isolated root-only
directory on this VPS: integrity passed and 1,348 payment records, 71 resellers,
10 hosted-bot records and the web worker table were present. The count includes
normal bot activity after the initial snapshot. Both backup files were mode 0600.
This verifies backup readability and isolated restoration, not a live database
rollback or payment/panel reconciliation drill.

`ajib web stop` stops only the website, retaining all state. `ajib web restart`
restarts only its processes. The legacy bot-only `ajib upgrade` is deliberately
blocked on an installed website until a coordinated web/bot upgrader is delivered;
do not bypass it or downgrade the bot's web fulfillment-owner guard. Do not use
the old uninstall script on this pilot. A repeat setup pauses web processes while
refreshing their code and resets access to the administrator-only read-only pilot.

## Earlier manual templates

The implementation is not cleared for public customer access. Keep both gates
off until the remaining Stage 4 acceptance criteria in `web-roadmap.md` pass.
These steps prepare an isolated pilot; they do not authorize bypassing its gates.
The manual renderer below remains available for reviewing alternative layouts;
the installed CLI-managed pilot uses the deployment described above.

## Build and review

Use Python 3.12 and Node 22. Install `requirements-web.txt` in a dedicated virtual
environment. Run the existing bot tests in one process and `tests_web` in another
(the legacy tests replace Python modules with synthetic stubs). In `web`, run
`npm ci`, `npm run api:types`, `npm run build`, and `npm test`. Browser tests use
an isolated synthetic database and bot token, never server credentials.

Generate configuration using real deployment settings, for example:

```sh
python tools/render_web_deployment.py \
  --domain "$AJIB_DOMAIN" --checkout "$AJIB_CHECKOUT" \
  --python "$AJIB_WEB_PYTHON" --state-directory "$AJIB_STATE_DIRECTORY" \
  --env-file "$AJIB_WEB_ENV_FILE" --output ./deployment-review
```

This command only writes review files. Inspect the Nginx config, certificate
paths, units and environment example before installing them. Serve `web/dist`
through Nginx; [Vite's production deployment guidance](https://vite.dev/guide/static-deploy.html)
describes the production build. The development server must not serve production.
Configure the Mini App URL on the associated Telegram bot through BotFather.
Authentication implements Telegram's
[signed initData validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
Browser sign-in requires a private bot confirmation and the initiating browser
cookie. Do not log the initData, login tokens or cookies.

## Shared SQLite and service identity

Create the dedicated unprivileged `ajib-web` account and `ajib-state` group.
Use a dedicated local state directory outside the source tree, root-owned with
group `ajib-state` and mode `2770`. The database must have group `ajib-state` and
mode `0660`. All participating bot, hosted worker, API and web worker runtimes
must point `AJIB_DB_PATH` at this same file and set
`AJIB_DB_SHARED_GROUP=ajib-state`. SQLite WAL/SHM files inherit database access.
The optional group setting preserves group access when the root bot reconnects;
the legacy default remains directory `0700` and database `0600`. Do not configure
shared access on the source-code or secrets directory. Backups remain `0600`.

Stop all affected writers for the first relocation. Create and integrity-check a
SQLite backup with the existing CLI, then install the snapshot at the new path.
Preserve the original snapshot as a recovery artifact. Configure all runtimes
before any restart, then verify pending payment counts and identities against
the snapshot. Do not copy only an active database while its WAL is being written.
The backup and restore CLI now honors an exported `AJIB_DB_PATH`. Ensure it is
present in the operator shell as well as the services; an unconfigured shell
still uses the legacy location. Restore stops previously active API/worker
services and the main bot, restores the configured database, and restarts only
those services. Separately coordinate any independently launched hosted workers.

The API/worker environment file is outside the repository, root-owned and
group-readable (`root:ajib-state`, `0640`). `AJIB_ENV_FILE` lets the existing panel,
payment and exchange-rate adapters read this explicitly selected file. Populate
it through the CLI with the same applicable bot identity, admin/checker rules,
panel credentials, gateway settings, exchange rate, and growth-feature settings.
Synchronize these settings during every coordinated configuration change. Never
give the web identity write access to secrets, source code or systemd units.
Hosted bot tokens stay in the existing private state store and are not returned
by the API. Public catalog/support files must be readable by the service identity.

The generated units bind the API to loopback, run as `ajib-web`, and allow writes
only to the state directory. Keep root-only system operations in the CLI.
Validate the units with `systemd-analyze verify`, validate Nginx with `nginx -t`,
and verify database access as the actual service user before starting a pilot.
Do not use `chmod -R` on the application tree to solve permission errors.

## Pilot and observability

1. Start with `AJIB_WEB_PUBLIC_PORTAL=0`, `AJIB_WEB_WRITES_ENABLED=0`, and a small
   comma-separated `AJIB_WEB_PILOT_USERS` list. Set the HTTPS origin and main bot
   username. Every process must run compatible code containing the bot adapters.
2. Verify `/api/v1/health`, browser and real Telegram login, ownership, language,
   catalogs, live panel reads, and configuration privacy. The health endpoint
   is not evidence that financial integrations have passed.
3. Exercise the release-gate failure cases on staging using synthetic provider
   fixtures, including simultaneous bot/API/worker requests and restart after
   panel success. Complete a real Telegram/device pilot before expanding access.
4. Enable pilot writes only after the pending concurrency/recovery items pass.
   Environment changes require coordinated service restarts. Currently the write
   gate pauses worker fulfillment as well as customer mutations; durable work
   remains queued. Public informational pages do not require this gate.

Watch API error rates, SQLite slow-transaction warnings and lock failures,
pending/uncertain operations, oldest pending jobs, and notification retries.
Structured API logs omit request bodies and query strings. The notification
outbox retries delivery independently of fulfillment. Unknown panel outcomes
remain reserved for investigation; they are never blindly recreated. Trials also
retain their shared eligibility claim while the panel outcome is uncertain.
The recovery UI and full production monitoring remain release requirements.

## Upgrade, backup and rollback

Backups must include the full SQLite file, all web extension tables and receipt
BLOBs, and the existing required configuration/secrets using the approved CLI.
The extension is additive and leaves legacy schema version 6 intact. A local
automated snapshot test checks receipts, challenges, trials, outbox and audit
records. A synthetic Linux CLI drill also exercises backup/restore with an
external database path; it does not replace the coordinated VPS recovery drill.
Back up the separate web environment file through the CLI alongside the archive;
the legacy archive contains the bot environment, not this separate service file.

Disable new web writes before rollback. Snapshot the current database and retain
the audit log and accepted orders. Use a compatible code rollback or forward
repair that preserves the web fulfillment-owner guard in the bot. Removing that
guard while web orders remain pending could re-enable duplicate fulfillment.
Do not restore an older database just to roll back code: subsequent payments and
panel mutations must first be reconciled in a coordinated recovery procedure.
Installation, upgrades, backup restoration, service restarts and secret rotation
are CLI-only operations.
