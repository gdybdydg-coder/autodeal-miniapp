# AUTO.RIA lower market boundary minus 5% — blocked integration draft

Requested 2026-09-18: stop valuing new notifications from our comparable cars.
Use the **actual lower boundary of AUTO.RIA's listing-specific market range**,
multiply it by **0.95**, and apply each saved minimum discount to that adjusted
reference. An average, percentile, cheapest comparable or overall model price
chart is not the requested lower boundary.

## Verified source findings on 2026-09-18

- The ordinary [listing detail API](https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info)
  documents listing price and attributes, without this market range.
- The [AI average-price API](https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai)
  is a paid method requiring `user_id`, `api_key` and an enabled permission.
  Its documented response contains `statisticData` of type `avgPrice` with
  `price.USD`. It does **not** document the requested lower/upper boundaries.
  Enabling or buying this method alone is not proof it supplies that range.
- The [older median API](https://docs-developers.ria.com/en/used-cars/average_price/median_average_price)
  is deprecated. Its percentiles describe a different statistic.
- The public Vito page (`40292766`) returned HTTP 200 with `averagePrice=5399`
  on 2026-09-18. It did not contain the lower boundary. The site's public
  `averagePrice` popup request returned HTTP 403. No alternate identity,
  credential, proxy or access workaround was attempted.
- No paid method was called, service bought, quota changed, private seller data
  retained or Telegram notification resent during this investigation.

## Prepared, locally testable part

`ria_market_range.py` validates an **internal** range contract (listing ID, USD,
explicit positive ordered lower/upper boundaries, observation time), applies
exact decimal multiplication by 0.95, and retains the source range in evidence.
Those internal field names are not an assertion about any provider wire schema.
Price/range freshness, identity, adjustment and condition notices are rechecked
before dispatch. Telegram displays the adjusted reference, raw lower boundary
and percentage below/above it. It does not label an above-reference price a gain.

The screenshot example 4168–4607 yields 3959.60, **not** 4607 × 0.95 or an
adjusted mean. Its listing price 4299 is 8.6% above the adjusted reference.
Screenshots are arithmetic examples only, not validated live source fixtures.

## Required before merge or deploy

1. Obtain an authorized supported source of the **listing-specific lower
   boundary** and a real sanitized response confirming field meanings, listing
   identity, currency and timestamp. Ask AUTO.RIA which method supplies the same
   range as the listing screen; do not assume buying the AI API resolves it.
2. Map that verified response to the internal contract; count/cache its requests
   within the existing budget and a bounded timeout. Do not retrieve secrets.
3. Switch monitor valuation to that adapter, remove notification-comparable
   requests, reject pending old-policy proofs, and test a live read-only quote.
   Missing ranges must preserve fresh informational alerts without inventing
   market prices. Preserve /stop, current epochs, saved discounts and all
   sent/uncertain claims; do not reprocess historical candidates.
4. Test the complete source-to-worker path, then deploy normally and verify.

This draft does **not** wire or enable a provider fetch, change the live monitor,
publish a new release, or claim a working AUTO.RIA valuation integration.
