# Compact subscription trial cards — 2026-10-01

The owner requested shorter, visually formatted Telegram cards after testing
the deployed trial, with IDs shown only in an owner view. Screenshots showed a
long order identifier wrapping across lines and repetitive technical notices.

## Current remote baseline and unchanged policy

Main `fd9252f64fc5dbc07e300bc555f89aa4d64f09f7`, tree
`5c49812727f83d701e65efc111b8d3391412c6de`, was verified through GitHub.
Render deployment `dep-dav9vv3ncjis73cenpo0` is live on that commit.
Read-only health returned success, PostgreSQL connected and delivery available.
All 118 baseline backend blobs were restored from the pinned remote commit and
verified by Git SHA. No stale checkout or server secret was used.

The approved quote stays 250 UAH / 30 days. Collection and public paywalls remain
disabled; only the existing owner's private authenticated chat can open the trial.
No bank details, real receipts, invoices, customer grants or transfers are enabled.
The existing private Stars pilot, subscriptions, /stop, epochs, source accounting,
discovery/valuation policies and delivery claims are unchanged.

## Presentation and owner details

`/subtest` now shows a compact customer-style card with standard Telegram HTML
bold formatting, spacing and emoji for price, term, trial status and expiration.
One short notice clearly says the test is free and no money should be sent.
Redundant action-success sentences and the technical search/stop footer are gone.
An active trial remains clearly active when a later renewal request is rejected.

Customer-style message text contains no order ID, Telegram ID or synthetic
reference. Approval/rejection buttons are absent from this presentation. The
owner alone has a «🛠 Деталі власника» navigation button, leading to the separate
owner card. `/subtest_admin` opens the same private owner card directly. It shows
the ID in monospace and the applicable confirmation/rejection controls. A return
button shows the customer-style card again. This is still the single-owner trial,
not a queue of real customer requests or a public admin launch.

`subtest:view` and `subtest:admin` are authenticated read-only callbacks. They do
not consume the trial event cursor or write any row, so opening details with a
later update ID cannot hide a queued earlier receipt/approval. All state-changing
callbacks retain the existing authorization, deduplication and transaction logic.

Dynamic text is HTML-escaped. The only supported markup used is `<b>` and `<code>`
with `parse_mode=HTML`; actual formatting options were checked against the
official Bot API on 2026-10-01:
https://core.telegram.org/bots/api#html-style

Owner-scoped menu probe `subscription-owner-menu-v2` adds `/subtest_admin` while
preserving public, configured quota/check and existing Stars commands. No global
menu or environment configuration is changed.

## Validation and release

73 focused backend tests passed in 4.53 seconds with the repository's offline
network fence. New meaningful checks cover identifiers/actions absent from the
customer-style view in every order state, escaping/balanced supported HTML,
forged/group detail callbacks, read-only navigation and an earlier queued action,
plus continued owner-menu scope and free public access.

Final full backend regression: **1005 passed in 151.05 seconds**, one existing
Starlette/httpx deprecation warning, no failures. All functional edits preceded
this run. The existing `apply` transaction function is byte-for-byte unchanged.

Only the trial presentation/routing, its tests and this record are release changes.
No full manual-payment or discovery WIP is merged. The release commit includes
`[skip render]` for one controlled manual deployment, avoiding a second automatic
deployment; verify deploy history before requesting that single deploy. This does
not change service auto-deploy settings. Exact remote tree must match the tested
files and main must move without force.

After deployment, send `/subtest` for the new card, then «🛠 Деталі власника» or
`/subtest_admin` for ID and test confirmation controls. New replies use the new
format; no previous Telegram message is deleted or rewritten. Re-opening a view
does not activate access, restore stopped searches or transfer money.
