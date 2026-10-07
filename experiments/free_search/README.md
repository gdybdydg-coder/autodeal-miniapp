# Isolated free-search experiment

## 2026-10-07 evidence continuation

### Stage 1 pagination review

`public_pagination.py` inspects both observed script attribute spellings for
page index/size, a digest of the remaining query scope and the visible next
link. `bounded_probe` now reports `coverage` separately from HTTP completion.
Changing page sizes, changing declared scopes, gaps, ambiguous metadata or an
unobserved terminal page keep coverage `incomplete`. A 20-card page is never
itself proof of the end. Even a consistent declared end is not an atomic
snapshot or whole-market completeness. Implicit versus explicit query defaults
remain unresolved; the checker does not invent equivalence or alter URLs.

The checker is diagnostic only: it adds no network routes, pagination loops,
production integration, request-budget increases or automatic retries. Raw live
HTML and customer diagnostic data are not committed. Independent control-card
availability and page absence must be reported separately; the mechanism above
does not prove the exact historical reason for any particular missing ID.

`evidence_audit.py` separates unknown publication time, local public collection,
and passive Render paid-production delivery timings. It reports each queue stage
without substituting polling intervals or first detection for publication time.
It rejects noncausal/nonfinite timestamps, refuses a collector's own output as
an independent control population, and retains absent control IDs explicitly.
Even independent sample overlap is never represented as whole-market recall.

`public_only_valuation` accepts only a fresh-public-probe contract and passes
an explicit allowlist of public detail fields to the prior research estimator.
It cannot import paid cache labels. A marker alone is not external attestation;
review the acquisition code and preserve the receipts. Unknown valuations do
not create messages. The asking-price estimator remains uncalibrated and is
not a replacement for the production lower-bound-minus-5-percent formula.

The bounded probe now rejects production credential configuration before I/O,
records monotonic HTTP durations, wall-clock receive/parse timestamps, response
body hashes and available cache headers. It still retains no raw seller data,
follows no redirects and stops on denial; at most five requests per invocation.
An explicit alternative may inspect page 3 without detail requests:

```python
from experiments.free_search.bounded_probe import run, ALLOWED_FEEDS
result = run(feed_urls=(ALLOWED_FEEDS[2],), detail_limit=0)
```

This is not an automatic pagination/cadence expansion or a cumulative budget
manager. The caller must bound the whole session. Private live measurements
stay outside this public repository. Run the complete standard-library suite:

```sh
python -m unittest discover -s experiments/free_search/tests -q
```

Continuation priorities: independent fixed-window control sampling; investigation
of missing/reordered/expired page items; complete cold-start public peer baskets
with held-out targets; source appearance bounds across repeated observations;
only then a separately approved isolated Render run. Do not merge the whole old
WIP branch into current main: it carries historical backend changes. Any future
integration must preserve the then-current paid access, /stop, user filters,
schedule, valuation rule, dedupe and disabled administrative automobile copies.

This branch is a code-only review candidate. Production diagnostic exports,
listing snapshots, private audit reports and measured live evidence are not
included. Nothing imports this experiment into the production backend.

Python 3.10+ standard library is sufficient for the isolated experiment:

```sh
python -m unittest discover -s experiments/free_search/tests -q
python -m experiments.free_search.replay_demo --users 100 --searches 5
python -m experiments.free_search.replay_demo --users 200 --searches 5
```

For the complete backend regression, install `backend/requirements.txt` in an
isolated virtual environment, then run without production credentials:

```sh
python -m pytest backend/tests experiments/free_search/tests -q
```

The existing backend test suite fences socket/DNS I/O. Source APIs and Telegram
must be represented by fixtures. Do not start the backend app for this replay.

## Components

- `public_cards`: explicit add-date proof, a no-send baseline, bounded recent
  overlap and per-ID/add-date deduplication; no max-ID-only cursor.
- `public_details`: allowlisted JSON-LD facts, currency and mileage units.
- `visible_adapter` and `filter_gate`: publication/category/region proof,
  brand/model, multiple regions, known optional conflicts, missing optional
  fields permitted, damage/parts markers never an exclusion by themselves.
- `http_budget`: durable reservations, bounded backoff, source denial and 429.
- `offline_queue`: shared evaluation and per-user/listing claims, activation
  epochs, /stop, explicit uncertain outcomes, no blind ambiguous-send retries.
