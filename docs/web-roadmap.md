# Website and Mini App delivery ledger

This file tracks implementation evidence. A route or screen alone does not
establish feature parity. The web interfaces must retain the bot's ownership,
pricing, accounting, and recovery rules before the corresponding row can close.

## Fixed architecture

The same VPS runs the existing bot, FastAPI transport, durable web worker, and
Nginx-served React application. SQLite remains authoritative. Telegram identity
is required. Storefronts use `/s/{slug}`. Persian, English, Russian, and Turkmen
share one frontend. The production write and public-portal gates default off.

Installation, upgrades, restore, service restarts, and secret rotation stay in
the CLI. A deployment must retain these capabilities for the existing bot.

## Inventory

`web-entrypoints.json` records every statically decorated Telegram and CLI
entrypoint, including source location and dispatch expression. Regenerate it
with `python tools/inventory_web_features.py`. The initial baseline contains
221 entrypoints; the current generated inventory contains 222. Scheduled work and dynamic next-step flows are included in
the workflow families below, rather than counted as separate menu buttons.

| Workflow family | Actor and ownership | Rules and dependencies | Acceptance criterion | Stage / current evidence |
| --- | --- | --- | --- | --- |
| Browser/Mini App login | Telegram user, selected storefront | Correct bot token, fresh signed data or browser-bound approval | Expiry, replay, wrong browser/bot and revoked access rejected | 2–3: implemented; automated API tests |
| Portal/role navigation | Current server-resolved role | Role selector cannot grant rights | Direct URL/API access respects current permissions | 3: implemented; desktop/mobile role switching and permission-denial tests pass |
| Language and onboarding | User within storefront | Existing stored preference; fa RTL, en/ru/tk LTR | Preference persists and layouts work in all languages | 3: implemented |
| Public catalog | Visitor, selected storefront | Existing plan targets, duration, pricing and availability | Only eligible plans appear, no wholesale/private fields leak | 3: implemented |
| Downloads, guidance, FAQ, support | Visitor/customer, storefront | Existing client-specific guidance and support configuration | Client links and localized guidance cover bot journeys | 3–4: shared client catalog and four-language instructions implemented; real-device verification pending |
| Accounts and usage | Customer-owned account; reseller-owned account through reseller portal | Recorded ownership, legacy names, live identity uniqueness, cycle history | Other accounts never visible; stale/duplicate identity blocks configuration actions | 2–4: customer service implemented; additional migration cases pending |
| Configurations and QR | Customer owner | Panel URI adapter; no public caching | Browser and Mini App return the owner's usable configuration | 4: implemented; desktop/mobile configuration journeys pass |
| Main-store checkout | Customer, main scope | Plan eligibility; immutable quote; account-credit and referral reservations | Duplicate/concurrent requests reserve only once | 2–4: card/crypto checkout implemented; provider integration pilot pending |
| Manual payment and receipt | Customer; authorized admin/checker | Private validated image; receipt routing and checker share | Only permitted reviewers can approve; one fulfillment | 4/6: web review and Telegram review adapter implemented; checker parity tests pending |
| Cryptocurrency payment | Customer, selected storefront | Existing signed payment provider; status and amount verification | Retries cannot create duplicate fulfillment; partial/uncertain outcomes retain reservations | 4: main-store worker implemented; live provider validation pending |
| Payment history/cancellation | Payment owner | Scope and status restrictions; release unused benefits atomically | Cancellation cannot refund a paid/uncertain operation | 4: implemented for unsubmitted web card checkouts |
| Immediate renewal | Existing account owner | Existing eligibility, plan snapshots, protected blocks, live cycle checks | Concurrent bot/web renewal performs one reset and one charge | 4: adapter implemented; concurrency/locking review outstanding |
| Reserved renewal | Existing account owner | Existing reservation scheduler, activation state, review and recovery | Fund once, apply on eligibility, surface attention without repeated reset | 4: adapter implemented; complete regression scenarios outstanding |
| Test accounts and waitlist | Customer, existing eligibility scope | Disable switch, one-use claim, replacement policy, shared queue | Bot/web requests cannot issue two tests or bypass restrictions | 4: shared eligibility, waitlist and worker implemented; concurrency/uncertain-outcome tests pass; replacement/recovery pilot pending |
| Trial activation and follow-up | Customer/test account | Connected marker, help/download paths, existing notifications | Current trial journey is visible and actionable in either interface | 4: connection confirmation and client guidance implemented; notification/cleanup parity pilot pending |
| Referrals and attribution | User within storefront | First attribution, discounts, cap and liability rules | Link attribution and rewards agree with bot | 4/7: main referral code/attribution, summary and checkout reservations implemented; hosted actions pending |
| Referral wallets and withdrawals | Reward owner; authorized payout administrator | Existing minimum, pending-request deduplication, payout audit | Reserve/settle once; no cross-storefront balance access | 4/6: main wallet and withdrawal requests implemented with shared bot/web reservation test; payout administration pending |
| Main account credits | Main customer | Existing credit ledger and idempotent reservation/finalization | Same credit cannot fund two simultaneous orders | 4: balance/checkout integration implemented; full-credit pricing and single consumption tests pass |
| Reseller application/recruitment | Applicant, referring user, admin | Approval and recruitment reward rules | Application and earned milestones retained across interfaces | 5–6: earned recruitment reward claims implemented; reseller applications/administration pending |
| Reseller search and customer history | Approved reseller, own configs | Ownership, removal flags, existing account lifecycle | Cannot search/access another reseller's customers | 5: list implemented; detailed lifecycle pending |
| Reseller create/edit/block/delete | Approved reseller, own configs | Shared funding, protected blocks, external panel identity | Existing authorized actions work with audited failures | 5: pending |
| Reseller immediate/reserved renewal | Approved reseller, own configs | Prepaid-first reservations, account locking and recovery | One panel action and one ledger effect across all runtimes | 5: pending |
| Reseller funding and debt settlement | Reseller; permitted payment reviewer | Top-ups, prepaid-first spending, FIFO debt, overpayment rules | Old and new pending orders preserve their funding path | 5: summary implemented; payment actions pending |
| Credit restrictions and recovery | Reseller; admin | Existing two $5 recovery cycles, level limits, block policy | Web displays and enforces the same computed policy | 5–6: summary implemented; administration pending |
| Reseller sales/statistics/history | Reseller, own business | Existing reporting timestamps and filters | Totals match bot, without exposing unrelated transactions | 5: pending |
| Hosted bot registration/settings | Approved reseller, own bot | Unique token/bot identity, runtime setup, no secret disclosure | Owner can configure supported settings without receiving stored secrets | 5/7: storefront title/slug implemented; bot setup pending |
| Hosted catalog and storefront | Visitor/customer, hosted scope | Existing markup, enabled plans, support and branding privacy | Suspension disables access; all customer operations stay within scope | 7: public routing/catalog/auth implemented; commerce parity pending |
| Hosted sales, renewals and payment follow-ups | Hosted customer; owner reviewer | Wholesale funding, retail margin, scoped incentives, owner follow-up | Same retail sale and wholesale effect across web/Mini App/bot | 7: pending; hosted web checkout explicitly unavailable |
| Hosted earnings, referrals and withdrawals | Reseller/customer in hosted scope | Earnings reserves, referral liabilities, minimum withdrawal | Correct owner receives and settles each liability once | 5/7: pending |
| Customer/reseller administration | Admin | Existing bans, permissions, debt restoration and reason/audit requirements | Every existing business operation has equivalent controls | 6: pending |
| Plans and support administration | Admin/authorized storefront owner | Catalog validity, recommendations, safe updates | Changes preserve references and concurrent edits | 6: pending |
| Receipt checker reporting/settlement | Configured reviewer; admin | Existing share calculation, reviewed amounts, payout history | Web totals and settlements match existing reporting | 6: payment-review adapter only |
| Referral and hosted payouts | Admin | Existing liability reservation and settlement functions | Payout cannot be settled twice | 6: pending |
| VPN panels and placement | Admin | Stable server IDs, eligibility, weights; secrets stay CLI-only | Safe nonsecret configuration and live status match CLI | 6: pending |
| Copy and bulk migration | Admin | Existing durable engine, preview, collision checks, resume/cancel | Recovery does not duplicate/delete wrong accounts | 6: pending |
| Broadcasts | Admin | Explicit recipient preview, reachability, retries, cancellation | Only confirmed audience receives approved message | 6: pending |
| Expired cleanup/test cleanup | Admin/scheduler | Existing hold, renewal and ownership protection | No active/reserved/protected account is removed | 6: pending |
| Business/growth reports | Admin | Existing event deduplication, lifecycle dates, scope | Totals match bot reports | 6: pending |
| Worker health and attention | Admin/operator | Pending payments, notifications and uncertain operations | Failed work is visible and recoverable | 2/6: operation list, trial attention, heartbeat, outbox retries and audit implemented; recovery actions pending |
| Telegram notifications | Correct bot and recipient | Durable outbox, isolated failure and bounded retry | Delivery failure never repeats purchase/renewal | 2–7: worker implemented; full event parity pending |
| Backups/migrations/restore | CLI operator | All runtimes coordinated, SQLite backup API, safety snapshot | Restore includes extension tables/receipt BLOBs and is exercised | 2/8: shared database CLI and isolated VPS restore verified; coordinated live recovery pending |
| Staging/pilot/rollback | Operator, selected pilot users | Opt-in gates, compatible revisions, pending operation preservation | Full journeys pass before expanding access | 8: informational deployment live; operator confirmed browser/Mini App sign-in pilot; full customer release pending |

