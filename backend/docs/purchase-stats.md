# Administrator purchase statistics

`/stats` retains operational/queue/latency/quota information and adds lifetime
purchase counts. The customer/search headline counts use the same customer
scope as the purchase block; registered service accounts are excluded.

## Evidence and scope

- Universe: registered `users.id`, irrespective of ready/stopped state, searches,
  current entitlement or expiration.
- Exclusions: `ADMIN_TELEGRAM_ID`, the existing owner-only Stars/preview account
  constants, and explicit comma-separated `STATS_EXCLUDED_USER_IDS` if configured.
  No identity/name guessing, and notification tests do not exclude real customers.
- Buyers: distinct members of that universe with an `approved` manual payment
  request, positive amount/duration and UAH currency. Both historical bank-reference
  and current receipt-based review workflows set `approved` only in the atomic
  owner-confirmation transaction. The older `BankCredit` record is optional.
- No expiration predicate: repeat approvals/renewals count once and past buyers
  remain buyers. Unconfirmed receipts, pending/rejected/cancelled requests,
  gifts, synthetic previews and the owner-only Stars pilot do not count.
- `billing_orders`/`AccessEvent(kind=paid)` represent automatic Stars purchases,
  not administrator-confirmed bank receipts; they are not this metric's source.
- There is no generic test flag in historical manual requests. An unlisted test
  account with an ordinary owner-approved request cannot be inferred reliably;
  an explicit verified exclusion is necessary. No production IDs are invented.
- Counts attest to owner confirmation, not independent bank reconciliation.

All three values come from one aggregate SELECT and one database snapshot, with
no per-user queries, cached numbers, writes, or access-gating changes. The two
client sets are joined before counting, so orphan payment rows cannot inflate
the numerator. `not_purchased = total - buyers`.

## Access and failure handling

The webhook authenticates its secret, validates a private non-bot sender and the
bot mention, then checks the configured administrator on every `/stats` request
before touching the database or other command handlers. Database/configuration
errors return `Не вдалося отримати статистику`, never fabricated zeros or SQL.
Only the aggregate reply is returned; receipts and bank/account data are absent.

At startup, one read-only check runs the deployed purchase query and full `/stats`
formatter. Its private service log contains aggregate counts, a UTC timestamp,
the subscription block and `command_render_ok`; it sends no Telegram messages.
This verifies runtime query/rendering, not delivery of a real Telegram command.
No new HTTP endpoint, migration, production test payment or scheduled task exists.
