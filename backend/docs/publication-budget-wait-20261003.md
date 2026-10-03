# Publication intake: distinguish quota waits from missing automobile details

The October 3 read-only production snapshot showed the supplementary dated
public-page intake at its rolling 1,000-call daily ceiling. This is a separate
ceiling from the shared operator/provider budget. The previous intake incremented
its detail-attempt counter before admission and discarded a candidate after
three quota/lease refusals as `unavailable_details`, even with no detail request.
Server health alone did not identify that loss or establish fresh discovery.

## Change

- Preserve candidates during quota, access and lease waits. Compute the first
  possible local retry from rolling reservations and the existing primary
  headroom. These waits consume no detail-failure attempts and survive restart.
- Retain the one-hour publication evidence cutoff. A candidate that becomes too
  old is recorded as `expired`; this patch does not replay old advertisements or
  revive previously discarded/sent/uncertain delivery records.
- Count real transient detail failures separately; retain their three-attempt
  bound. Keep every source and Telegram eligibility check.
- Read optional same-ID public card brand/model/year fields and reject explicit
  search contradictions before a detail request. Missing/conflicting metadata
  remains unknown and proceeds to authoritative official details. Do not infer
  region/category or filter out damaged whole cars. Cross-page conflicts remove
  only the preview assumption.
- Expose the supplementary admission reason and local retry timestamp alongside
  the existing aggregate public intake status. Clear stale `last_error` after a
  successful collection.
- Log one private, read-only startup snapshot of actual current paid searches,
  primary feed cursors/states, global reservation gates, recent source attempts
  and Telegram receipt timestamps. Do not expose purchase/search aggregates in
  a public endpoint. No recipient IDs, filter contents or payment receipts enter
  the new diagnostic. Missing diagnostics are logged as unavailable, not zeros.
  A bounded local audit also partitions recorded search/car pairs by filter,
  valuation, discount and delivery outcome. It distinguishes distinct cars from
  per-search decisions and flags truncation; it performs no new source calls.

Hourly/daily/total caps, accounting, payment policy, subscription records,
filter values, the AUTO.RIA price formula, scheduler cadence, OLX and marketing
campaigns are unchanged. No paid diagnostic probe, quota reset or package purchase
is performed. Availability is local admission, not verified provider balance.
Quota expiry can still prevent delivery; this patch must not be described as a
complete remedy for insufficient intake capacity or unmeasured source coverage.

## Verification

Use an isolated test environment, never the production entry point or secrets:

```sh
python -m pytest backend/tests/test_publication_budget_wait.py \
  backend/tests/test_recent_publications.py \
  backend/tests/test_paid_sources_production.py \
  backend/tests/test_owner_car_notifications.py \
  backend/tests/test_monitor.py backend/tests/test_parallel_discovery.py \
  backend/tests/test_quota_management.py backend/tests/test_backend.py -q
```

Tests fence external network I/O and use temporary SQLite and fake source/Telegram
transports. Cases include budget waits across restart, short rolling-limit
recovery, truthful expiry, real transient failures, preview contradictions and
unknown fields, read-only diagnostics, paid delivery and owner-copy regressions.
These do not establish live market recall, source availability or phone delivery.

A bounded public-page observation on October 3 retrieved 100 parseable cards in
1,367,956 bytes after the allowlist robots check. It confirmed the optional
same-ID attribute shape. The raw HTML, seller contacts and user identifiers are
not repository fixtures or public evidence. One page does not prove coverage.

## Rollout and rollback

Publish an isolated fix branch only after checking deployment/workflow triggers.
After the existing owner-authorized delivery/source repair, fast-forward the
unchanged main head and verify the exact live release, private pipeline snapshot
and new status gate. Do not overlap a manual deployment with an automatic build.
Rollback is the previous application revision; no schema/data/env rollback is
required. Preserve paid-source restrictions and owner-copy receipts.
