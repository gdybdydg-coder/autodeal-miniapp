# Dated public-publication fallback

Opt-in `RIA_RECENT_PUBLICATIONS_ENABLED=true` adds a shared, bounded fallback to
the existing official date-window searches. It does not enable the old active
window, scan the historical catalog, or reevaluate an observed car after a price
change. The September 30 user decision explicitly includes fresh repeat
publications under an old ID, subject to the same durable deduplication.

## Evidence and baseline

The public collector uses only the two existing allowlisted `/uk/last/hour/`
pages and robots.txt, with the existing bounded transport. It creates no
MonitorJob, quote or delivery. Its first successful two-page snapshot is a
durable baseline and sends nothing. Reenabling after disabling creates a new
baseline; restarting an enabled process retains its existing baseline/queue.

An eligible candidate has a nonpromoted used-car card with a valid listing URL
and a separate addition date strictly after the baseline and at most one hour
old. Update dates, appearance in last/hour, ID age and first observation are not
publication evidence. Conflicting addition dates across pages discard that ID;
changed preview prices remove only the preview assumption. Category is confirmed
through `autoData.categoryId == 1` in official details, not through the global
public page, which also contains motorcycles and commercial vehicles.

Naive addition dates use an **explicit Europe/Kyiv source-clock interpretation**,
consistent with the live public samples. The official API documentation does
not specify a timezone for naive addDate. Both HTML and official API addition
values must resolve to exactly the same second. Ambiguous or nonexistent DST
times are rejected. This assumption is exposed in operator diagnostics and is
not used to invent publication-to-phone latency.

The idle monitor checks official active/unsold status, current positive USD
price, the matching addition date, category, saved filters and activation
epochs. Only matching interests become one shared normal valuation job. Missing
optional characteristics and damage/repair do not exclude a car; known filter
contradictions and existing abroad/custom exclusions do. The existing official
AI quote, lower bound × 0.95, minDiscount and confirmed-only policy apply.

Any existing monitor job for the ID prevents additional intake. Existing seen
records and all recipient delivery claims, including sent/uncertain, are kept.
Stopping or editing a subscription during either details or quote invalidates
its recipient epoch. A late subscriber receives no earlier publication. Expired
or disabled HTML deliveries are cancelled rather than refreshed as old inventory.
Primary API confirmation may promote the job to the normal publication source.

## Bounds and priority

- Collector cadence in Kyiv: 23:00–08:00 3600s, 08:00–18:00 110s,
  18:00–23:00 60s. Ordinary API polling cadence is unchanged.
- Two public pages per collection. Durable HTTP caps: 125 requests/hour,
  1600/day and 3 GiB/day, including reserved failed/crashed requests. Robots
  permission is rechecked every six hours. Denials stop fetching; 429 uses
  Retry-After with at least 900s backoff. No cookies, redirects, hidden endpoints,
  CAPTCHA workarounds or IP rotation.
- Maximum 128 waiting candidates; an explicit overflow counter records drops.
  Processed IDs are retained for two hours with a hard 10000-ID bound. No old
  page pagination or retrospective recovery.
- Each fallback API request reserves against both the unchanged global durable
  ledger and an extra rolling cap of **200/hour and 1000/day**. Keep at least
  20% of hourly/daily provider capacity for primary work. Cached responses do
  not consume either budget.
- Primary due searches and valuation jobs take scheduling priority. Intake uses
  one otherwise idle monitor step. Details are shared across subscribers;
  valuation shares the existing cache/queue and four-operation source limit.
- Collector has one worker and one dedicated DB connection and yields to bot
  backlog or memory pressure. HTTP waits hold no DB transaction. Persisted
  ownership, reservations and latest-state merging fence overlapping processes.

The extra cap is a ceiling, not an estimate of real consumption or provider
balance. This does not claim net savings against the already-disabled active
window. It avoids adding one paid fallback search per saved filter.

## Diagnostics and validation

`/api/recent-publications-status` exposes only aggregates: baseline, cadence,
queue, consumed HTTP/API capacity, verified jobs, duplicate/expiry/filter/date
rejections and overflow. No seller details, VINs, credentials, recipient IDs or
raw HTML are retained by the collector.

The independent old HTML shadow remains observation only and is not promoted
into this source. Its overlap counts do not establish complete market recall.
The two-page fallback can miss publications outside its sampled pages, during
source denial, budget pressure, overflow, missing date/category evidence or
long downtime. It complements the official per-filter search; it does not
replace it or guarantee all listings.

Selected tests cover genuine dispatcher fan-out, old-ID republication, initial
and restart baselines, promotions/old update dates, official date/category/price
and filter failures, missing optional data, condition flags, /stop, late
activation, sent/uncertain claims, queue and API/HTTP caps, concurrent collection,
primary priority, proof-preserving stale-price refresh and confirmed-only
withholding. Full follow-up validation: **937 backend tests passed**, including
57 selected fallback/diagnostic tests. The remote tree must match this tested
local tree before main is advanced without force.

The selected incident diagnostic also retries only unclaimed probe stages on
six otherwise idle monitor ticks when a startup source lease was busy. Existing
per-stage durable claims and request caps remain unchanged; no diagnostic
creates jobs, valuations or messages.
