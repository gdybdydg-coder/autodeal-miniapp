# Public HTML observation pilot

Explicit opt-in: `RIA_HTML_SHADOW_RUN_ID=20260929-v1`. Empty/invalid ID disables
the task. The run ID is a durable experiment identity: restart/redeploy never
resets its deadline, counters, baseline or terminal state. Use the same ID when
re-enabling; a new ID starts a new experiment and needs an operator decision.

This first stage samples the first TWO public `/uk/last/hour/` pages every five
minutes for at most 24h from initialization. It is deliberately not a full-market
scan or a scoped regional recall experiment. Default ordering and promoted ads
can move between pages; first observation does not prove original publication.

## Isolation

- The only write target is one `SourceProbe` with prefix `html-shadow-v1-`.
- Store only listing IDs and first-observed timestamps; no raw HTML, seller
  identifiers, descriptions, phone numbers, VINs, photos, or cookies persist.
- `MonitorJob` is read by indexed source ID solely to compare saved discovery
  timestamps. No provider API requests, estimates, matches, delivery rows,
  subscription epochs, dedupe claims or user state are created or changed.
- Separate single worker thread and DB pool (one connection, no overflow).
  PostgreSQL statements/locks have 2s/0.5s limits. No DB connection during HTTP.
- Yield when the main monitor heartbeat is stale, either queue has >=100
  pending items, or measured process RSS exceeds 384 MiB.
- Every run persists request reservations BEFORE HTTP, preventing a restart
  from refreshing the request allowance. Reported reservations are an upper
  bound, not an AUTO.RIA API balance or exact completed-request count.

## Bounds and access

At most 600 reserved HTTP requests, 10,000 observed IDs, approximately 1 GiB
recorded response bytes, 2.5 MB per page, and 24h. Error responses/aborted bodies
are not included in recorded response bytes. Each page uses 8s connect / 15s
read timeouts and a 25s checked streaming deadline. No redirect following.

Fetch robots.txt first, recheck at least every six hours; fail closed if it is
unavailable, invalid or denies a sampled path. Explicit 401/403/redirects stop
the run. 429 waits at least 15min or the larger Retry-After (seconds or HTTP
date); an invalid header waits 24h. Other
errors wait 15min. Three consecutive errors stop. No IP rotation, login,
CAPTCHA solving or hidden endpoints. HTTP bodies are parsed in memory only.

Successful first-cycle IDs form a baseline, excluded from timing comparisons.
For later IDs that also have existing API jobs first observed since run start,
compare first-observed timestamps. Unmatched HTML IDs may be irrelevant to any
subscription; they are NOT automatically missing notifications. API timestamps
can represent supplemental discovery. Different source scopes and night polling
mean these numbers cannot establish whole-market recall or speed superiority.

## Diagnostics and operation

`GET /api/html-shadow-status` returns aggregates only: state, deadline, counters,
baseline/unique IDs, overlap and first-observation ordering. It never issues
network calls or returns individual IDs. Main health/source-status and business
logic remain separate. Inspect shadow diagnostics and primary health/queues
after deployment. 24–48h coverage comparison is not complete at startup.

Setting `RIA_HTML_SHADOW_RUN_ID` empty disables the task on the next deployment;
the durable experiment remains available in storage. The code fails independently
and defaults off. Do not automatically switch production discovery to HTML.

No infrastructure purchase is required for this bounded experiment. Existing
service resources are shared, so isolation is logical and concurrency-bounded,
not a claim of a separate machine or zero resource overhead.
