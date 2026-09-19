# Stage 4: local customer acceptance and deployment handoff

## Candidate boundary

This increment implements main-store customer choices, payment progress and
cross-interface continuation. It does not deploy, enroll pilot users, perform real
payments or open access. The manifest retains `customer_release_ready: false` and
fulfillment contract 2. Hosted commerce and reseller/admin dashboard completion
remain later stages.

## Public interfaces

- `GET /api/v1/accounts/{server_id}/{username}/renewal-options` authorizes the
  account and returns plans with immediate/reserved availability, safe reasons and
  an existing reservation reference. It uses the shared account-state and protected
  block rules. Viewing choices performs no allocation, invoice creation or funding
  reservation. Submission rechecks current ownership, plan, cycle, claims and choice.
- `GET /api/v1/payment-methods` uses the authenticated stored language, storefront,
  configured payment services and live write policy. Card transfer remains Persian
  only. Unavailable methods include neutral reason codes, never configuration data.
- Existing payment responses retain identifiers and fields and add `progress`:
  a public code, permitted actions and whether polling should continue. Completed
  payment settlement does not end polling while reserved renewal activation is
  pending. Claimed or uncertain obligations cannot expose a customer replay action.
- Account, payment-method, renewal-option and payment-progress responses have
  explicit OpenAPI models and generated frontend types. API version remains v1.

The frontend consumes those choices, displays four-language status/reason wording,
supports renewal plan selection, pauses polling in hidden pages and refreshes on
return. Payment details also provide manual refresh and support navigation.

## Continuation and compatibility

New SQLite-mode bot card checkouts persist the original quote, checker assignment,
benefit references and bot fulfillment owner before displaying payment instructions.
Receipt submission and cancellation share one transaction service across bot and
API. Browser/Mini App continuation preserves the original payment identity and
owner; it never inserts a replacement web fulfillment job for a bot obligation.

Telegram customers use `/payments` (or `/payments <payment-id>` for a particular
pending order). Receipt images are validated/re-encoded into private SQLite storage.
Duplicate submission returns the committed receipt rather than replacing reviewer
evidence. Telegram's existing confirmations menu reads that image directly from the
database and uses the existing approval callbacks; HTTP availability is unnecessary.
Bot and web notification delivery share leased outbox records. The main bot can
deliver main-store notices while the website worker is unavailable; it cannot claim
a hosted bot's notices. Delivery retries do not repeat accounting.

Persisted crypto invoices continue through their existing URL/identity and original
fulfillment owner. Legacy receipts without enough quote/owner provenance show a
support action and retain reservations. Old bot prompts existing only in process
memory have no durable payment to resume in the browser; they must be reviewed
during the coordinated baseline handoff, not converted into invented replacement
orders. Legacy JSON-mode bot behavior is preserved.

Web write pause blocks new web actions and Telegram mutations of web-owned
checkouts. It does not silently disable the existing bot's independent business
workflows. Processing existing web obligations remains controlled separately by
the recovery/worker policy. No new panel mutation is initiated by a progress read.

## Local acceptance matrix

All paths below are repository-relative. Each row is a local synthetic acceptance
gate; real clients, provider settlement and the deployed service topology are
separate gates in the next section.

