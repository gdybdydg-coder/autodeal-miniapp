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

`parse_public_cards(html)` is a separate bounded, offline parser for public
listing cards. It keeps the add date distinct from the update date and treats
the preview USD value as non-authoritative. Promotion markers, duplicate IDs,
bad links, ambiguous dates and conflicting preview prices are explicit issues.

`advance_publications(state, cards, observed_at)` establishes a no-send first
baseline, accepts only add dates after that baseline and within a one-hour
window, and persists each ID's last accepted add date. A later update timestamp,
preview price change, first appearance or raising action does not create a
candidate. A genuinely later add date can admit a repeated old ID. State has a
strict serializable round trip for restart-safe offline tests.

Candidates only authorize a later public-details/category/filter check. They do
not contain valuation or delivery decisions. The parser does not establish
whole-market coverage and does not fetch old pages.

`filter_gate.py` is a pure offline safety gate for normalized public evidence.
It preserves saved-filter semantics: required publication/category/active/
positive-price facts must be proven, a selected region must match, known
optional facts must match, but missing optional vehicle details do not hide a
car. Known abroad/customs exclusions block; damage and repair-parts markers are
notices only. A match authorizes valuation research and is never delivery-ready.

The current service terms contain both an allowance for automated processing of
non-phone public data and a broader anti-parsing clause. The experiment treats
that ambiguity as a go-live blocker, regardless of robots. See checkpoint:
../../backend/docs/zero-paid-api-source-audit-2026-10-01.md

`visible_adapter.py` is fixture-only. It extracts exact pre-title category and
oblast labels plus the visible creation date and listing ID, then joins them to
the existing add-date candidate and JSON-LD detail result. Footer links,
conflicting evidence, identity/date mismatches, non-USD price and inactive or
unknown availability fail closed at the appropriate stage. Old IDs are not
rejected merely for being old when a genuine later add date passes baseline and
deduplication. Update/reprice/appearance still cannot create publication proof.
Checkpoint: ../../backend/docs/zero-paid-api-visible-adapter-2026-10-01.md

`offline_queue.py` models bounded shared jobs, current activation epochs,
per-search seen rows and durable per-user/listing delivery claims. It rechecks
`/stop`, enabled state, epoch and activation time both after shared evaluation
and before hypothetical send I/O. Restarts retry evaluation but convert an
in-flight send to `uncertain`, never to a replayable pending claim. Any existing
claim state remains the final dedupe authority. The module has no sender and
cannot deliver. Checkpoint:
../../backend/docs/zero-paid-api-offline-queue-2026-10-01.md

`http_budget.py` is a network-free state machine for a hypothetical shared
fetcher. It requires explicit terms, robots and public-route approval; reserves
rolling request and byte budgets before hypothetical GET I/O; forbids redirects
and cookies; and bounds queue, in-flight work, body size, retries and backoff.
429 pauses the source, HTTP denials persistently block it, and a restart retains
the full uncertain reservation charge before any delayed retry. Test budget
values are synthetic and do not authorize live collection. Checkpoint:
../../backend/docs/zero-paid-api-http-budget-2026-10-01.md

`offline_pipeline.py` composes the isolated stages with sanitized fixtures:
baseline/add-date proof, one transactionally queued detail reservation, public
detail/category/region evidence, current saved filters, one shared research
estimate and durable claims. Queue overflow rolls publication progress back;
terms denial, 429 and budget pauses create no evaluation or claim. Restarts keep
pending work, full uncertain HTTP charges, stopped users, epochs and dedupe.
Fresh old-ID publication can be checked again but cannot repeat an existing
user/listing claim. The estimate is caller-supplied offline research only and
cannot send or replace paid AI. Checkpoint:
../../backend/docs/zero-paid-api-offline-pipeline-2026-10-01.md