## Verification ledger

- Initial Windows/Python 3.14 baseline: 901 passed, 38 failed, 247 errors,
  1 skipped, 369 subtests passed. Sandbox/temp-path and OS differences affect this run.
- Unmodified source exported to an isolated Ubuntu/Python 3.12 baseline:
  1,176 passed, 10 failed, 369 subtests passed. Failures involve shell scripts
  with CRLF line endings (backup/restore and runbot); they predate web edits.
- Changed-source full Linux bot suite: **1,186 passed, 369 subtests passed**.
  The original CRLF failures were resolved by preserving LF for shell scripts;
  backup/restore also now supports a configured external database path.
- Subsequent targeted checks for the atomic payment-owner guard and shared
  SQLite state: **26 passed**. Shared download/pricing regressions:
  **75 passed, 53 subtests passed**.
- New API tests run independently from the legacy module stubs. Latest Linux run: **29 passed**.
  Windows: **27 passed, 2 POSIX tests skipped**.
- Synthetic Linux backup/restore plus existing backup/operator checks:
  **46 passed**, including restoration from the configured database location.
- React/TypeScript production build passed; the OpenAPI export now contains
  **36 paths** and generates frontend API types.
- Latest browser run: **14 passed** across desktop and mobile, including all
  languages, Persian RTL, account/configuration access, permission denial,
  mobile role switching/logout, paused write controls, Telegram dark theme and
  safe areas, downloads, and administrator operation visibility.
