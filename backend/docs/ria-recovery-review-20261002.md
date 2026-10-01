# RIA recovery review — 2026-10-02 Kyiv, NOT DEPLOYED

Base main 43084b2b7e8551e4cabff3ee3eb2fb90142dc402.
Reused existing code-only ab3feed090c030d4b1be33b6626611796623c3fe;
cherry-picked onto the current main as 289adc1. No new discovery mechanism,
filters, source cadence, quotas, formula or unknown-estimate delivery policy.
This is a rebased and newly verified existing fix, not a newly invented fix.

Prepared fixes:
- Overflow does not mark an unadmitted publication seen; bounded repeat can admit it
  after capacity returns. Regression: test_overflow_candidate_can_enter_after_capacity_returns_and_restart.
- Genuine newer addition date can be revisited under an old ID; mere price/update
  does not count. Regression: test_newer_add_date_retries_rejected_id_but_same_date_reprice_does_not.
- Stale preview price cannot filter out matching fresh API details. Regression:
  test_preview_price_does_not_hide_fresh_matching_current_api_price.
- Fresh jobs retain pending on transient valuation errors, max 3 tries / 600 sec,
  durable backoff. Quota/lease pauses do not finalize a listing; permanent denial
  and unavailable range do not loop. See test_transient_quote_retry.py, including
  restart, /stop and two-user shared recovery. Unknown valuation remains explicit.

88 focused tests passed. Full current-base backend + free-search regression:
1225 passed, 110 subtests passed, one pre-existing Starlette/httpx warning, 150.04 sec.
All tests offline with network fence. No real paid API/Telegram calls in tests.

Read-only production audit and an independent bounded public-page observation were
performed separately. Private records/identifiers/user filters are not in this branch.
Per-user historical completeness cannot be reconstructed from aggregate log lines.
Real overflow/expiry counts are accumulated events, not unique confirmed missed deals.
The private delivery packet records evidence boundaries, counts and uncertainties.

Known remaining limits: two dated public pages, one-hour extra-intake age, 600sec
primary overlap and quota headroom still bound recovery. Fixing seen admission does
not prove whole-market recall or recover expired historic listings. No old-base scan.
Before any deployment: owner approves this exact diff separately from payment/OLX,
then a bounded canary with explicit metrics and rollback retaining delivery claims.
