# Reseller funding and credit recovery

Wholesale purchases spend available prepaid money first and borrow only the
remainder. For example, a $5 order with $2 prepaid uses $2 prepaid and $3 credit.
Pending orders reserve both portions; outstanding reservations reduce the
amount available to other orders in either bot.

Recovery requires two **$5 prepaid spending cycles at every level**, or $10
total. The first cycle leaves the existing restriction in place. The second
restores the full credit limit for the reseller's current level. Orders can
span cycle boundaries; order count is irrelevant. Mixed orders contribute only
their prepaid portion. Directly funded hosted wholesale orders also count.
Top-ups, transfers, and debt settlements do not count. New late/default events
reset recovery progress, and extra spending is not saved against future penalties.

Restoring credit does not forgive debt, change settlement deadlines, or remove
independent administrator restrictions. Admin **Restore full credit** requires
a reason and records the actor, time, action ID, and before/after state. The
confirmation supports either notifying the reseller or making a silent change.

## Upgrade

1. Back up the database and stop the main bot and hosted workers together.
2. Install the same revision for all runtimes, retaining managed SQLite storage
   (`AJIB_SQLITE_ACTIVE=1`). New split orders require transactional storage.
3. Start the runtimes. Schema version 6 adds `reseller_order_funding`; startup
   persists recovery state reconstructed from verified historical prepaid
   spending after the latest adverse event. Existing full-credit accounts stay
   restored. Historical amounts are never inferred from outcome counts.
4. Confirm a reseller profile shows settlement terms above statistics and a
   $5 purchase preview correctly divides the prepaid and credit portions.

Existing pending orders retain their saved legacy funding path. Do not run
older workers alongside this revision or downgrade them against schema 6.

## Recovery and operations

Funding reservations and completed panel work are durable. Reconciliation
retries saved fulfillment accounting without repeating the panel operation.
It releases abandoned, unstarted reservations after 24 hours. Work whose panel
outcome is uncertain remains reserved for investigation; it is not silently
charged or refunded. Admin reseller details show pending-order counts.

Watch `ajib.reseller_funding` for `funding_recovery_failed` events. Identify a
pending operation by its reseller and operation IDs in `reseller_order_funding`.
Verify the panel outcome before resolving uncertain work; never replay a
renewal merely because its accounting is pending.
