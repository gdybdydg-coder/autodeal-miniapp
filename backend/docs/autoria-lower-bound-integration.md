# AUTO.RIA AI lower market boundary minus 5%

## Verified provider access on 2026-09-18

The owner's existing paid account returned HTTP 200 twice from the documented
[AI valuation method](https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai):

`POST https://developers.ria.com/auto/ai-avarage-price/`

Server-only query credentials: `user_id` and `api_key`. JSON body:
`{"langId":4,"period":168,"params":{"omniId":"40292766"}}`.
The method name really is spelled `avarage`. Two successful probes took about
11–12 seconds. Two other attempts timed out; the production timeout is bounded
at 20 seconds and does not retry within an evaluation.

Sanitized actual response (all seller/vehicle identity data discarded):

```json
{"statisticData":[{"id":"avgPriceBlock","type":"avgPrice",
 "price":{"USD":5423,"UAH":243425},"avgValueRange":0.05,"quantityAdv":261}]}
```

The ordinary detail API does not supply this valuation. The earlier public-page
popup HTTP 403 did **not** mean this paid API was inaccessible. No new service
was purchased, no access workaround used, and no Telegram recovery claim reset.

## Boundary and adjustment

The provider supplies an average **and** `avgValueRange`. We interpret the latter
as the fractional half-width of its symmetric market band:

- lower USD = floor(average USD × (1 − provider range fraction));
- upper USD = floor(average USD × (1 + provider range fraction));
- AUTODeal reference = lower USD × **0.95**;
- discount = (AUTODeal reference − asking price) / AUTODeal reference × 100.

Thus the live API fixture gives a band **5151–5694**, then an AUTODeal reference
of **4893.45**. These are derived values, not explicit lower/upper wire fields.
The additional 5% is an owner-selected adjustment, separate from AUTO.RIA's band
width. A different source width is respected; there is no fixed 10% reduction.

`avgValueRange` is present in the observed live response but omitted from the
documentation's example. Symmetric interpretation is consistent with the user's
listing screenshots (e.g. mean 3920 and width .05 gives 3724–4116); exact parity
with the native app has **not** been independently verified for the same listing.
The API uses a 168-hour observation period. App period, refresh and rounding may
differ. The concise card labels this "Ринкова ціна: ≈ $…" and the difference
"Вигода: …%". At the owner's request, the formula is retained in server evidence
rather than printed in the notification. The estimate is not an achieved sale price.

Missing, nonnumeric or invalid width/mean, missing USD, zero observations or
ambiguous average blocks yield no range. We never assume a default width,
substitute a percentile/cheapest peer, or use a general model sales chart.
Normalized evidence retains listing ID, mean, range fraction, observation count,
period, bounds and observation time; `similarCars`, seller IDs, VINs and raw
response bodies are not stored or logged.

## Runtime and delivery guarantees

`RIA_AI_PRICE_ENABLED=true` selects this policy for notifications. The existing
server API key plus `AUTO_RIA_USER_ID` authenticates one POST per uncached new
candidate, through the unchanged shared budget/lease. Cache lifetime is 60
seconds. Failed calls consume budget. Method-specific 401/403 does not block
ordinary publication discovery; shared quota protection still applies.

There is no notification-comparable fallback or old-candidate scan. If no usable
range is obtained, a freshly priced matching candidate gets an explicit
informational card. Priced cards apply each saved minimum discount after the
5% adjustment. A listed price above the reference is never described as profit.
Repair candidates disclose condition markers; parts-only/abroad/custom exclusions
remain. Candidate and range freshness, known changed/removed details, current
filter epochs and /stop are checked again at dispatch.

On upgrade, only pending or current unsent delivery interests can request a fresh
valuation. Finished historical jobs, stopped epochs, sent/uncertain claims and
once-only recoveries are not reset. The optional operator quote probe has its own
once-per-ID durable claim and cannot create matches, jobs or messages.

## Verification

Offline coverage includes the actual sanitized API shape, invalid/missing band
fields, non-5% widths, error redaction, no redirects, quota/cache sharing,
new-only discovery, no peer requests, per-subscription thresholds, outage cards,
/stop during valuation, old pending proof refresh, sent/uncertain deduplication,
and the once-only read-only probe. The screenshot arithmetic fixtures test the
owner's extra 5% separately; they are not reconstructed live listings.
