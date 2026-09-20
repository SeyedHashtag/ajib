# Gated legacy website bootstrap

Use this only for the reviewed administrator-only legacy installation with no
managed baseline, no web-owned payments, and no web/account-operation obligations.
It is not an override for the coordinated upgrade compatibility checks.

Run the candidate coordinator from a retained checkout outside both live code
directories. Keep that checkout and its Python runtime available for interrupted
recovery. The exact main commit must have passing Python, shell and web CI and
must declare contract 2 with `customer_release_ready: false`.

Before maintenance, inspect `web_bootstrap.inventory(web_operator.load())` through
the retained coordinator and save the reviewed result as a private JSON file.
It records installed code hashes and configuration digests without secret values.
Review the server modifications against the candidate before using this file.

```sh
python core/cli.py web bootstrap-baseline --review-file /private/review.json --commit FULL_SHA --dry-run
python core/cli.py web bootstrap-baseline --review-file /private/review.json --commit FULL_SHA --yes
```

Builds, separate stable virtual environments and isolated migration rehearsal
finish before downtime. The bootstrap rechecks the reviewed inventory, records
the prior service states and every directory switch in the maintenance journal,
then stops only this application's API, worker and bot supervisor. It preserves
the existing edge route/image, shared database location and unrelated services.

The retained release's private directory contains an integrity-checked database
snapshot, reviewed inventory, runtime environment and verified runtime archive
(including uploads, hosted receipts, reminders and disclosure state). The journal
also identifies the private web-configuration archive. Legacy code trees remain
on disk. Existing payment IDs and panel state are never recreated or restored.

Bootstrap recovery is **forward only**. The old code does not implement contract 2
and is never labeled as a compatible rollback. An interruption leaves financial
runtimes stopped. From the retained candidate coordinator, run:

```sh
python core/cli.py web upgrade-status
python core/cli.py web recover-upgrade --yes
```

Recovery repeats only idempotent installation steps, preserves the current live
database, verifies candidate hashes and resumes the candidate. Unexpected file
states or configuration changes require reviewed forward repair. After health
passes, the actual installed candidate hashes and immutable commit become the
managed baseline. Future upgrades use the normal coordinator and compatible code
recovery; do not use the retained legacy trees as rollback releases.

The resulting policy is administrator-only, no pilot users, no new web writes and
no web obligations processing (the preflight required none). Existing bot activity
resumes only after candidate installation. Old sessions/challenges are invalidated;
stored language preferences remain. Complete the origin-scoped cache/branding
handoff and actual service/HTTPS checks separately, and record unresolved client
or provider checks. This procedure never enables a customer pilot.

Local acceptance: interruption at each directory rename, retry after copy,
migration/health/completion failure, retained post-backup payment records, private
receipt preservation, session invalidation and permanent exclusion of legacy
runtime restart are covered by `tests_web/test_bootstrap.py`.
