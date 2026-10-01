# Guarded subscription preparation

This release installs commercial billing and a durable launch check on the existing
service. It does not approve a commercial Stars price or convert 250 UAH to Stars.
The existing 1-Star owner pilot and synthetic receipt trial keep their own ledgers.
No discovery, filter, valuation, polling or AUTO.RIA quota code is changed.

## Initial deployment

Set protected `SUBSCRIPTION_LAUNCH_PREPARED=true` and
`SUBSCRIPTION_EXPECTED_ADMIN_ID` to the owner explicitly authorized for this task.
The code compares it with the existing `ADMIN_TELEGRAM_ID`; it never overwrites the
existing admin identity. Set `SUBSCRIPTION_APPROVED_OFFER_JSON={}` for this preparation.
No Stars price, terms, refund policy or search/payment review is currently approved.
Consequently both persistent `sales` and `enforce` start false and the campaign is
`scheduled_blocked`. The one owner preview is clearly marked as an unsellable draft.

The existing service's lifespan runs the executor every five seconds. Database
rows, not the timer, preserve the schedule and progress. Campaign ID:
`autodeal-launch-20261002-0900-kyiv`. Due: 2026-10-02 09:00 Europe/Kyiv / 06:00 UTC.
A start after 09:15 Kyiv expires without sending; a first installation after 09:00
also expires. The 15-minute window is a conservative operational expiry, not an
advertised offer deadline. Blocked, expired, complete and cancelled campaigns never
re-arm on restart. Merely changing environment variables does not overwrite a
persisted offer/campaign or clear its blockers. A later commercial decision needs
an explicit reviewed, versioned update to those records before the deadline.

## Schema and invariants

`Base.metadata.create_all` adds only new `billing_*` and `marketing_consents` tables.
No existing column/table is altered, no user/search/payment history is removed.
A real successful_payment must match the durable order, buyer, integer amount and
XTR currency. Invoice creation records explicit agreement to the current offer.
The checkout and invoice quote must match the current price/terms; stale callbacks
show current terms. An invoice or checkout approval never grants access. A late
valid successful payment is recorded even if sales were paused or the invoice
expired. Unique charge/order identifiers, one control-row transaction lock and a
single transaction protect the payment, access audit and expiration update.
Renewals add 30 days to max(current time, existing expiry). No recurring invoice
parameter is sent. Gift grants cannot shorten existing access. Real pilot access
is honored without becoming a commercial sale; synthetic previews never grant it.

Commercial API, enqueue and immediate pre-send checks enforce access only when the
persistent control says so. Saved filters, /stop, purchase/status/terms/support and
opt-out remain available. Search monitoring and matching remain unchanged.

There is no existing evidence of marketing consent. /start does not opt in.
`/marketing` shows a separate consent button; `/marketing_off` and `/stop` opt out.
Campaign selection requires explicit consent, ready user state, no known block and
no active access. The same conditions are checked before sending. Recipient rows
store claim, outcome, message ID and retry_at. 429 honors retry_after; timeout and
crashed in-flight sends become uncertain and are not blindly replayed. This cannot
guarantee exactly-once delivery after all possible network failures.

A DB lease excludes concurrent campaign executors; conditional updates protect
notice claims. Broadcast is at most one message per five seconds and yields to
pending/sending car notifications, command replies and recently created invoices.
Every send explicitly sets allow_paid_broadcast=false. Commercial payment handling
uses the existing webhook secret/authentication and a separate execution task.

Readiness requires approved price/terms/refunds/support and transition/search/
payment reviews, a current monitor heartbeat, recent accepted car delivery,
configured webhook, current discovery heartbeat when enabled, one confirmed admin
preview, an exact match of offer and campaign content, and read-only live Telegram
checks. No real test charge is made. Missing decisions or a critical error leave
new restrictions and advertising off. A permanent report is queued for the verified
admin. Telegram acceptance is reported as acceptance, never reading.

## Operator controls in the verified private Telegram chat

- `/billing_admin` — campaign state, blockers, audience counts and preview status.
- `/billing_admin payments` and `/billing_admin access` — recent ledger/access.
- `/billing_admin cancel` — cancel the campaign; retain entitlements/payment history.
- `/billing_admin pause` — cancel plus disable new sales and paid enforcement.
- `/billing_admin grant USER_ID ISO_DATE_WITH_ZONE reason` — audited gift/promise;
  requires an existing user, future timezone-aware expiry, never shortens access.
- `/billing_admin refund ORDER_ID` — review a real commercial refund.
- `/billing_admin refund_confirm ORDER_ID` — expressly request the actual refund.
  A persistent claim prevents retry of an uncertain result. Refunded access is kept
  pending an operator review of overlapping gifts/payments; never revoke blindly.
- `/paysupport question` — persistent support ticket delivered to the verified admin.
- `/billing_admin reply TICKET text` — authorized reply to that ticket's user.
- `/api/billing/admin` — read-only diagnostic protected by Telegram Mini App HMAC
  and the same configured owner check. No public billing admin endpoint exists.

## Rollback and recovery

Before any commercial sale exists, restore the previous code commit if preparation
breaks startup; retain all new tables. Once an order exists, retain the commercial
webhook handler and use `/billing_admin pause` as the safe rollback. Do not roll
back to an old handler that cannot record delayed successful payments. Interface
rollback alone never removes ledger records. Do not delete tables, truncate orders
or reset campaign send state. Unknown payment/refund/send results require manual
reconciliation, not a blind retry. Existing provider budget and delivery claims
must not be reset.

## Validation and limits

Tests use synthetic users, synthetic offer prices and isolated SQLite databases,
with the repository's socket/DNS fence. They cover invalid/duplicate/concurrent
payment, renewal/expiry, prior pilot/gift access, stale prices/terms, webhook/admin
access, API/enqueue/pre-send gating, delayed /stop, restart, durable cancellation,
leases, opt-out, 429, uncertain send/refund, blockers and monitor priority. Existing
backend/frontend suites are regression gates. Production deployment is verified
separately through Render's commit, /health release and sanitized startup/preview
logs. Offline fixtures are not a real payment or a live PostgreSQL concurrency test.

Official references checked for this release:
- https://core.telegram.org/bots/payments-stars
- https://core.telegram.org/bots/api
- https://core.telegram.org/bots/faq
