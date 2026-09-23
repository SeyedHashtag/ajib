# Account-operation safeguards: local acceptance ledger

This is an implementation increment, not completion of the Stage 4 milestone.
No production deployment, database migration, pilot enrollment, payment, or
access-gate change has been performed for this increment.

## Current contract

The additive schema 6 operation-detail, account-claim, child-step and audit-event tables retain the
existing operation IDs and payment IDs. The lifecycle distinguishes preparation,
dispatch, verified panel success, financial/domain completion, and uncertainty.
The account claim is released only at completion. An expired process lease is
never proof that a panel request failed.

The release manifest requires fulfillment contract **2** and still declares
`customer_release_ready: false`. Contract 1 cannot safely roll back code that
depends on retained claims after panel verification. A separately reviewed,
coordinated baseline is required before adopting this contract on production.

## Coverage and remaining work

| Workflow | Local implementation | Acceptance / remaining work |
| --- | --- | --- |
| Main web creation | Durable allocation, operation identity, panel marker and retained credit | Lost response is reconciled to the same payment without another create |
| Main bot creation | Shared creation helper at paid creation call sites | Full cross-interface real-payment continuation remains a pilot gate |
| Main payment completion | Payment, incentives, account claim and web notification commit in one transaction | Injected outbox failure rolls all changes back |
| Reseller / hosted owner creation | Stable reservation-based allocation, no ambiguous fallback; claims retained through accounting | Recorded funding/settlement intent drives recovery; legacy records without sufficient provenance remain blocked |
| Split reseller funding | Shared finalizer completes funding-owned claims in the accounting transaction | CLI recovery requires persisted fulfillment data and matching funding terms |
| Immediate renewal | Shared durable intent and generation checks; a verified result can resume accounting | Complete browser choice/progress responses and real renewal acceptance remain |
| Reserved renewal | Stable payment/reservation operation; atomic history, notification and claim completion | Hosted obligations cannot be claimed by the main reseller scheduler; recovery preserves the original owner |
| Main browser, main bot and hosted trials | Shared global claim and stable allocation; uncertain claims never age out | Tests cover lost-response recovery and prevention of another trial |
| Legacy pending trial | Retained for investigation | No automatic retry or invented panel-success evidence |
| Trial replacement history | Old and new account claims, eligibility refreshed under claims, atomic history/outbox completion | Historical entry is the durable cleanup queue. Scheduler sends the notice and starts its grace period outside the trial transaction; cleanup conflicts and changed eligibility are tested |
| Admin create, edit, reset and delete | Account locks and durable command identity; fresh admin check in Telegram adapter | Tests cover conflict with customer fulfillment and uncertain edit retry |
| Admin rename | Restored after unused-destination and panel identity/entitlement verification; atomic reference updates and immutable history | Tests cover collisions, changed ownership, timeout, accounting failure, trial classification and cached references |
| Admin / reseller block changes | Existing desired-state journal and shared account locks | Audit complete mutation coverage and interruption behavior |
| Scheduled expired cleanup and manual cleanup review | Durable removal ownership, fresh generation/reference/eligibility checks, atomic metadata/outbox completion | Tests cover competing renewal, missing-response reconciliation and accounting failure; broader live policy acceptance remains |
| Reseller banned/debt deletion | Batch claims, persisted generation/debt intent, child deletions and atomic financial completion | Repayment after dispatch blocks disputed adjustments; partial or ambiguous batches retain claims for investigation |
| Account transfers / migrations and admin copy | Source/destination claims and journaled creation, entitlement updates, reference move and source deletion | No ambiguous automatic compensation; only a proven undispatched remaining step may resume through its original workflow |
| Main payment CLI recovery | Read-only preview, evidence digest and refreshed evidence under account locks | Changed quote, identity, panel state, ownership or revision rejects apply |
| Trial CLI recovery | Proven creation marker and plan can finish the original trial | Uncertain or missing evidence retains global eligibility claim |
| Funding CLI recovery | Matching funding terms plus recorded fulfillment required | No reconstruction of missing settlement data from account appearance |
| Hosted customer settlement CLI recovery | Shared settlement intent and finalizer preserve funding, margins, referrals, discounts and future reservations | Tests cover duplicate completion, changed terms, accounting/outbox failure and CLI recovery; legacy records missing intent remain blocked |
| Legacy operations without provenance | Inspectable, conservatively blocked | Reviewed provenance repair required; no force-complete option |
| Undispatched requeue | Supported for original web-owned main payment | Broader owner-specific requeue adapters remain |
| Cross-process / service-user locking | Synthetic Linux processes use three distinct UIDs and one shared group | Competing account write rejected, unrelated writes proceed, process death retains claim |
| Production recovery and pilot | Not performed | Reviewed baseline, deployed-revision checks, operator payments and 24-hour pilot required |

