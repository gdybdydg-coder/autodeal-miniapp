# Manual payment integration — preparation, NOT a public launch

## Verification update — 2026-10-02 07:09 UTC / 10:09 Kyiv

Branch: `wip/manual-payment-verification-20261002`.
Verified code commit: `316a3cae0ef9cf2fcc1c2ab72cfe66a2a84b7458`.
Code tree: `12a86c4b5352b0a800d6e5bd1545b6da1cb2ffd5`.
CI: https://github.com/gdybdydg-coder/autodeal-miniapp/actions/runs/36976966110

The earlier local PostgreSQL/browser limitations below are now resolved by an
isolated GitHub runner: PostgreSQL 16, a fresh schema for each test, synthetic
identities/credits and Chromium with every browser request intercepted.
No production credentials are used. The job has read-only repository permissions,
a 15-minute deadline and no deploy/send step; its database is destroyed afterward.

Final results: **118 backend tests passed**, **80 frontend tests passed**, and the
390px browser flow passed: queue → owner review → final confirmation → stored
result. No horizontal overflow was detected in the confirmation view. The one
existing Starlette/httpx deprecation warning remains. These checks do not prove
actual phone clipboard behavior, bank credit or production database migration.

Added a real PostgreSQL/SQLite race check between manual confirmation and the
existing owner access grant. A changed expiry invalidates a stale confirmation;
neither path shortens access. Existing tests cover concurrent duplicate requests
and confirmations, transaction rollback, bank-credit reuse, restart, renewal,
authentication and notification uncertainty.

Fixed stale queued notifications: when a request advances, older pending/retry
notices become `superseded` instead of arriving after approval. They remain in
the history, with a clear owner UI label. Already attempted or uncertain notices
are retained for reconciliation, not hidden or blindly retried. In-flight sends
cannot be recalled if a request changes after the send was claimed.

Carried forward main's receiving-card copy change (`4c3df6d`) in this branch.
This follow-up did not change production, grant access, activate sales, modify
schedules or send customer messages. The owner-approved campaign text remains
250 UAH / 30 days; it has not been sent.

Still required before a real launch: resolve the platform/payment-method conflict,
connect a permitted customer purchase flow, verify support/terms and transition
rules, inventory promised existing access, and verify a private production backup
and restore. No new Stars integration was added. Public bank request creation
remains blocked; passing CI is not a paid-launch authorization or completion claim.

Owner authorization received directly in chat on 2026-10-02. Agreed terms remain
250 UAH / 30 days, bank transfer, owner personally verifies actual credit and
explicitly grants access. No Stars integration, real payment, customer message,
campaign, production database/configuration change or deployment in this work.

## Earlier production evidence — before the card-copy release

Production base: `43084b2b7e8551e4cabff3ee3eb2fb90142dc402`;
Render live deploy: `dep-davcskad0e5s73fgb8n0`.
At 2026-10-02T06:00:03.568857288Z (09:00:03 Kyiv), the existing launch executor
logged `blocked`, `sales=false`, `enforce=false`, 161 existing users, zero commercial
orders/entitlements, zero campaign recipients. The code makes blocked terminal;
restart does not re-arm it. This is NOT a complete inventory of legacy/gift/trial
access. External noon automation is a reminder only, not an activation. Neither
it nor unrelated automations was changed. The three-run night snapshot is disabled.

A later bounded filtered log read returned 156 `notification accepted` records,
from 05:32:24 through 06:30:26 UTC, no error matches, hasMore=false. Acceptance is
not user reading; a filtered window is not proof of full AUTO.RIA coverage. No
provider requests were made by this development task.

## New backend

- Six additive `manual_*` tables in separate SQLAlchemy metadata. No existing
  table is altered and no prototype row is copied to real access.
- Every mutation takes the existing BillingControl UPDATE lock, the same lock
  used by legacy paid and gift grants. Missing control fails closed.
- One open request per user; immutable snapshot: 25000 minor UAH units and 30 days.
- Optional late evidence; each update has immutable evidence/audit history.
  Receipt IDs enter only from the secret-authenticated Telegram webhook and
  request owner, not arbitrary Mini App JSON. Original receipt remains in Telegram;
  the application does not download files or handle bank passwords.
- Owner-only queue, statuses, literal search, counts and pagination. No username
  required. Requests persist independently of owner notifications.
- Two-step confirmation: exact bank amount, owner verification checkbox, stable
  receiving-account and credited-operation IDs; raw bank references are hashed.
  A receipt filename is not a bank reference and the software cannot establish
  whether the owner's bank input is truthful or aliases one existing operation.
- Confirmation hashes tokens, binds owner/request revision/prior expiry and a
  five-minute deadline. Replay returns committed result. Stale tokens fail closed.
- Bank credit uniqueness, real Entitlement, AccessEvent, request, audit and notice
  are ONE transaction. The old paid pilot and gifts are preserved via billing.expiry.
- Renewal: `max(now, existing expiry) + 30 * 86400`. UTC epoch storage; Kyiv display.
  Search readiness, /stop, filters, epochs and car delivery rows are not touched.
- Durable notices distinguish accepted, known rejected, retry (429) and uncertain.
  Only known failures can be retried by the owner. Timeout/crashed send acceptance
  is uncertain and requires reconciliation. No exactly-once delivery claim.
- Storage errors return 503 without SQL/parameters, never an unpaid label.

## Registered preparation interfaces

`MANUAL_PAYMENT_REVIEW_ENABLED` and `MANUAL_PAYMENT_NOTICES_ENABLED` default false.
Neither was set on Render. With review disabled, no manual tables are created,
API rejects access, /payments falls through, and no notice task starts.
Both switches are required for the notice executor. Existing protected owner
configuration is checked on every admin action; no administrator is assigned by name.

