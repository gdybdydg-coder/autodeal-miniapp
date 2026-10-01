# Private receiving-details preview — 2026-10-01

## Authorization and baseline

The owner approved adding the illustrated receiving-details card to the existing
bot and proposed testing activation on **2026-10-02 at 12:00 Europe/Kyiv**.
This release adds only an inert, owner-only preview. It does not authorize an
automatic public launch, real IBAN collection or a production paywall.

Connected GitHub main: `71bbf246870479362f7fbc48f2ba1afd29a8edd3`.
Base tree: `c7f556e42ec8f78c8869c9f15f154a1b06240d05`.
Render was live on that exact commit, deployment `dep-davb3unavr4c73b9e0lg`.
All 144 original repository blobs were verified against the pinned remote tree
before changes. Cached files were accepted only after their remote Git SHA matched.

Read-only production health succeeded with PostgreSQL connected and delivery
available. The initial source-status request timed out; a later bounded retry
returned only monitor, launch.activity, budget and quota. All 76 search groups
had successful cursors, the night interval was 3600 seconds, 2 valuations were
pending, Telegram delivery queue was empty and quota was available. No status
read consumed a development AUTO.RIA request.

## New private UI

`/subtest` now includes **🏦 Перегляд реквізитів**. The protected Telegram HTML
card shows the recipient name, receiving account/IBAN, recipient code, bank and
sample payment purpose, with the existing approved **250 UAH / 30 days** quote.
An existing order's immutable quote takes precedence over the current default.
There is no customer order ID, Telegram user ID or synthetic bank reference in
the message. The recipient code is a bank receiving field, not a customer ID.

The card prominently says **Тест без оплати** and tells the owner not to transfer
money or submit a real receipt. **📋 Скопіювати IBAN** uses Telegram's native
`copy_text` button. **✅ Я оплатив · тест** opens only a read-only receipt preview;
it does not mark a payment or grant access. From that preview, an explicit
**📎 Тестова квитанція** can use the already-existing synthetic trial workflow.
If no open trial exists, the owner can explicitly create one. Confirmation and
rejection remain in `/subtest_admin` / **🛠 Деталі власника**.

Both new callbacks authenticate the existing owner's private chat before reading
the receiving profile. Other users, groups and forged identities receive no data.
Opening these screens writes no database rows and consumes no trial update ID;
an earlier queued receipt or `/stop` remains effective. Actual documents/photos
are not collected, downloaded, reconciled or treated as paid receipts.

## Receiving profile and deployment

Real receiving details are absent from repository code, tests, fixtures and this
record. They are supplied only as `SUBSCRIPTION_PREVIEW_RECIPIENT_JSON` in the
existing Render service, with `mode: test_only`, `recipient_name`,
`recipient_code`, `iban` and `bank_name`. Only this new key is merged; existing
service settings and secret values must not be read or replaced. No activation
variable is introduced, and `PAYMENTS_ENABLED` remains hardcoded false.

Missing/invalid profiles show a compact unavailable message, without exposing
partial details or throwing. Validation checks complete string fields, length,
control characters, a ten-digit recipient code, Ukrainian IBAN structure and
MOD-97 checksum. Dynamic recipient/bank text is HTML-escaped. Offline tests use
an invented account with an all-zero routing code, never the owner's account.

Only subscription_preview.py, its tests and this record belong to the release.
The search transaction logic, discovery WIP, manual-payment WIP, Stars pilot,
public free access, /stop, epochs, sent/uncertain claims and accounting are
unchanged. There is no old-catalog scan, automatic purchase or recovery.

Release message uses `[skip render]` so configuration and code can be deployed
once together. Check current main and deploy history before a single manual
deployment; do not alter the existing auto-deploy setting or start a duplicate.
Verify every remote candidate blob against the tested local file before moving
main without force. Record live deployment verification in the WIP follow-up.

## Verification and limits

Focused regression: **84 passed in 4.14 seconds**.
Full backend regression: **1029 passed in 146.18 seconds**, one existing
Starlette/httpx deprecation warning. All functional edits preceded the full run.
The existing trial `apply` transaction function is unchanged.

New checks cover profile privacy/authentication, invalid configuration/checksum,
escaped formatted text, native copy payload, no invoice/payment link, unchanged
database rows during navigation, saved quote, real receipts ignored, and earlier
receipt and /stop events after higher-ID preview views. The regression is fenced
offline; no development paid API request or Telegram test message was sent.

An actual Telegram rendering/copy test remains for the owner. Offline tests and
a live deployment cannot prove a real bank transaction, customer access
activation, complete ad discovery, or a push notification on a phone.

Official documentation checked on 2026-10-01:

- https://core.telegram.org/bots/api#copytextbutton — native copy button.
- https://core.telegram.org/bots/payments-stars — digital goods/services inside
  Telegram bots/Mini Apps require Stars; the inert IBAN mockup is not a permitted
  live purchase flow or a complete billing implementation.
- https://api-docs.render.com/reference/update-env-vars-for-service — API env
  updates are saved without an automatic deploy; the connector's merge behavior
  preserves existing variables without retrieving their values into the context.
- https://render.com/docs/deploys — `[skip render]` avoids automatic duplicates.

Next owner check: `/subtest` → receiving-details preview → inert paid step →
explicit synthetic receipt → `/subtest_admin` approval → status/renewal review.
An exact one-time reminder is set for 2026-10-02 at 12:00 Europe/Kyiv, with no
automatic code/config/access/payment changes. Public billing requires a separate
compliant purchase design and an explicit launch decision.
