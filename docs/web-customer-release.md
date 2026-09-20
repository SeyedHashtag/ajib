# Coordinated upgrade and customer release candidate

This is not an approved Stage 4 release. The release contract deliberately keeps
`customer_release_ready` set to `false`. Deployment status, operator confirmations,
pilot identities and acceptance evidence belong in private operator records, not
in this repository. Keep customer writes disabled until required gates pass.

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

For the unmanaged read-only installation, use the separately tested
[gated bootstrap procedure](web-bootstrap.md). It retains the old files without
claiming they support contract 2 and uses forward recovery until the actual
candidate is installed and verified.

## Outstanding release blockers

1. Finish end-to-end acceptance across cleanup/debt policy changes, migrations,
   ownership-preserving renames and settlement. Shared claims and recovery adapters
   are now implemented for these workflows; the coverage and remaining acceptance
   decisions are recorded in the
   [account-operation ledger](account-operation-acceptance.md).
2. Exercise the implemented hosted-settlement and scheduled-renewal recovery
   adapters in the deployment drill. Legacy records with incomplete provenance
   remain conservatively blocked and require reviewed forward repair. Synthetic
   concurrency passes with three distinct Linux UIDs; actual production identities
   and deployment still require a coordinated drill.
3. Complete the external evidence in the [Stage 4 customer acceptance matrix](stage4-customer-acceptance.md):
   live legacy-order continuation, checker receipt routing, account history, trial
   recovery/cleanup, rewards/withdrawals and four-language real-client journeys.
   Their local synthetic acceptance now has a per-workflow test mapping.
4. Verify server-calculated renewal choices, payment availability, safe progress
   and typed customer interfaces against the deployed candidate. Local implementation
   and browser checks are included in the Stage 4 matrix.
5. Install and exercise the baseline/candidate on the VPS with general access and
   new customer writes disabled. Complete real service interruption, upgrade,
   recovery, and isolated backup-restoration drills against the deployed revision.
6. Verify the selected pilot identities, collect operator-performed real payment
   evidence, complete the 24-hour pilot, then consider main-store promotion.

Synthetic tests cannot close production, real payment, or real Telegram-client
acceptance gates. Do not set `customer_release_ready` merely because unit tests pass.

## Local validation for this increment

The subsequent account-operation increment passed 1,189 legacy tests plus 369
subtests and 216 Linux web/deployment/recovery tests on 2026-09-19, including the
trial-replacement regression and synthetic service-identity concurrency drill.
The production frontend build, public artifact scan and all 14 browser journeys
also passed during this increment. See the [operation ledger](account-operation-acceptance.md) for its
coverage and remaining blockers. The results below describe the earlier
coordinator increment.

- Python 3.12 bot suite: 1,189 tests and 369 subtests passed in a separate process.
- API/deployment/shared-state suite: 123 tests passed, including interrupted
  upgrades, rollback retaining newer payment records, live configuration sync,
  process death during account mutation, and hosted reservation retention.
- Tracked shell scripts passed Bash syntax and ShellCheck checks. OpenAPI and
  generated frontend types remained consistent; the TypeScript/production build passed.
- All 14 desktop/mobile browser journeys passed. Windows server teardown required
  stopping the two verified synthetic test-server processes; the test runner then
  exited successfully. These journeys do not attest to real payment fulfillment.

## Branding deployment handoff

This handoff is pending; the local candidate does not change production sessions,
cache state or access gates. Include it in coordinated maintenance with customer
writes paused and compatible recovery artifacts retained.

1. Inventory the deployed bot display names/usernames, public configuration labels,
   links, download names and connection payloads privately. The new guards reject
   embedded private branding instead of changing functional URLs or credentials.
   Any affected flow requires a compatible migration and verification before release.
2. Revoke existing web sessions and login challenges during the switch. Clear old
   cookie identifiers using server-generated expiry headers scoped to this origin;
   issue only neutral session/login cookies afterward. Users reauthenticate through
   Telegram. Preserve database language preferences and restore them on sign-in.
3. Clear obsolete local storage and cached client content through a reviewed,
   origin-scoped transition. Do not embed old private identifiers in the new public
   JavaScript to migrate them. An origin-wide storage reset requires checking that
   the origin hosts only this application; otherwise provide a scoped transition.
   Anonymous local-only language preferences may reset to browser language; stored
   authenticated preferences must remain intact.
4. Publish the production build, purge this application's Cloudflare HTML/assets,
   stop serving superseded asset files, and verify old asset URLs and source maps
   are unavailable. Browser caches already downloaded cannot be remotely erased
   with certainty; verify fresh loads and supported reload paths. Preserve private
   rollback builds outside the public asset directory.
5. Verify public headers, rendered pages, Telegram messages, receipt/configuration
   access, QR codes and error paths for all four languages and roles against the
   deployed commit. Limit proxy/container changes to this application; unrelated
   VPS services remain outside this maintenance operation.

Keep `customer_release_ready: false` until the full release evidence is approved.
Real Telegram clients, operator-performed card/crypto payments and the 24-hour
pilot are separate acceptance gates, not consequences of a passing local build.
