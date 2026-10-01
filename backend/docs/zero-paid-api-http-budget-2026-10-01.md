# Zero-paid API: offline HTTP budget and backoff

Checkpoint: 01.10.2026, about 06:30 Europe/Kyiv. Production was not changed.

Added `experiments/free_search/http_budget.py`, a pure offline state machine for
a hypothetical shared public-page fetcher. It imports no network client,
backend, database or Telegram code and cannot make a request. The configured
budgets in tests are synthetic examples, not approved production cadence or a
claim that continuous collection is permitted.

Every request requires three explicit gates: written terms approval, robots
allowance and an approved public route. The current experiment must set the
terms gate to false because the official clauses documented in the source audit
remain ambiguous. The only modeled routes are two feed pages and a detail page;
there is no arbitrary URL, hidden endpoint, cookie, redirect or paid fallback.

One shared work key deduplicates consumers. Before hypothetical I/O,
`reserve_next` persists a GET reservation, increments the attempt count and
charges the maximum response size against rolling hourly/daily request and byte
budgets. A successful bounded response settles to actual bytes. A crash retains
the full worst-case charge, settles the uncertain reservation and delays the
idempotent GET before it may be attempted again; reservations are never refunded.

The queue, in-flight work, attempts and response size are bounded. HTTP 429
pauses the whole source and honors a valid `Retry-After`; invalid values fail
safe to one day. 401/403/451 persistently block the source. Redirects are not
followed and trigger a one-day pause. Oversized or unexpected content and 5xx/
transport failures use bounded exponential backoff and eventually exhaust.
404/410 are terminal unavailable results. Response bodies, URLs, headers,
seller data and HTML are not retained by this model.

Verification: 21 HTTP-budget tests passed. They cover permission gates, shared
deduplication, queue/in-flight/request/byte caps, reservation-before-I/O,
cookies/redirect prohibition, byte settlement, body limits, 429, denials,
transient backoff/exhaustion, restart accounting, FIFO eligibility, strict
serialization and dependency isolation. The 19 offline queue tests and 29
visible-adapter/filter-gate tests also passed in the selected run. No backend
regression is claimed because none of these modules is imported by production.

Next step: build an offline orchestration test that connects sanitized card and
detail fixtures through publication proof, HTTP reservations, filter gating and
the shared queue. It must prove that one candidate is fetched once for multiple
subscriptions, that a denied/budget-paused fetch creates no evaluation or
delivery claim, and that restart preserves every boundary. Continuous live
collection remains blocked pending written terms clarification.
