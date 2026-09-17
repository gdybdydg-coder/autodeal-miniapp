# Step 3: worthwhile-price classification — release 20260917-33

This review replays previously retained real AUTO.RIA responses. It makes **zero
new provider requests** and is not a current appraisal of those vehicles. Neither
the exhausted `eligible-20260917-1` audit nor its 96-call budget is restarted.
The machine-readable [review](valuation-review-2026-09-17.json) records input
file hashes, observation times, candidate outcomes and comparable listing IDs.

## Classification and evidence

`strict-v2` requires at least five comparable vehicles with the same official
brand, model, generation, modification, body, fuel and transmission identifiers.
Year tolerance is ±1; mileage tolerance is ±max(30,000 km, 20%). Undamaged,
active, domestic, customs-cleared condition must be explicit in source details.
Missing IDs are not guessed; the existing exact full-label modification resolver
remains available. User budget and region do not restrict the comparison sample.

Details must be no older than 15 minutes. Source IDs exclude self and duplicate
listings. A full, unmasked VIN is hashed solely to collapse known relistings;
raw VINs and seller contact data are not retained. Unknown or masked VINs cannot
establish vehicle identity, so undetectable relistings remain a limitation.
A sample with a maximum/minimum price ratio above 2 is unvalued.

The decision is `price <= median * 0.85` using decimal values, independently of
the rounded display discount. Outcomes are `deal`, `not_deal` and `unknown`.
Evidence contains observation times, comparable prices and IDs, rejected-peer
reasons, sample size, median, threshold and lookup cost. An unfinished or capped
comparison is never labeled an ordinary price or a confirmed bargain.

## Real-data replay

| Retained sample | Price | Median | 15% threshold | Result at the recorded time |
| --- | ---: | ---: | ---: | --- |
| Passat 39378439 | $14,400 | $13,300 | $11,305 | Ordinary price |
| Passat 40444164 | $8,700 | $11,700 | $9,945 | Qualifying deal (25.6% below median) |

The first audit has 24 candidate observations: 1 ordinary price and 23 unknown;
one check was unfinished. The eligible-condition audit has 24 observations:
1 deal and 23 unknown, including 16 unfinished checks. These are separate audit
observations, not a claim of 48 distinct vehicles. The two successful medians
have independently recalculated evidence. The same archived observations are
rejected as stale when assessed after their freshness window.

This small, selected sample does **not** establish live coverage, prediction
accuracy, resale profit, or reliable valuation across Audi/Mercedes variants.
Step 4 must measure fresh eligible/unknown outcomes, latency and actual API cost
on the owner's opted-in subscription. The app states that these are asking
prices, not completed sale prices or a guarantee of vehicle condition.

## Reuse, delivery and budget

The additive `valuation_peers` table retains sanitized comparison observations.
Only cars fetched for peer comparison seed the sample; monitored cheap candidates
do not automatically become the market reference. Selection uses matching
attributes, year/mileage distance and freshness, never the cheapest prices.
Expired observations are ignored and purged on subsequent writes. Known price
changes, invalid details and removals supersede older cached observations.

In a deterministic test, the first valuation needs 1 peer search and 5 detail
requests. A similar subsequent candidate reuses the same fresh peers with **0
additional comparison requests**, including after process restart. Discovering
and fetching the candidate still costs requests. A four-request interruption
resumes with only two more requests; its counters never reset. These are fixture
measurements, not a forecast of production savings or universal coverage.

Before dispatch, the candidate must be no older than 5 minutes and its price
proof must still recompute and remain current. Missing/old proof, stale peers,
and known changed peer prices return the listing to the durable valuation queue,
even if it has not yet reached the delivery queue. Existing consent, ownership,
stop and per-user/listing deduplication remain effective.

Verification: **178 backend tests and 59 frontend checks pass**. This includes
threshold rounding, bad/missing attributes, VIN relistings, stale/future prices,
mixed samples, restart/cache reuse, budget interruptions, and queued repricing.
The Mini App exposes the last unknown-reason category and explains the policy in
Settings. `/api/source-status` reports `valuation_policy.version = strict-v2`.

Deployment leaves monitoring, delivery and mass scans disabled. No subscription
is activated, no message is sent, and the hourly/daily/lifetime caps are unchanged.
