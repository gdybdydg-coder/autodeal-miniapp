# Inactive subscription preview in the actual bot — 2026-10-01

## Authorization and scope

The owner's latest instruction is: «Так, тоді загружай проплату в сам бот, але
поки не вмикай його ні для кого, поки ми працюємо безкоштовно.» It authorizes
integration and an ordinary deployment of this inactive preview. Earlier WIP
restrictions continue to apply to public charging, access enforcement and the
free-discovery experiment. No other WIP changes are included in this release.

Approved commercial quote: **250 UAH / 30 days**, retained as UAH, not silently
converted to a Stars amount. This release implements a persistent synthetic
trial, not live bank collection or a complete production billing system.

Baseline checked through the connected GitHub and Render services:

- Main: `005897b0a93c7fe0166424341c0564a5fbaf94cb`.
- Tree: `0cc95880c4c2676ac5c0d14a2427ae232ea67bed`.
- Manual trial WIP: `938aa3226cacbeb2dabcb27d7d2a2e5c499fbe90`.
- Discovery WIP: `2c5ce7fb94028b9abffae569b2a0acf2a7b4c72f`.
- Existing Render deployment: `dep-dav2570473hc73d7bkj0`, live with that main,
  completed `2026-10-01T09:07:41.772737Z`.
- `/health`: successful, PostgreSQL connected, delivery available.
- The initial projected source-status read timed out after 15 seconds. No full
  status was printed and this timeout is not proof of a source failure.
- Existing service auto-deploys main commits; no new resource or env edit is needed.

## Bot behavior

Only the existing owner's authenticated private Telegram chat can use `/subtest`
or `/subscription_test`. The owner menu preserves public commands, the existing
Stars pilot and check/quota commands when enabled for that owner. Other users,
group chats, bots and forged owner callbacks receive no trial controls or grants.

The command displays the approved quote and an explicit disabled-payment notice.
Buttons create one open order, mark a **synthetic** receipt, approve a test term,
reject an open order or test renewal. No real receipt, IBAN, receiving profile,
invoice, purchase button or money transfer is processed. Only authenticated owner
button acknowledgements and responses to requested commands are sent; there is
no promotional or test-listing broadcast.

`subscription_previews` is a new additive table, separate from search
subscriptions and Stars payments. Its single owner row contains bounded history
(20 orders), immutable order quotes, synthetic references, a test expiration and
its own event cursor. PostgreSQL row locking and a version compare-and-swap
protect concurrent actions. Duplicate approvals cannot add time twice; renewal
extends from the later of current time and test expiry. Backward server time
fails closed. Recreating an engine reads the saved state; production PostgreSQL
restart behavior still requires an actual owner test.

The Telegram webhook adds `callback_query` to the existing message and checkout
updates, retains its secret validation and does not drop pending updates. Invalid
or unrelated buttons cannot change searches. The test event cursor never writes
`User.last_update`, so a later-arriving `/stop` is not masked.

No paywall or paid-access guard exists in this preview. Monitoring, delivery,
positive current-price checks, optional-field/damage handling, saved filters,
minDiscount, official AI valuation and confirmed-deals policy are untouched.
Stopped searches stay stopped. Epochs, sent/uncertain claims, quotas and counters
are not reset. No recovery or old-catalog scanning is introduced. The earlier
owner-only 1-Star payment/refund experiment is unchanged.

## Validation

The fresh backend snapshot came from the remote baseline, with every original
backend blob verified against its Git SHA. Tests use pinned backend requirements
and the repository's offline network fence; no paid AUTO.RIA call was made.

Final regression command: `python -m pytest backend/tests -q`.
Final regression result: **995 passed** in **148.96 seconds** after all code
changes, including callback updates and quota-menu preservation. One existing
Starlette/httpx deprecation warning appeared; no test failed.

Meaningful preview cases cover authentication and chat scope; ordinary free
search access; saved quote; synthetic receipt/approval/rejection/renewal;
duplicate, concurrent and out-of-order updates; exact expiry; rollback in time;
bounded history; acknowledgement failure; new-engine persistence; and preservation
of every pre-existing database table during trial actions. An explicit test proves
that a trial's higher update ID cannot suppress `/stop`. Menu tests cover both
matching and different configured quota owners. Webhook configuration tests verify
callback delivery without dropping pending updates.

SQLite concurrency and generated PostgreSQL DDL were checked offline. These are
not a substitute for observing the owner's real Telegram command or PostgreSQL
transaction in the deployed service. No automatic transfer or test grant is
created during deployment.

## Collection constraint and next step

Official Telegram documentation, checked on 2026-10-01, requires Stars for selling
digital goods/services inside bots or Mini Apps:

- https://core.telegram.org/bots/payments-stars
- https://telegram.org/tos/bot-developers (section 6.2)

Consequently this deployment does not install actual IBAN collection instructions
or enable card-payment approval. A compliant real purchase flow, its Stars price,
receipt reconciliation/refunds and public launch remain separate decisions and
implementation work. Changing a flag cannot turn this synthetic preview into
real billing.

After deployment, the owner can send `/subtest` and complete the synthetic buttons.
Check the displayed status again in a later session. A real payment or a production
restart test must not be claimed from this offline result. If rollback is needed,
revert only this code release through a new ordinary commit; do not delete test
history, existing Stars orders, search states or accounting.