When deployed and explicitly enabled in a separately reviewed environment:

- `/payments` and `/payments all 1` open the persistent queue and owner Mini App.
- `payment-review.html` uses signed Telegram initData headers only, not localStorage,
  query-string identities or user-supplied IDs. UI text uses textContent.
- `/api/manual-payments/admin` lists requests; card, notice state, clarification,
  rejection, preview, confirm and known-failure retry routes enforce owner identity.
- `/payment_receipt AD-...` as photo/PDF caption attaches late evidence. Owner-only
  receipt deep link returns the stored Telegram file to the protected owner chat.
- Customer status and paid-report routes preserve access to support workflows even
  without a paid entitlement. Internal Telegram ID is absent from customer status.

Public request creation deliberately returns `public_bank_sales_blocked_by_platform_policy`.
There is no production environment switch to bypass it. Isolated API tests override
the policy function in memory, against synthetic SQLite and isolated PostgreSQL. No live customer can
create a bank-payment request through this branch as delivered. Public client
checkout/requisites are therefore NOT connected to the live Mini App.

## Earlier verification — superseded by the update above

- Existing prototype: 121 tests passed under outbound TCP/DNS fence.
- Full backend: 1086 passed (1062 existing + initial 24 new); after final regression
  additions, focused manual suite: 27 passed. One upstream TestClient deprecation warning.
- Full frontend: 79 passed (75 existing + 4 new owner UI behavioral tests).
- New API tests exercise the actual create_app routing, signed Mini App HMAC,
  webhook secret, foreign identities, closed/late receipts, configuration revocation,
  concurrent create/confirm, duplicate bank credit, gift/legacy preservation,
  renewal/expiry/restart, transaction rollback, /stop, notices and feature lifecycle.
- PostgreSQL CREATE TABLE syntax compiled for all six tables. This is NOT a live
  PostgreSQL transaction/concurrency/migration test. PostgreSQL server binaries are
  absent; package setup failed due to unavailable setgroups/setuid capabilities.
  No production database was used as a substitute.
- Browser smoke script prepared: `node tools/manual-payment-ui-smoke.cjs`.
  All HTTP is mocked and data synthetic. NOT executed successfully: no Chromium
  binary; attempted browser download returned an invalid/truncated archive.
  Node UI behavior tests passed, but browser/phone visual validation remains open.

## Separate security / repeat-processing review

Reviewed lock sharing, request ownership, constant tariff, SQL parameters, bank
uniqueness, confirmation replay/expiry, status privacy, file-ID provenance, UI text
escaping, notice claims and rollback boundaries. Found and fixed a misleading
receipt-success message for already-closed requests; regression added. Uncertain
notices intentionally have no retry button. No secret or real receipt fixture used.

## Before any production integration

1. Resolve the commercial/platform blocker without silently changing the owner's
   payment method. Official rule: https://core.telegram.org/bots/payments-stars
   requires Stars for digital sales inside bots/Mini Apps. Manual review does not
   remove it. Do not masquerade as a donation/physical product or claim a website
   automatically provides an exception.
2. Isolated PostgreSQL and mocked browser checks are now complete, including
   concurrent owner grants, rollback, unique constraints and restart. Actual
   owner checks in Telegram on a phone remain outstanding.
3. Verify real recipient/IBAN, support and terms from protected owner data. No
   recipient profile was verified or invented in this run.
4. Produce owner-authorized aggregate inventory of existing paid/gift/trial/expired
   users. Do not infer it from the commercial-entitlement count. Preserve promises.
5. Use an authorized private backup of the relevant existing and new tables,
   encrypted storage with restricted access, and restore into an isolated database.
   Compare counts, entitlements and sample audit consistency. No production backup
   or restore was performed here. Do not place data/dumps/credentials in Git.
6. Only then consider additive schema and code deployment with review/notices,
   sales and enforcement still disabled, followed by authenticated owner checks.
   Never import the synthetic fixture database. Startup create_all is initial
   additive creation only; later schema changes need versioned migrations.

## Pause / rollback

This change has not run on production, so no operational rollback is required now.
For a future verified deployment, disable MANUAL_PAYMENT_NOTICES_ENABLED to stop
new sends and MANUAL_PAYMENT_REVIEW_ENABLED to pause manual handlers. Retain all
manual tables, bank-credit uniqueness records, BillingControl, Entitlement and
AccessEvent. Keep customer status/support available in any planned production
rollback; the all-handlers switch is an emergency measure, not a sales toggle.
Do not restore an old database over newer payments. Review code rollback may use
the prior release only if it continues honoring the shared Entitlement table and
all delayed payment handlers. Existing /billing_admin pause is a separate old
Stars/campaign control; it is not a manual-payment launch command.

## Example text (synthetic preview, not sent)

Customer: «🕓 Заявка AD-TEST123456 прийнята. 250 грн / 30 днів.
Власник перевірить фактичне зарахування та особисто підтвердить доступ.
Повторно сплачувати не потрібно. Статус збережено, квитанцію можна додати пізніше.»

Owner: «📥 AD-TEST123456 · Тестовий клієнт · 250 грн / 30 днів.
Очікує перевірки. Квитанція не є підтвердженням зарахування.
✅ Підтвердити надходження і відкрити доступ · 💬 Запросити уточнення ·
❌ Відхилити · 📋 Усі очікують».

Real credit decisions, verified banking information and approval of real requests
remain exclusively with the owner. No real request was approved by the assistant.
