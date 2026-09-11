# VPS installation and recovery

The implementation is not cleared for public customer access. Keep both gates
off until the remaining Stage 4 acceptance criteria in `web-roadmap.md` pass.
These steps prepare an isolated pilot; they do not authorize bypassing its gates.
No production services or database have been changed by this implementation.

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
