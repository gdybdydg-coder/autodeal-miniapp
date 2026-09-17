# Valuation audit — 2026-09-17

Result: **partial; the wider valuation launch check remains open**. The
production parser, comparator and median calculation were exercised on 24 real
AUTO.RIA candidate listings across three models. One candidate had enough eligible
peers for a median. All completed independent recalculations agreed with the
production estimator; this does not establish complete coverage or true resale
value. No candidate was confirmed as a deal in this bounded sample.

Run `popular-20260917-1`, profile `popular-v1`, completed at
2026-09-17 12:52:55 UTC. Backend commit:
`da1ad67804a328bdc1021db921f19ea8b2895183`.
[Sanitized historical evidence](valuation-audit-2026-09-17.json) preserves candidate
and peer IDs, asking prices, relevant attributes and rejection reasons. These
observations are not current prices; no provider call is needed to inspect them.

## Coverage and request use

| Model | Candidates | Medians | Provider calls | Limitations |
| --- | ---: | ---: | ---: | --- |
| Volkswagen Passat | 8 | 1 | 32 | 5 ineligible candidates; 1 with only 4 eligible peers; 1 valuation pending when the per-model call cap was reached |
| Audi A6 | 8 | 0 | 10 | All 8 candidates ineligible for the current condition requirements; 5 also lack modification ID |
| Mercedes-Benz E-Class | 8 | 0 | 13 | 6 ineligible candidates; the other 2 have no eligible peers in the bounded peer sample |
| Total | 24 | 1 | 55 | 19 ineligible, 3 with insufficient peers, 1 pending, 1 valued |

The cap was 32 calls per model and 96 overall. The run never retries automatically,
including after a restart. The request count includes failed upstream attempts;
it is not an independently verified AUTO.RIA package balance. The provider did
not return the allowlisted quota headers in this run.

## Verified calculation

Volkswagen Passat 2016 [#39378439](https://auto.ria.com/auto_volkswagen_passat_39378439.html)
had an asking price of **$14,400**. Five other eligible listings had sorted asking
prices **$12,500, $13,200, $13,300, $13,850 and $14,500**. Their median is
**$13,300**. The exact 15%-below-median threshold is **$11,305**, so this candidate
correctly does not qualify as a deal. Five other retrieved peers were rejected
for a missing or different modification ID.

Eligible peers share make, model, generation, modification, body, fuel and gearbox;
year differs by at most one and mileage by at most 30,000 km or 20% of candidate
mileage, whichever is larger. All must satisfy the existing condition, location
and customs requirements. The candidate itself and repeated IDs cannot inflate
the sample. No buyer price ceiling or regional filter constrains the peer query.

## What the missing data means

Of 24 candidates, 16 had no usable technical-condition ID, 3 were explicitly
marked as professionally repaired, and 5 were marked undamaged. The current
estimator accepts only the latter condition, together with explicit compatible
flags and all required vehicle identifiers. Missing condition and missing
modification overlap; they must not be added as separate candidate counts.

The [official detail contract](https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info)
places `technicalCondition.id` at the top level and vehicle identifiers under
`autoData`, matching the parser. It distinguishes condition 1 (undamaged) from
condition 2 (professionally repaired), 3 (unrepaired damage) and 4 (not running /
parts). The [search contract](https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto)
also documents the nested generation/modification and condition parameters used
by the peer query. Both contracts were inspected on 2026-09-17.

This inspection found no documented field-path mismatch. It does not prove the
provider populates every field or honors every search parameter. Returned peers
still require local attribute checks. A missing condition is not evidence that
a vehicle is undamaged, and fewer than five peers are not evidence that there
are no bargains on the market.

## Remaining work before enabling real-car delivery

1. Improve the number of usable peer comparisons. Investigate a bounded fallback
   using explicitly available technical attributes for missing modifications,
   and keep unknown or repaired condition separate from undamaged vehicles.
   Do not silently weaken the current deal criterion to increase coverage.
2. Validate any changed matching policy against these retained examples, then
   check suitable real Audi and Mercedes candidates with a fresh, capped run.
   An actual below-threshold case still needs live review; synthetic boundary
   tests alone do not validate a real bargain.
3. Confirm a real-car notification reaches the owner's private Telegram chat,
   including deduplication, pause/stop and refresh-before-send behavior, before
   broadening the one-search pilot.

All 100 backend tests passed for the deployed audit code. The audit itself made
no listing/delivery inserts and sent no Telegram messages. Monitoring and automatic
delivery remained off throughout this run; no new paid service was created.
