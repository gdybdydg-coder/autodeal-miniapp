# Zero-paid API: public-source audit and filter gate

Checkpoint: 01.10.2026, about 03:40 Europe/Kyiv. This is an isolated WIP
experiment. Production `main`, Render, paid API/AI, jobs, subscriptions,
delivery claims and Telegram were not changed.

## Official terms and bounded observation

The footer of the public AUTO.RIA page links to the current service terms:
<https://oferta.ria.com/auto>. Sections 1.6.1-1.6.5 say automated processing of
non-phone public data is permitted subject to the agreement and law. Sections
1.16-1.17 also contain a broad prohibition on parsing/copying the platform
database. These clauses are not treated as permission for a continuous
commercial collector. Written clarification from AUTO.RIA is a go-live gate;
robots alone cannot resolve the contractual ambiguity.

One bounded observation used the public first page for listings added during
the last hour plus three ordinary detail pages: two passenger cars and one
motorcycle. No API, login, cookies, hidden endpoint, CAPTCHA bypass, seller
contact extraction, raw HTML retention or production job was used.

Observed public evidence:

- the feed exposes a timestamp, positive displayed price, city and a detail
  link, but it mixes passenger and non-passenger transport;
- passenger detail pages visibly exposed a passenger breadcrumb, oblast/city,
  asking price, body/engine/transmission facts and `Оголошення створене` date;
- the motorcycle sample lacked the passenger breadcrumb and was therefore a
  useful negative category sample;
- one passenger page visibly showed import/customs statements, but that does
  not prove a stable structured customs field across listings;
- visible page presence and JSON-LD still do not prove that a price is fresh or
  that the listing remains active between polls.

This sample proves field presence, not two-page coverage, parser stability or
permission for continuous collection. It is deliberately too small for any
claim about savings, recall or replacement of the paid source.

## Offline gate added

`experiments/free_search/filter_gate.py` receives normalized evidence only and
has no network/backend/database imports. It requires genuine publication proof,
passenger category, active state and a positive exact Decimal price. A selected
region must be proven. Known price/year/body/fuel/transmission/mileage conflicts
block. Known abroad/customs exclusions block. Missing optional vehicle details
do not block; damage and repair-parts markers are notices only.

The gate can return `eligible_for_valuation=True`, never
`ready_for_delivery=True`. `minDiscount` and `confirmed_deals_only` still need a
separate validated market estimate, so no free-source candidate can be sent.

Verification: 12 new filter-gate tests passed. Together with the existing
public detail and publication tests, 65 isolated tests passed. No full backend
regression was claimed because this module is intentionally not imported by
the backend.

## Next bounded step

Build a fixture-only adapter for explicit visible category/region/creation-date
evidence, with conflicts and schema changes failing closed. Do not start a live
collector until the terms ambiguity is resolved. Then evaluate whether a
fresh-page observation can consistently distinguish active, abroad and customs
states without descriptions or personal data.
