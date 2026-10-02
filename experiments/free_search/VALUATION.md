# Experimental valuation by comparable asking prices

`valuation.py` is an offline research module. It is **not AUTO.RIA's appraisal**,
not evidence of sale prices, and not approved for production. It contains no
network, backend or database client. No paid appraisal is read by its estimator.
The existing production rule (AUTO.RIA lower bound minus 5%) is unchanged.

## Interface

```python
from experiments.free_search.valuation import estimate

outcome = estimate(subject, peers, as_of)
result = outcome.as_dict()  # JSON-safe; amount/discount values are decimal strings
```

`subject` and each peer are mappings with `listing_id`, `brand`, `model`, `year`,
`price`/`currency` (or explicit `price_usd`) and `observed_at`. Timestamps are Unix
seconds or timezone-aware datetimes. Optional fields: `generation`, `engine_cc`
or `engine_liters`, `fuel`, `transmission`, `body`, `mileage_km`, `region`,
`price_observed_at`, `publication_at`, `duplicate_key`, `title`, `price_context`,
`price_kind`, `whole_vehicle`, `damaged`, `repair_parts`.

Adapters must supply normalized categorical fields and explicit units. In
particular, engine litres and cubic centimetres are separate named inputs;
mileage is kilometres. The module does not guess exchange rates, translate
arbitrary generation names or assume that a page fetch proved a publication date.

`status` is `estimated` or `unknown`; `classification` is
`plausible_undervalued`, `not_undervalued`, `suspicious_price` or `insufficient_data`.
The result includes `reason`, `reference_price`, `reference_price_usd` (USD only),
`discount_percent`, comparable count, age range, exclusions and missing-field
notices. `production_approved` is always false and confidence is `uncalibrated`.
The durable pipeline must retain unknown results with their reason and retry or
review them explicitly. Unknown is neither a proved bargain nor a negative label.
Per-user discount thresholds are applied after this shared estimate, separately.

## Fixed research policy

- A subject needs a known brand/model/year, a positive supported-currency price
  and an observation at or before `as_of`, with a price at most one day old.
- Peers are at most 30 days old. These are conservative research defaults, not
  optimised source-discovery overlap intervals or measured market-price decay.
- Brand/model must match; year is within one year. Known generation, fuel,
  transmission and body conflicts are excluded. Engine differences exceed
  `max(100 cc, 10%)` and mileage differences exceed `max(50,000 km, 35%)` are
  excluded. Missing optional details are disclosed, not an automatic exclusion.
- At least five comparables are required. A sufficiently large same-region
  basket is preferred; otherwise the regional mix is disclosed. No invented
  regional or equipment price correction is applied.
- The latest known observation per listing is used. Explicit duplicate-vehicle
  keys count once; self/same-vehicle observations cannot price themselves.
  Equal-time conflicting observations are unresolved. Identical-looking cars
  without evidence of shared identity cannot reliably be deduplicated; this is
  a remaining data limitation.
- Median absolute deviation with a conservative minimum band removes extreme
  peer outliers. Large remaining interquartile spread gives an unknown result.
  The experimental reference is the lower asking-price quartile. The production
  `lower bound × 0.95` formula is not copied into this independent estimator.
- Explicit advance-payment, monthly-payment, individual-part and placeholder
  amounts are uncertain. Finite Ukrainian/Russian/English text patterns are a
  heuristic, not proof that a seller's amount is genuine. Very tiny absolute or
  extreme relative prices are retained for review. Damage or selling an entire
  vehicle for parts is never itself an exclusion. Missing photos are irrelevant.
- Missing live source permission, free data coverage or a sufficiently dense
  legally collected peer basket is not solved by this pure calculation module.

These thresholds were fixed before the historical comparison. They are neither
trained nor claimed calibrated. Broadening comparables to reduce unknowns needs
a separately held-out evaluation; unknowns must not simply be relabelled bargains.

## Reproducible checks

```bash
python3 -m unittest experiments.free_search.tests.test_valuation -v
python3 -m unittest experiments.free_search.tests.test_valuation_benchmark -v
python3 -m experiments.free_search.valuation_benchmark \
  --targets /private/historical-valuation-fixtures.json \
  --peers /private/historical-peer-features.json \
  --output /private/valuation-aggregate-results.json
```

The benchmark CLI accepts privately supplied observations. It never downloads
inputs. It passes asking-price features into the estimator and keeps historical
provider quotes as comparison labels outside it. It excludes target IDs and
peers observed after the target. Output contains aggregates, not listing/user IDs.

The `--targets` schema is `{items: [{source_id, features, asking_price_usd,
candidate_observed_at, historical_label: {lower_usd, quote_observed_at}}]}`.
Features use same-source names for brand/model/transmission/fuel/region/body,
`generation_id`, `engine_cc`, and `mileage` in kilometres. A fuel string may have
a comma-separated displacement suffix; the benchmark strips that suffix because
engine displacement is compared separately. `--peers` can use the same schema
(without labels) or normalized estimator inputs under `items`.

`evaluate_labeled_cases` reports FP, definite FN, unknown-positive and
unknown-negative separately. Opportunity recall includes unknown-positive cases
in the denominator. Empty precision/recall denominators are `null`, not 100%.
Historical agreement measures resemblance to the prior provider, not whether a
car was really undervalued or sold at that price.

## Measured limitations, 2 October 2026

A privately retained snapshot supplied 2,000 deterministically time-stratified
targets (61 brands, 487 brand/model combinations), observed between
2026-09-29 12:14:25.464065 UTC and 2026-10-02 08:33:20.767960 UTC. Using only those
2,000 observations as peers left all targets unknown: the per-model/time sample
was too sparse. This is not evidence of source-wide coverage.

The next check kept the same model and target sample, using 40,435 retained peer
observations from 17 September–2 October, restricted to observations preceding
each target. It estimated 726 targets and retained 1,274 unknown (63.7%). At a
10% discount comparison threshold against the historical provider lower bound
multiplied by 0.95, counts were TP=71, FP=38, TN=564, definite FN=53 and
unknown-positive=268. Agreement precision was 65.14%; opportunity recall including
unknowns was 18.11%. Median absolute reference difference was 7.54%, **only for
the 726 estimated targets**. These results do not justify replacing production.

All those quotes/details were already accumulated production data. There were
zero new paid calls, external calls or Telegram messages in the benchmark. This
does not prove fresh free acquisition, a complete historical revision series,
true bargain labels, real source stability or a usable cold-start peer basket.
No private observations are committed to the repository.