- The operator confirmed real browser/Mini App sign-in on September 12. Full
  gateway/panel journeys and coordinated live VPS recovery remain unverified.
  Deployment details and evidence are in `web-deployment.md`.

### September 11 informational deployment

- `utility.jibijij.top` now serves public pages through the existing Traefik and
  Cloudflare proxy, with a verified Let's Encrypt origin certificate. Access to
  the portal remains administrator-only; writes are disabled.
- The VPS was upgraded by the operator from 2 GB to 4 GB RAM. Existing n8n,
  Traefik, and VPN services were preserved and their post-deployment status checked.
- Added `ajib web setup/status/doctor/logs/stop/restart/renew-certificate`, menu
  entries, database-path propagation to the operator CLI, and private companion
  configuration backups. The legacy bot-only upgrader is blocked for web installs.
- Bot, hosted workers, API, and paused worker all use the same relocated database.
  Integrity passed; 1,347 payments, 71 resellers, and 10 hosted-bot records were
  preserved at deployment. No web fulfillment operations were created.
- Deployment caught a root-created SQLite SHM permission mismatch. Shared
  permissions now cover the database, WAL, and SHM; an actual separate-identity
  Linux regression test covers unprivileged writes while a root connection is open.
- Full bot suite rerun: 1,186 tests and 369 subtests passed. The expanded API and
  deployment suite passed 45 tests, and 49 targeted CLI/backup tests passed. The
  production build passed, and all 14 desktop/mobile browser assertions passed
  (the Windows test-server teardown did not exit promptly). Live checks covered
  TLS, unauthorized access, CSRF, socket survival across an API restart, and an
  isolated restore of the real backup without replacing the live database.
  Full customer journeys and live recovery release gates
  remain open; public informational availability does not close Stage 4.

### September 12 operator pilot and configuration maintenance

- The operator reports that browser Telegram sign-in and the Mini App pilot both
  worked. This closes the initial administrator sign-in check, not customer purchase
  or renewal acceptance. Public portal and financial writes remain disabled.
- Added `ajib web sync-config --dry-run/--yes/--recover` for the read-only pilot,
  preserving website access settings while copying current bot configuration and
  public catalogs. A private journal supports interrupted synchronization; recovery
  leaves the live database authoritative and preserves prior service activity.
- Installed and exercised the command on the VPS. Its first attempt refused before
  changing services because the bot's saved configuration checksum was stale.
  A verified database/configuration backup preceded a checksum-only repair and an
  ajib supervisor restart; readiness then matched the saved configuration. The
  command synchronized the changed catalog and restarted only the API/worker.
  Local/public health and Nginx configuration passed; unrelated services stayed up.
- Validation: 61 API/deployment tests and 55 existing CLI/backup tests passed
  (plus 3 subtests). The subsequent readiness-check regression passed with all
  17 configuration-sync tests. Fixtures exercise partial installation/start,
  failed health checks, recovery with newer payments, and previously stopped services.
- Coordinated code upgrades and secret rotation remain pending. The legacy bot-only
  upgrade block is still required and must not be bypassed.

## Release rule

Do not enable the public customer release on the basis of the initial screens
or API tests. Stage 4 includes trials, referrals/withdrawals and verified renewal
recovery, as well as purchases. Keep public access and writes disabled until its
required rows and the cross-interface regression gates are complete.