## Operator workflow after reviewed installation

Run these commands as root on the installation. They do not send panel mutations
as a reconciliation shortcut.

```sh
ajib operations list --status uncertain
ajib operations list --scope main --origin-id <payment-id>
ajib operations inspect <operation-id> --json
ajib operations reconcile <operation-id> --dry-run
ajib operations reconcile <operation-id> --evidence <digest> --reason '<review reason>' --yes
```

An inspection returns the classification, permitted action, safe panel snapshot,
and evidence digest. It omits configurations and raw panel notes. If evidence
changes before apply, inspect again. An apparently renewed account, elapsed time,
missing response or process restart does not prove a dispatched renewal succeeded.

Apply shares the coordinated maintenance lock and refuses an outstanding upgrade
or configuration-sync journal. It never switches a bot-owned payment to the web
worker. Unsupported or ambiguous outcomes remain under investigation with their
reservations retained. List/inspect and dry-run do not authorize completion.

## Local validation

Tests live in `tests_web/test_account_operations.py`,
`tests_web/test_operation_recovery.py`, `tests_web/test_operations_cli.py`,
`tests_web/test_admin_account_operations.py`, and
`tests_web/test_operation_service_identities.py`. Legacy and web suites run in
separate processes because legacy fixtures replace imported modules.

Validation on 2026-09-19: 1,189 legacy tests plus 369 subtests passed; all 216 Linux
web, deployment and recovery tests passed, including the service-identity drill.
This includes the replacement eligibility and cleanup concurrency regressions.
The frontend type/build and public artifact scan passed;
all 14 desktop/mobile browser journeys passed during this increment. API export
and generated types were regenerated with no changes. Bash syntax and ShellCheck passed
on tracked scripts. The Windows browser runner required stopping its verified
synthetic API/Vite processes to finish teardown; it then exited successfully.

The service-identity test uses temporary public Python source and a temporary
SQLite directory. It changes no OS account definitions or production service
configuration. Linux root is needed to launch the synthetic UIDs; ordinary CI
records this drill as skipped unless run with the necessary privileges.

Passing synthetic tests does not close the pending rows above and does not
authorize public customer access.

## Public-name privacy

Main-store titles, icon, login and service wording use neutral localized content.
Hosted labels retain reseller branding after centralized validation. Cookies and
browser storage use neutral identifiers; stored server language preferences remain.

The shared guard covers normalized/encoded text, Telegram payloads and markup,
attachments, QR input (including encoded VMess), API responses/headers and built
client artifacts. Errors omit raw exceptions and validation-input echoes. Portal
operational summaries expose safe status fields rather than private logs/audit
details. Internal implementation names remain in private source, CLI and state.

`test_public_branding.py` exercises four languages, roles, cookies, OpenAPI, error
paths, unsafe configured labels, attachment/QR content and operational privacy.
`tools/check_public_assets.py` rejects private names and source maps after the
production build and runs in CI. Browser journeys check rendered neutral titles.

Functional URLs and configurations are rejected when unsafe, never rewritten.
Actual deployed bot names, configured links and customer configurations have not
been certified by synthetic tests. Embedded internal names require a compatible,
reviewed migration before the affected flow can pass its production gate. See
[the branding deployment handoff](web-customer-release.md#branding-deployment-handoff)
for session and cache compatibility requirements.

Additional workflow evidence is in `test_account_rename.py`,
`test_cleanup_operations.py`, `test_reseller_removal.py`,
`test_migration_operations.py`, `test_hosted_settlement.py` and `test_trials.py`,
all under `tests_web/`. These tests do not attest to live financial settlement or
complete every Stage 4 journey.

Reserved-renewal review cannot replace the saved account baseline while an
unresolved operation owns the obligation or account. Main-store, hosted payment
and reseller review paths retain the original reservation and direct the operator
to reconciliation. Unclaimed reviews retain their existing behavior.
`tests_web/test_renewal_review_claims.py` verifies this across durable operation
phases without dispatching panel mutations.
