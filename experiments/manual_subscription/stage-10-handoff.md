# Stage 10 — full manual queue, isolated review, 2026-10-02 Kyiv

Status: CODE PREPARED AND OFFLINE-TESTED; NOT DEPLOYED. Price 250 UAH / 30 days.
The owner personally checks actual bank credit, then explicitly confirms access.
No new Stars code, automatic bank reconciliation, charges, Telegram sends or reminders.

## Reuse and gaps addressed

Imported existing public stage-9 experiment from commit
938aa3226cacbeb2dabcb27d7d2a2e5c499fbe90 onto production base
43084b2b7e8551e4cabff3ee3eb2fb90142dc402. Existing /subtest is a separate
owner-only synthetic preview, not a durable customer payment system.
Stage-9 Ledger, local invitation auth, receipt quarantine and durable outbox are reused.
`ReviewLedger` adds optional/late receipts, full queue, actor audit with expiry before/after,
bank-account + actual-transaction uniqueness, two-step owner confirmation, rejection reasons,
known-failure retries and disabled-by-default backlog summary. All runtime files unchanged.

## Open ALL requests

Run from repository root, only on the reviewer's own computer:

```
python experiments/manual_subscription/queue_harness.py --db /tmp/autodeal-review-fixture.sqlite
```

Use a NEW synthetic database. Open http://127.0.0.1:8767/admin and /client.
Read the private invitation file printed by the command; enter the matching one-use role code.
The admin default is waiting; **Усі заявки** includes created, waiting, needs-info,
approved and rejected. Search by code/name/username/exact user ID; page size 20,
Previous/Next reaches all records. Opening a card does not change its state.
The prepared server command is `ReviewAPI.command(session, '/payments', filters, now)`.
**/payments is not yet registered in the live bot.** Existing older harnesses remain
historical stage-9 tests; use queue_harness.py for the new workflow.

Requisites are loaded only from an explicit private profile using the existing validator.
Absent profile: no invented recipient or bank details. All displays prohibit real transfers.
Profile format validation does not establish bank account ownership. Support uses existing
/paysupport as a future integration route; the local prototype sends no messages.

## Verification

`python experiments/manual_subscription/check-offline.py`: 121 tests passed,
non-loopback TCP/DNS fenced. Includes three new actual loopback HTTP scenarios.
The 15 queue tests cover all requested payment/renewal/queue/auth/rollback cases;
125 requests traverse seven pages, plus a user without username and same-amount users.
`node --check .../review-ui.js`: passed. HTTP tests exercise the backend used by the new UI;
a real browser/phone rendering and clipboard check remains outstanding.
Existing production entitlement gates checked OFFLINE: 4 tests passed (API + enqueue +
presend enforcement, existing gift/paid pilot preservation, admin grant and disabled offer).
These gates are not switched on and are not connected to this SQLite membership table.

## Boundaries and remaining implementation before production

SQLite BEGIN IMMEDIATE proves atomicity here; PostgreSQL integration/migrations are not yet
implemented or tested. Production integration must put request + unique bank-credit +
Entitlement + AccessEvent + notice in the SAME PostgreSQL transaction. Never copy synthetic
memberships to real sales. Preserve maximum existing paid/gift/approved trial expiry and
/stop/search epochs; use the existing server API/enqueue/presend gates.

Confirmation is owner-only, revision/expiry-bound and expires after 5 minutes. Preview shows
an estimated expiry; final expiry starts at actual confirmation (up to 5 minutes later).
Repeated confirmation returns the committed result. Different tokens for an already-approved
request fail closed. The bank reference must be the operator's stable, actual credit ID and
stable receiving-account key. The system cannot verify the truth of operator input.

Notification outbox is durable and survives send failure. Known rejection can be retried;
ambiguous Telegram acceptance remains uncertain for operator reconciliation. No exactly-once
Telegram delivery claim. Summary records are deduplicated per hour and OFF by default;
no summary sender/schedule has been enabled. Files remain a bounded quarantine, not malware
scanned/encrypted production storage. Retention/scanning/authenticated Telegram integration
and operational support/refund rules still need review. Rejection never claims a refund.

Official Telegram digital-goods rules require Stars for sales within bots/mini apps:
https://core.telegram.org/bots/payments-stars
Manual bank verification does not itself remove that conflict. Public payment rollout remains
blocked pending a resolved model; isolated queue development continues. No Stars implementation
was added to satisfy that rule. Existing production billing campaign is unchanged.

## Journal / acceptance criteria

- Restored and reviewed stage-9 code; production main/Render checked read-only.
- Implemented and tested durable queue; no reliance on owner-message delivery.
- Added authenticated local UI, late uploads and two-step final confirmation.
- Reused server access guard tests; integration gap is explicit.
- Ready for owner review of local synthetic workflow, not real collection/deployment.