- `offline_pipeline`: atomic multi-page fixture intake and rollback on overflow;
  parsing failures retain bounded retry state and recipient claim overflow
  leaves unscheduled work pending.
- `replay_store`: an isolated SQLite state and event journal. A foreign SQLite
  database is rejected; no production environment variable is read.
- `replay_demo`: 120 independently predefined synthetic IDs across two pages;
  one shared detail and synthetic research estimate fan out to up to 200 users
  with multiple searches. The other 119 details remain pending. It exercises
  restart, /stop and mocked API acceptance. It sends nothing.

An optional persistent replay database must have a new experiment-only path:

```sh
python -m experiments.free_search.replay_demo --database /tmp/demo.free-search.sqlite3
```

## Bounded source diagnostic

`bounded_probe` is the only network-capable module. It performs at most five
public GET requests: robots, two observed public feed routes and two returned
detail links. It cannot reach paid API or Telegram hosts, follow redirects or
use cookies. Denial/429 stops it. It emits only allowlisted observations.

```sh
python -m experiments.free_search.bounded_probe > /tmp/public-probe.json
```

It is not a continuous collector. Check current source terms and robots before
operational use. Public field presence does not prove freshness, full coverage
or availability of a free authoritative market estimate. A link census only
checks extraction from the observed pages, not recall of the whole service.

No paid valuation fallback or real sender exists. Without a trustworthy market
reference, a candidate remains pending/unvalued and cannot authorize delivery.
A caller-supplied research estimate is explicitly synthetic in the demo; it is
never represented as AUTO.RIA valuation. Production lower-bound-minus-5-percent
pricing and the separate user discount threshold remain unchanged.

## Backend changes for review

The small backend diff retains fresh jobs across bounded transient valuation
failures and quota pauses. It also carries forward the prior WIP fixes for
publication queue overflow, genuinely newer add dates and stale preview prices.
Review before any deployment; this branch does not change schedules, environment,
payments, webhooks or subscriptions. Existing delivery claims must not be reset.

## 2026-10-02 continuation (separate review branch, offline only)

The 2026-10-01 experiment is retained, including its 150 existing tests and 110
subtests. New modules extend it; the original `replay_demo` remains a historical
single-detail demonstration and is not the new full-flow load result.

- `durable_pipeline.py`: SQLite WAL + FULL synchronous writes; shared detail and
  valuation work, durable scan/page cursor, atomic page acceptance, capacity
  backpressure without marking unqueued cars processed, recent-window late IDs,
  re-publication evidence/history, per-car/user dedupe, and explicit reasons.
- `network_guard.py`: all callback sockets and DNS are blocked by default;
  configured production credentials reject execution. Zero successful external
  calls is an offline boundary, not evidence of live source availability.
- `valuation.py`: independent comparable *asking* prices with explicit
  uncertainty and provenance; see `VALUATION.md`. No production appraisal rule
  changes and no paid/cache fallback. Suspicious prices cannot create claims.
- Detail callbacks must return a current `observed_at` timestamp. Existing public
  detail/card/visible parsers can prepare normalized fixtures; unknown dates,
  selected regions or source evidence stay unresolved rather than successful.
- Known transient detail/valuation/delivery failures back off, then retain an
  unresolved record at the retry limit. Timeout delivery remains `uncertain`;
  recovery never blindly resends it. Known photo rejection can retry as text.
- Each simulated send rechecks subscription expiry, search filters and `/stop`.
  Optional `clock` allows time to advance between recipients. Changing access
  alone never re-enables a stopped search.

Run the new durability tests without production credentials:

```sh
python -m unittest discover -s experiments/free_search/tests -p test_durable_pipeline.py -q
python -m pytest experiments/free_search/tests -q
```

New durable files must end in `.free-pipeline.sqlite3`; an existing foreign
SQLite database is refused. No production database configuration is read. The
public methods are documented in `durable_pipeline.py`, and the executable
fixture scenarios are in `tests/test_durable_pipeline.py`.

The overlap floor is mandatory input, not an invented live tuning value.
Page-budget exhaustion and repeated pages mark scans incomplete. A resumed
page containing a different snapshot requires a new overlapping scan. Reaching
an observed source end still does not prove whole-source coverage. No ongoing
public fetcher, real Telegram sender, automatic unresolved replay or production
cutover is included. Requests denied by source terms/CAPTCHA/limits must never be
worked around. Archived unresolved and completed rows need a measured retention
policy before a long-running deployment; this offline prototype does not delete
them to make a queue appear empty.
