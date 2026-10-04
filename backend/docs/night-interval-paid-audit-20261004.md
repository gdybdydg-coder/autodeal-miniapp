# Restore the owner's night schedule and audit paid-source eligibility

## Verified baseline

The full handoff was read before the change. GitHub/main and the running Render
release were both `d2744ad947dda595fd420d9db117133a443df25e`, tree
`0ccb20d5143ecd79d57f320fcca7a54bb3f973cb`. The previous checkout had a
different commit history but the same tree. Work uses a separate worktree and
branch `fix/restore-night-paid-audit-20261004` from the fetched public main.
No applicable AGENTS.md was present in the checkout or its ancestors. The
existing manual-payment workflow triggers other named branches, not this one.
Render watches main; previews are off. No configuration or database mutation
is part of this repair.

## Defect and correction

The preceding release introduced `continuous_paid`, substituting 60 seconds
for the explicit 3600-second night target. This was an unauthorized schedule
change, not an access failure. The corrected expectations failed in all 21
isolated baseline cases, including the strict-paid 23:00 transition, winter,
DST and reserved-budget planning cases.

Remove that override from the policy and every caller. The policy file is
identical to the preceding `e4657cc6` version. Targets in Europe/Kyiv are:

| Period | Base interval |
| --- | ---: |
| 23:00–08:00 | 3600 seconds |
| 08:00–18:00 | 110 seconds |
| 18:00–23:00 | 60 seconds |

Existing planning reserves and all durable rolling quota gates remain. They
may lengthen these intervals. Normal healthy timers adopt the current period
without clearing cursor/epoch, quota/error waits, in-progress pagination or
delivery claims. The retired-HTML primary recovery from the preceding release
is retained. The supplemental publication source keeps its existing schedule
and independent caps.

## Paid-client investigation

The source-access predicate, purchase writers, billing, filters and worker are
unchanged. Both actual owner-confirmation writers (bank-reference and current
receipt review) update the approved request, entitlement, approval audit and
access event together. Their saved-search activation and renewal are checked
through those writers, not by declaring an entitlement to be a purchase.

The private read-only pipeline snapshot now includes the full current schedule
and an independent comparison of durable `PaymentAudit.action == approved`
expiry, operational entitlement, guarded eligibility and monitor membership.
It distinguishes an inconsistent current approval from revoked access,
stopped/disabled searches, missing/stale epochs and explicit exclusions. It
also distinguishes administrator/pilot exclusion from additional configured
exclusions. A discrepancy is evidence to investigate; diagnostics do not grant
access, rewrite payment history or construct any source/Telegram transport.
The audit is not published in public source status and contains no recipient
IDs, filter values, receipts or bank data.

The pre-change production query covered
`[2026-10-04T04:30:00Z, 2026-10-04T07:38:12.804718043Z)`
(07:30–10:38:12 Europe/Kyiv), with `hasMore=false`. After filtering actual
acceptance events, it contained eight client automobile messages and eight
owner copies. Snapshot records in the same log results were not counted as
delivery events. These events predate this deployment and do not establish
delivery to every buyer, phone push arrival or complete market coverage.

## Isolated validation

Run tests with a clean environment, temporary databases, fake source/Telegram
transports and the repository's collection/session-level network fence.
The first affected run passed 144 cases in 23.79 seconds. It includes both
confirmation formats, renewal preserving days, payment/stop checks at actual
transport boundaries, quota waits, HTML retirement recovery, restored night
timers across restart and read-only eligibility mismatch diagnostics. A reused
SQLAlchemy session observes another session's payment revocation.

All 1732 collected backend cases passed in disjoint groups: 569 in 90.82 s,
569 in 66.98 s, 567 in 90.12 s, and all 27 owner-copy cases in 13.09 s.
The 200-paid-client delivery case passed in 4.39 s. An earlier monolithic
attempt ended without a final summary and is not counted as successful.
Live rollout evidence is recorded separately after completion. An existing
Starlette TestClient deprecation warning is unrelated to this repair. No paid
diagnostic scan, test purchase, client test message, quota reset, advertisement
or OLX activation is used.

## Rollout and limits

Recheck main immediately before fast-forward promotion; never force a ref or
overwrite another change. Confirm the exact deployed commit and private
schedule/access snapshot. Check the existing deploy queue before any single
manual trigger if automatic deployment does not start.

The handoff reports that direct external PostgreSQL diagnostic access was
unavailable. This repair uses private application aggregates; no firewall or
secrets are changed. Saved aggregate evidence can establish recorded
eligibility and Telegram acceptance. It cannot independently verify bank
receipts, device push arrival or why a particular unprovided listing was
missed. A short quiet observation after rollout is not delivery proof.
