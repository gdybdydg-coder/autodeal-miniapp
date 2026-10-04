# Paid access and ordinary owner notifications

Baseline: `82d32a4e13952e7230ef44f22bbf36a1ed2c3973`, Render
`dep-db10eldg1s2s73fvuni0`, live 2026-10-04 08:00:23 UTC. Main and the
deployed release were checked again before this repair. One Render web service,
one instance, uvicorn factory; monitoring and dispatch run in that application.

## Proven regressions

- `990316b` reused statistics exclusions for paid source access. Statistics
  intentionally exclude the owner; source eligibility consequently rejected
  even that account's approved manual purchase and current entitlement. The
  same false denial made an owner renewal appear to be a first activation.
- The administrator-copy producer bypassed ordinary payment/filter eligibility
  for its recipient. Isolated cases reproduce a copy to an unpaid owner and a
  copy to a paid owner whose own discount filter rejects the car.
- After a delivery claim was committed, an initial access-read exception could
  leave an unattempted delivery in `sending`. A generic pre-transport callback
  failure was classified as uncertain. After an explicitly rejected Telegram
  photo, the saved attempt history lost a subsequent fallback access-read
  failure's technical reason.

No widespread denial of the three non-administrator current paid accounts was
established in the pre-release production snapshot. Three searches belonging
to two of those accounts were planned; the third had no enabled search. This
is not a claim of complete discovery or successful delivery of every match.

## Repair and boundaries

The configured owner uses the same approved-manual-purchase and current-
entitlement predicate as every customer. Administrator role, Stars pilots,
previews, gifts, pending applications and screenshots cannot authorize source
work. Other explicitly excluded service accounts remain excluded. Statistics,
payment confirmation, price, duration, filter logic and quotas are unchanged.

The car-copy producer, historical recovery, copy payload, special rendering
and transport bypass are removed. Startup and a throttled cleanup retire only
unattempted copy reservations. Old accepted/attempted/uncertain history stays
intact and is not replayed. A never-attempted reservation can be reclaimed only
through the ordinary fresh, paid, currently matching queue path; its retained
marker is classified as `ordinary_reclaimed`.

One durable owner activation marker bounds restored ready/enabled searches to
the repair time. It changes activation epochs, retaining search definitions,
payment records, delivery history, previous seen/match rows and every other
customer's shared feed. Stopped/inactive searches remain inactive. Access-read
or migration failures are technical failures, never payment grants.

Known access-read failures before a new Telegram attempt defer the same claim
by 60 seconds and retain `access_unavailable`. A real ambiguous Telegram
transport still remains uncertain and is not automatically retried. A complete
database outage can prevent recording a deferral; no general outage guarantee
is inferred from these tests.

Private, bounded SELECT-only diagnostics report each current purchased
account without identifiers, payment credentials or filter values. They retain
approval/access evidence at the time of saved acceptances, distinguish old
copies from ordinary messages and provide concrete saved decision examples.
No diagnostic provider scan or Telegram message is sent.

Night remains 23:00–08:00 Europe/Kyiv at 3600 seconds, day 08:00–18:00 at 110,
evening 18:00–23:00 at 60. OLX, promotion, billing and provider plans are outside
this repair.

## Validation and rollout

Regression fixtures use temporary SQLite databases, fake AUTO.RIA and Telegram,
a clean environment and a collection/test network fence. Required cases cover
unpaid-first ordering, two paid recipients sharing one quote/detail, both
confirmation formats without restart, legacy purchases, queued expiry, stop,
disabled searches, owner ordinary matching/nonmatching delivery, receipt-photo
admin notices, overlapping workers, restart, timeout and safe retries.

All 1774 collected cases in 82 files passed in three disjoint complete groups:
592 in 115.88 s, 591 in 70.76 s and 591 in 116.07 s (each exit 0). Before repair,
the owner-access/renewal reproduction had 7 failures, unchanged copy paths had
2 failures, and the exact frozen baseline delivery-read reproduction had 3
failures. The corresponding fixed cases and adjacent scenarios pass. The only
warning is the existing Starlette TestClient deprecation.

Before promoting main, check the parent still matches the recorded baseline,
publish the exact tested tree with a non-force fast-forward, and verify the
actual Render commit. Observe normal work only. Telegram API acceptance is not
a phone receipt, and `/health` is not a delivery test.

## Safe rollback

Retain the recorded baseline and use a new non-force commit for any rollback.
Do not run a database downgrade, delete the activation marker, reset a shared
feed, or revert to an unrestricted source policy. Payment/search/history rows
are not rolled back. A rollback must retain the strict purchase predicate,
the corrected ordinary owner eligibility, disabled copy production and the
one-time activation boundary; reverting the entire old release would restore
known owner/copy defects. The optional expanded read-only diagnostics can be
reverted independently to their baseline implementation. Source and Telegram
work can be paused during a material incident while preserving payment and
search records, then a reviewed minimal forward repair deployed.