| Customer workflow | Required evidence | Tests |
| --- | --- | --- |
| Login, role and tenant boundaries | Expired/replayed/forged authentication, CSRF and revoked access fail; preferences persist | `tests_web/test_security.py`, original browser journeys |
| Catalog, methods and checkout | Server availability matches stored language/configuration/pause; quotes and credit rules remain shared | `test_customer_acceptance.py`, `test_orders.py`, card/crypto browser journeys |
| Owned accounts, usage and history | Retired identities do not grant access; migrated/renamed trial and cached references retain ownership | `test_account_rename.py`, `test_migration_operations.py`, original account/configuration journeys |
| Configurations, QR, guidance | Only verified owner can retrieve private configuration; unsafe content is rejected; four-language guides remain available | `test_security.py`, `test_public_branding.py`, original browser journeys |
| Card continuation and review | Bot-to-browser and browser-to-shared-bot transitions preserve ID/owner; image is privately available to the bot; duplicate receipt/review has one result | `test_customer_acceptance.py`, legacy payment/checker suite |
| Rejection and cancellation | Credit released once; submitted/claimed obligations cannot cancel; concurrent receipt/cancel commits one transition | `test_customer_acceptance.py`, `test_orders.py` |
| Crypto continuation and verification | Invoice identity, merchant/currency/amount and lost-response reconciliation retain the original order; partial/uncertain results retain funds | `test_gateway.py`, crypto browser journey, legacy provider tests |
| Payment history/progress | Neutral localized states and permitted actions; reserved activation remains visible after settlement; hidden-page polling stops and resumes | `test_customer_acceptance.py`, customer browser journeys |
| Immediate renewal | Shared eligibility and protected blocks, fresh submission checks, one reset/accounting result despite conflicting interfaces or changed generation | `test_customer_acceptance.py`, `test_account_operations.py`, `test_operation_recovery.py` |
| Reserved renewal | Original scheduler ownership, atomic completion/history/outbox and visible pending activation | `test_hosted_settlement.py`, `test_account_operations.py`, reserved-progress browser journey |
| Trials, waitlists, replacement and activation | One shared claim, queued state, connected marker, protected old/new trials and retained uncertain outcome | `test_trials.py`, `test_operation_recovery.py`, trial-queue browser journey |
| Cleanup and conflicting mutations | Cleanup cannot remove a claimed/renewing trial; changed generation/debt/ownership blocks disputed completion | `test_cleanup_operations.py`, `test_reseller_removal.py`, `test_migration_operations.py` |
| Referrals, rewards and credits | Attribution, immutable reservations, full/partial credit and reward finalization agree with bot accounting | `test_rewards.py`, `test_orders.py`, `test_hosted_settlement.py`, legacy incentive tests |
| Wallets and withdrawals | Shared pending liability, one request under retries/concurrency, private history and unchanged payout administration | `test_rewards.py`, wallet/withdrawal browser journey |
| Notifications, recovery and privacy | Outbox failure rolls back domain changes; retries do not dispatch again; recipient languages and neutral public assets | `test_customer_acceptance.py`, `test_operation_recovery.py`, `test_public_branding.py` |
| Cross-process operation | Separate Linux bot/API/worker UIDs share claims and preserve them after process death | `test_operation_service_identities.py`, `test_account_operations.py` |

Test filenames without a directory above are under `tests_web/`. Browser tests are
`web/tests/journeys.spec.ts` (restricted access) and
`web/tests/customer-journeys.spec.ts` (synthetic customer writes enabled). The latter
uses `SYNTHETIC_CUSTOMER_WRITES=1` only with the loopback fixture, fake payment
provider and disposable database; it is not a production release switch. CI runs
both browser configurations and runs legacy/web Python suites separately.

## Validation recorded September 20, 2026

The local working-tree candidate passed the following checks. These results are
not a production release approval; capture the eventual immutable commit when
preparing deployment.

| Check | Result | Local evidence |
| --- | --- | --- |
| Legacy Python suite, separate process | 1,189 tests and 369 subtests passed | `.web-local/stage4-legacy-verified.log` |
| Web Python suite on Linux | 239 passed, including separate bot/API/worker service UIDs | `.web-local/stage4-web-complete.log` |
| Customer-write browser journeys | 14 passed across desktop/mobile Chromium | `.web-local/stage4-customer-browser-final.log` |
| Restricted-access browser journeys | 14 passed across desktop/mobile Chromium, including simulated Mini App navigation | `.web-local/stage4-restricted-browser-final.log` |
| OpenAPI and generated TypeScript consistency | 38 paths; regeneration leaves both artifacts unchanged | `docs/web-openapi.json`, `web/src/api-schema.d.ts` |
| TypeScript and production build | Passed | `npm --prefix web run build` |
| Public production artifact scan | Passed | `python tools/check_public_assets.py` |
| Shell syntax and ShellCheck | Passed; ShellCheck uses CI's `-x` flag | Eleven repository shell scripts |
| Patch whitespace | Passed | `git diff --check` |

The ignored `.web-local/` logs are local evidence, not distributed release
artifacts. CI runs the reproducible tests above. Windows Playwright required
explicit teardown of its synthetic API/Vite child processes after assertions
completed; both browser runs returned success. No production processes were used.
The Python run reported two dependency deprecation warnings, with no skipped or
failed web tests. `customer_release_ready` remains false.

## External acceptance and handoff — pending

1. Review the exact candidate commit and production pending orders; verify the
   compatible contract-2 baseline, private-name compatibility and session/cache
   transition in `web-customer-release.md`. Preserve payments and reservations.
2. Deploy with general access/new web writes disabled. Exercise coordinated service
   interruption, hosted-worker recovery, code rollback retaining subsequent
   payments, and backup restoration into an isolated location. Check actual service
   identities and public HTTPS without changing unrelated VPS services.
3. Verify browser and real Telegram-client journeys in all four languages, including
   configuration usability, receipt review, immediate renewal, reserved activation,
   trials, applicable rewards and withdrawals against database/panel evidence.
4. Verify the two operator-selected pilot identities privately, record operator-
   performed card/crypto payment evidence against the deployed commit, and observe
   at least 24 hours. Duplicate fulfillment, lost updates, unexplained payments,
   ownership failures or unresolved database locks block promotion.
5. Only after all release evidence passes, use the release CLI to promote main-store
   customer access. Keep hosted-commerce restrictions and recovery artifacts.

Local tests do not attest to any of these external gates.
