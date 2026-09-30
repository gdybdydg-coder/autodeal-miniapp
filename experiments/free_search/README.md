# Isolated free-search experiment

Not imported by the backend and not deployed. No network, production database,
API credentials, jobs or Telegram deliveries. Python 3.10+ standard library only.

Run synthetic tests from repository root:

```sh
python -m unittest discover -s experiments/free_search/tests -v
```

`parse_public_details(html, expected_id)` reads public `application/ld+json`
Vehicle/Offer assertions from caller-supplied HTML. Returns an immutable
allowlisted result with exact Decimal prices, absent fields, invalid-value issues,
and `public_jsonld` provenance. Canonical, Vehicle and optional Offer URLs must
agree with the expected listing ID on HTTPS auto.ria.com; conflicts fail closed.
Missing optional fields and damage are not exclusion conditions. No currency or
mileage-unit conversion is invented. Unknown stock state stays unknown.

Always `ready_for_delivery=False`: this parser proves neither fresh publication,
passenger category, region, abroad/custom eligibility, actual availability,
freshness of price nor profitable valuation. JSON-LD values may be stale. Never
use Vehicle/Offer presence alone to send an alert or replace official AI pricing.
Seller/VIN/contact/description/image content is not returned or persisted.

Input limits: HTML 2.5MB, JSON-LD script 256k characters, 64 scripts, 500 traversed
root/list/graph nodes, 20 offers per Vehicle. Invalid JSON, duplicate keys,
nonfinite/invalid prices and conflicting assertions reject the sample. Parser
handles root arrays and @graph, not arbitrary nested recommendation objects.
Foreign Vehicle nodes in the same root/graph conservatively reject the sample.
Schema variations are deliberately unresolved, not guessed.

Future fetcher must independently enforce allowed public paths, robots/terms,
no redirects/cookies, request reservations, source backoff and bounded retries.
This module has no fetcher and no paid fallback.

Checkpoint: ../../backend/docs/zero-paid-api-parser-2026-10-01.md
