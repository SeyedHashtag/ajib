# Coordinated upgrade and customer release candidate

This increment is a local candidate, not an approved Stage 4 release. The release
contract deliberately sets `customer_release_ready` to `false`. Production remains
on the administrator-only, read-only website installation until the remaining
acceptance work is completed. Operator-selected pilot IDs belong in private
deployment settings and are not stored in this document.

## Implemented controls

- `ajib upgrade` detects a website installation and uses the shared coordinator.
  Bot-only installations retain the legacy path. The coordinator supports
  `--channel`, `--version`, `--yes`, and `--dry-run`.
- Targets resolve to immutable commits with successful Python, shell, and web CI
  checks. A compatibility manifest and installed file hashes reject incompatible
  releases and local code drift. An existing maintenance journal blocks another
  upgrade.
- Bot and web Python environments are built at retained stable paths; frontend
  assets are built before service downtime. Additive migrations are rehearsed on
  an isolated database snapshot.
- A private journal records service activity, configuration, access gates, and
  directory switches. New web requests pause before bounded draining. Only ajib's
  API, worker, supervisor, and Nginx container are managed; the live database stays
  in place. Backups and old releases are retained.
- Recovery restores compatible code and configuration without restoring an old
  database. Failed recovery health checks stop ajib financial runtimes and leave
  web gates paused. Legacy restore and uninstall scripts refuse web installations.
- `ajib web sync-config` supports live release controls: pause admission, drain,
  pause existing web processing, synchronize, verify health, then restore prior
  gates and service activity. Interrupted recovery preserves the original gates
  in its journal and leaves processing paused. Ordinary synchronization and code
  upgrades reject credential rotation, including existing panel credentials.
- Admission and existing-obligation processing have separate SQLite controls.
  API authorization reads the current policy on each request. Public access applies
  to the main store; hosted-commerce restrictions remain.
- Pilot promotion requires the installed revision's acceptance records, at least
  two explicit non-admin customers, at least 24 hours of pilot operation, and
  worker/notification/operation health. Real-payment evidence must reference a
  positive completed payment for a selected customer on that revision. A reserved
  renewal must actually be applied and cannot satisfy the immediate-renewal check.

## Shared financial safeguards

The main bot, web worker, reseller renewal handler, and reserved-renewal scheduler
now use durable account-operation identities for renewal calls. Linux account
locks serialize these paths with reseller/admin block changes. Mutation intent is
committed before panel I/O, without retaining a SQLite write transaction. An
interrupted operation remains unresolved; elapsed time never permits a blind
reset retry. A recorded successful renewal can resume financial completion without
another reset. Web account creation also records its operation before panel I/O.

Hosted payment recovery retains credit and referral reservations while a panel
operation needs investigation. Such payments cannot return to ordinary approval
or rejection after a process interruption. These guards are not yet integrated
with every account mutation path; the outstanding work below remains a release
blocker.

Web crypto checkout persists its merchant order ID and merchant identity before
invoice creation. Reconciliation checks invoice identity, currency, expected
amount, and the provider's actual USD payment amount. Uncertain/partial outcomes
retain reservations. Provider outages are rate limited and cannot starve the
rest of the invoice queue. Panel uncertainty is never turned into approval by a
later payment poll. Provider behavior is documented in the official
[invoice creation](https://doc.heleket.com/methods/payments/creating-invoice) and
[payment lookup](https://doc.heleket.com/methods/payments/payment-information) contracts.

## Operator interface after installing the reviewed baseline

```sh
ajib upgrade --channel stable --dry-run
ajib upgrade --channel stable --yes
ajib web upgrade-status
ajib web recover-upgrade --yes
ajib web sync-config --dry-run
ajib web sync-config --yes
ajib web sync-config --recover --yes
ajib web release status
ajib web release set admin --yes
ajib web release set pause --yes
ajib web release set pause --halt-worker --yes
```

`release set pilot --users <comma-separated-ids> --yes` and `release set public
--yes` enforce the gates above. `release record-check --name <check> --passed
--evidence-file <private-json-file>` records operator evidence, including a note and
payment IDs where required. It does not perform real payments or automatically
attest that a live journey passed.

`ajib web adopt-baseline --yes` records reviewed installed files only when both
installed code trees declare the same supported contract. It does not install or
verify an old deployment's missing safeguards and cannot open the pilot itself.
The first production baseline still needs a coordinated installation and review
of the intentional deployment changes.

## Outstanding release blockers

1. Complete durable ownership coverage for account creation in legacy bot paths,
   cleanup/deletion, migrations, and other conflicting mutations. Exercise bot,
   API, and worker concurrency under their actual Linux service identities.
2. Implement operator inspection and evidence-based reconciliation of uncertain
   panel outcomes; retain reservations when success cannot be established.
3. Finish the Stage 4 acceptance ledger: legacy-order continuation, checker receipt
   routing, account ownership/history edge cases, trial recovery and cleanup,
   referral/credit/withdrawal journeys, and four-language statuses/notifications.
4. Add server-calculated renewal choices, payment availability and safe progress
   responses, generated frontend types, and their browser journeys.
5. Install and exercise the baseline/candidate on the VPS with general access and
   new customer writes disabled. Complete real service interruption, upgrade,
   recovery, and isolated backup-restoration drills against the deployed revision.
6. Verify the selected pilot identities, collect operator-performed real payment
   evidence, complete the 24-hour pilot, then consider main-store promotion.

Synthetic tests cannot close production, real payment, or real Telegram-client
acceptance gates. Do not set `customer_release_ready` merely because unit tests pass.

## Local validation for this increment

- Python 3.12 bot suite: 1,189 tests and 369 subtests passed in a separate process.
- API/deployment/shared-state suite: 123 tests passed, including interrupted
  upgrades, rollback retaining newer payment records, live configuration sync,
  process death during account mutation, and hosted reservation retention.
- Tracked shell scripts passed Bash syntax and ShellCheck checks. OpenAPI and
  generated frontend types remained consistent; the TypeScript/production build passed.
- All 14 desktop/mobile browser journeys passed. Windows server teardown required
  stopping the two verified synthetic test-server processes; the test runner then
  exited successfully. These journeys do not attest to real payment fulfillment.
