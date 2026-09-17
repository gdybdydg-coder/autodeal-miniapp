# AUTODeal backend — subscriptions for new worthwhile cars

> Product direction changed on 2026-09-17: AUTODeal will monitor new, qualifying
> listings for saved subscriptions and notify users in their private bot chat.
> A full-market catalog and bulk database acquisition are no longer launch goals.
> See [the notification-first transition plan](docs/notification-first-strategy.md).
> Release 20260917-30 makes subscription creation the main action and disables
> mass scans by default. Release 34 adds measurements for the explicitly
> authorized notification rollout; each subscription still requires activation.

## Manual minimum discount (release 20260917-41)

The subscription form has one text field with a decimal keyboard for
`filters.minDiscount`: any numeric percentage from 0 to 100, including fractions.
Both comma and period input are accepted. The saved threshold applies to monitor
matching, the final delivery check and legacy search results. Valuation evidence
proves the median independently of the selected threshold. Subscriptions with
otherwise identical filters share discovery and peer-price requests.

Existing subscriptions still use 15%. Omitting the default from canonical filters
preserves their fingerprints, active watches and checkpoints. Changing a threshold
uses the existing edit flow and requires explicit reactivation. Provider caps,
monitor flags and notification consent are unchanged.

## Subscription management (release 20260917-31)

Users can save up to 20 distinct subscriptions, open a card to edit its name and
all filters, cancel changes, pause/resume through the existing consent gates,
and delete with confirmation. The compact list shows each subscription's state;
readiness and save failures are visible instead of being hidden behind a panel.
Device-local drafts remain separate and can also be edited in place.

Authenticated `PUT /api/subscriptions/{id}` accepts `name` and `filters`, updates
only the owner's existing row and rejects duplicate criteria with a specific
409 response. Canonically unchanged filters preserve activation and the monitor
checkpoint, so a rename does not restart monitoring. Changed filters pause the
subscription and invalidate its old watch/matches under the same user lock used
by delivery and /stop. Explicit activation creates a fresh baseline. Editing does
not consume a new subscription slot, opt in to messages, or start a catalog scan.
Creating a duplicate draft cannot silently pause an existing active subscription.

Monitoring, delivery and provider-budget settings are unchanged. Release 31
completed subscription management; release 32 below implements durable monitoring.

## Durable subscription monitor (release 20260917-32)

Step 2 replaces the one-search/three-car pilot with shared source filters,
persistent creation-time windows, a durable valuation queue, fair work steps and
independent Telegram dispatch. The production flags remain off for this release;
valuation verification and a real opted-in delivery test are still required.
See [implementation and acceptance evidence](docs/notification-first-strategy.md#durable-monitor-release-20260917-32).

## Auditable price evaluation (release 20260917-33)

Step 3 adds explicit deal/ordinary/unknown outcomes, retained comparable evidence,
freshness checks, exact decimal threshold decisions and known-vehicle deduplication.
An additive `valuation_peers` table shares recent peer observations across nearby
candidate searches without building a source-market mirror. Candidate price and
valuation proof are rechecked before delivery, including known peer-price changes.
The Mini App explains the comparison rules and the latest unvalued outcome.
See [the retained-data review and its limitations](docs/valuation-review-2026-09-17.md).
178 backend tests and 59 frontend checks pass. No new live AUTO.RIA audit is run;
fresh coverage, latency and Telegram delivery remain the step-4 launch test.
Production monitoring, delivery, mass-scan flags and request caps are unchanged.

## Measured subscription rollout (release 20260917-34)

Step 4 adds `delivery_timings`, an additive table recording discovery, valuation,
queueing, send start and Telegram API acceptance. The provider's `addDate` is used
only when it includes an explicit timezone; missing/naive dates produce unknown
publication latency. Telegram acceptance does not prove a phone push was shown.

Authenticated notification status and Settings show the owner's last-24-hour
new/evaluated/unvalued/queued cars, accepted alerts and last measured delivery.
Public source status exposes aggregate readiness and counters, without recipient
identities, filters or listing IDs. These reads make no provider calls or sends.
A first-enabled baseline records cumulative provider usage once, survives
restarts and counts all server provider calls since activation; it is not an
estimate of monitor-only usage. Existing quota counters are never reset.

Publish with notification flags off, verify readiness and no active subscriptions
or queued deliveries, then enable `MONITOR_ENABLED`, `SOURCE_READY` and
`DELIVERY_ENABLED` on the existing paid API. Keep `FULL_SCAN_ENABLED=false`, the
900/hour, 3000/day and 90027 cumulative caps, and the exhausted audit unchanged.
The owner already confirmed the test message. The owner must activate one chosen
subscription through Telegram's authenticated Mini App; never impersonate that
session or enable another user's search. Observe a genuinely new qualifying car
before declaring the live delivery/latency check complete. Idle readiness alone
does not establish source coverage or production delivery speed.

185 backend tests and 60 frontend checks pass. Synthetic tests verify timing,
owner isolation, no duplicate delivery, no success count for timeout/429/403,
and no provider spending when reading progress. Synthetic timings are not live
latency measurements. See the official [AUTO.RIA listing fields](https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info)
and [Telegram sendMessage result](https://core.telegram.org/bots/api#sendmessage).

## Retiring mass scans (release 20260917-30)

`FULL_SCAN_ENABLED` defaults to false. Startup pauses queued, running and waiting
full scans without deleting saved filters or results, and does not start the
full-scan worker. Both scan-start APIs and resume requests return
`409 full_scan_disabled`; owned progress/results remain readable. Public
`/api/source-status` reports `full_scan.enabled` and `full_scan.active_jobs` so
deployment can verify that the old workers have stopped. Delivery and monitoring
flags are unchanged. The Mini App uses the existing saved-search flow to create
subscriptions, with the 15% deal threshold; enabling alerts still requires the
existing readiness and consent checks.

## Historical full search scans (release 20260917-29, disabled by default)

The Mini App starts a durable scan with authenticated `POST /api/cars/scans`.
It captures **every returned ID page** (`countpage=50`) and checks an early
candidate in each worker step before collecting the remaining pages. It then
checks every queued unique ID and estimates its price using the existing strict
peer rules. The old eight-card `/api/cars/search` remains only for
older clients; the new UI does not use it or require manual continuation clicks.

`full_scans` and `scan_items` store scan progress; the new `market_cars` table
indexes checked public cars across searches. Tables are created on startup;
no deployed table is altered. The API process runs short scan steps under a DB lease. Progress and
completed cards survive client closure, server restarts and quota waits. Only one
scan per user runs at a time; a different filter pauses their previous scan.
Switching onlyDeals reuses the same scan and provider work. Up to 20 result sets
are retained per user; an oldest inactive derived result set can be evicted.
Saved subscriptions and delivery tables are unaffected.

- `GET /api/cars/scans/{id}?after=0&only_deals=true&cache_after=0` reads owned
  progress/results and a separate page of previously checked public cars. Both
  numeric offsets page stored data, spending no AUTO.RIA requests. Scan results
  include removed IDs so a price change or unavailable car cannot remain as an
  old cached match in the UI. All known dimensions and ranges are applied to
  the index; an uncached dictionary returns no cars until the worker resolves it.
- Start/progress responses include up to 50 cached cars without waiting on the
  provider. Cache counts are separate from scan coverage. This index is partial,
  not a downloaded AUTO.RIA database or a guarantee of current listing status.
  Results retain their original check time; valuations expire after 15 minutes,
  and cached cars are retained for one day. Fresh results are reused in later
  scan jobs. Up to 200 pre-index results are backfilled per minute of active
  scanning under the provider lease, without network calls. Removal tombstones
  prevent older results from resurrecting unavailable cars.
- `PATCH /api/cars/scans/{id}` with `{enabled:false/true}` pauses/resumes an owned
  scan. The next network operation finishes before the worker observes a pause.
- Starting the same filters restores progress. Completed scans older than 15
  minutes restart on an explicit start; `?restart=true` explicitly starts over.
- Each step still has a 32-request/42-second allowance. That limits one worker
  step, not total coverage. Hourly/daily waits resume automatically. Exhaustion
  of the absolute package ceiling requires operator attention; no cap is raised
  and no package is bought automatically.
- The UI reports discovered, checked, valued, unavailable and deal counts. It
  renders 50 cards at a time while the server checks the whole captured queue.
  Old matches show their check time and require price rechecking, without an
  old percentage being advertised as a current discount.
- A repeated/truncated source page or a final gap between discovered IDs and
  source count ends as **incomplete**, never “all checked”. AUTO.RIA's offset
  pages are not an atomic market snapshot; new/deleted ads can change coverage.
  Every captured ID is still attempted. Missing eligible peers/vehicle data do
  not produce a fabricated market price.

No new Render service, database, paid product or Telegram message is needed.
The worker is idle until a user explicitly starts a scan. Existing notification
flags and the one-time validation run ID must stay unchanged during this release.
Verification: `pytest backend/tests -q` and `node --test *test.cjs`. Fixtures cover
123 candidates, multiple pages, a qualifying car beyond page one, quota waits
across a day boundary, restart/lease recovery, owner isolation, cancellation,
stale prices and incomplete provider pagination, without live provider calls.

The GitHub Pages Mini App retains device-local drafts and now offers explicit
server saving through https://autodeal-api.onrender.com. The Telegram SDK supplies
raw initData; the API validates the signature and user ownership. No automatic
import occurs. Cloud saves always use enabled=false. A separate cloud list supports
read-back, restoring filters, and confirmed deletion. Open from a Telegram Mini App
button, not a normal browser link. No session data or bot token is persisted by the
client. Render hosting and authenticated cloud saving have been verified. Bounded AUTO.RIA search is
connected; peer valuation has two live five-comparable examples, Golf and Passat.
The wider three-model audit is partial: most sampled listings lack eligible
condition/modification data or sufficient peers. See the
[2026-09-17 valuation report](docs/valuation-audit-2026-09-17.md). The paid API can
run the explicit opt-in monitor described below. The owner has confirmed the
separate Telegram test message; a real-car notification and wider valuation
coverage remain launch checks.

Frontend checks: `node --test cloud-test.cjs`, `node filter-test.cjs`,
`node storage-test.cjs`. The DOM harness checks behavior, not rendered visual layout.

«Пошук», «Вигідні» and «Мої пошуки» now switch separate in-app screens. Ordinary
search results also open a separate screen after «Показати авто»; the filter form
never includes old results underneath it. «← До фільтрів» or the Search tab returns
to the unchanged form without another provider request. Filters are hidden on both
results screens; the saved-search manager is a full page, not a
modal. The bottom navigation stays available. Screen switches start at the top
without an animated scroll down the filter form, and retain entered filters.
Results loading/completion never scrolls the page. Returning to search or opening
saved searches also cancels a pending ordinary search's completion scroll.

Settings is a separate screen opened by both the header gear and the bottom
settings button. GitHub Pages serves HTML with a ten-minute cache lifetime, so
reopening Telegram alone may retain an older interface. Each UI release updates
the `autodeal-version` meta tag and `release.json`. `app-version.js` checks this
small same-origin manifest without cache on foregrounding and offers an explicit
reload button; reload preserves Telegram's URL fragment and device storage.
It never reloads automatically while the user is editing filters.

Valuation policy `asking-v3` treats an omitted optional technical-condition ID
as unknown condition, rather than evidence of damage, provided all four source
flags (damage, parts, abroad, customs) explicitly say false. Known adverse
technical states or missing/ambiguous flags still prevent comparison. This is
an asking-price estimate, not a vehicle inspection. If a modification ID cannot
be resolved, peers must match the explicit litre value in the source fuel field,
as well as generation, body, fuel, transmission, year and mileage tolerances.
Missing or ambiguous engine capacity does not qualify for this fallback. Exact
known modifications still take precedence; five independent peers and the 15%
threshold are unchanged. Previously unvalued monitor interests may be rechecked
once on policy change, only while their existing subscriptions remain active.

For an incident involving a specific public listing, `RIA_DIAGNOSTIC_LISTING_ID`
enables a once-per-ID check at startup. It logs only condition primitives,
valuation blockers, monitor presence and aggregate subscription matches to
operator logs. It shares the existing quota and is capped at three provider calls,
including dictionaries. It cannot create listings, deliveries or subscriptions,
and has no public trigger/reset route. Leave empty during normal operation.

After Pages has published a release, the operator can set `MINIAPP_RELEASE` to
that identifier. Startup verifies the bot identity and its existing default menu,
updates only our app's launch URL to `?v=<release>`, and verifies the result.
The DB records the operation; an unavailable result can be retried once on a
later explicit deploy. A different app URL is a conflict.
This changes no webhook, monitoring/delivery flags, subscriptions or messages.
`miniapp_menu` in `/api/source-status` reports the release, result, and safe
diagnostic method names/error codes. It never exposes tokens or response bodies.

The bottom «Вигідні» navigation runs the same authenticated search with the current
form filters and `onlyDeals=true`, without changing the regular search preference.
Its heading and criteria identify the request. The client also checks the exact
15% threshold, five comparables, supported valuation and freshness before showing
a deal. «Пошук» / «Змінити фільтри» returns to the form without another request.
Repeated taps while loading cannot duplicate requests. Navigation back to the
form prevents a completed background response from scrolling away from it.
Run `node --test deals-test.cjs live-test.cjs cloud-test.cjs` for these interactions.

Opening «Мої пошуки» reads the account list once per user opening, with duplicate
requests blocked while loading. This does not call AUTO.RIA. Device searches stay
available if the server fails; each list shows its count. «Новий пошук» returns to
the filter form without saving or searching. Opening a saved card restores its
filters and explicitly searches, unless another search is still running. Closing
the manager with its back arrow restores the previous navigation tab. A late cloud-save response
cannot hide a newly opened draft. Deletion still requires confirmation and saves
remain without notifications until the separate enable button is pressed.
Run `node --test manager-test.cjs` for these flows.

## Components

- FastAPI subscription API; PostgreSQL via SQLAlchemy (SQLite only in local tests).
- Raw Telegram initData HMAC validation, one-hour expiry and per-user ownership.
- Private /start and /stop webhook with a secret header; no unsolicited startup messages.
- Trusted internal delivery ingestion, separate from authenticated AUTO.RIA search.
- Matching of body, fuel, transmission, price/year and mileage, minimum 15% below
  sample median. The monitoring adapter requires five comparable real listings
  using the production estimator and official catalog IDs.
- Delivery deduplication per Telegram user and source listing, across all searches.
- Pending messages recheck consent/filters/freshness immediately before sending.
- 429 retry scheduling, 403 disables delivery. Ambiguous network outcomes are
  quarantined as uncertain rather than retried and potentially duplicated.

## Local development

From the repository root, in a dedicated Python 3.12+ virtual environment:

```sh
pip install -r backend/requirements.txt
pytest backend/tests -q
uvicorn backend.app:factory --factory --host 0.0.0.0 --port 8000
```

Set DATABASE_URL, TELEGRAM_BOT_TOKEN and a random TELEGRAM_WEBHOOK_SECRET of at
least 32 characters through server-only environment variables. PostgreSQL URL:
`postgresql+psycopg://...`. The two delivery flags default to false.
No dotenv file is loaded automatically. Tests use fake tokens, isolated SQLite
databases and injected fake senders; they never contact Telegram.

## API

Authenticated requests supply raw Telegram.WebApp.initData in the
`X-Telegram-Init-Data` header, never a browser-supplied user/chat ID.

- GET /health — read-only database probe: 200 with database=connected and
  database_type, or 503 with database=unavailable. Does not return credentials,
  user data or database error details. Reports delivery_available but does not
  verify the Telegram token, listing source, schema integrity or delivery.
- GET /api/subscriptions — this user's searches.
- POST /api/subscriptions — {name, filters, enabled}; filters match the Mini App's
  names and kilometre units (mileage bounds are in thousands). Same filters update
  one subscription. Maximum 20 per user.
- PATCH /api/subscriptions/{id} — {enabled: true/false}.
- DELETE /api/subscriptions/{id} — owner-only deletion.
- POST /telegram/webhook — Telegram-only, secret header required.
- GET /api/notifications/status — authenticated runtime, private /start and test status.
- POST /api/notifications/test — explicitly sends one test to the authenticated
  user's verified private chat; at most once per ten minutes, including failures.
  Requires TELEGRAM_TEST_ENABLED=true and a configured webhook. This separate
  opt-in works with MONITOR_ENABLED, SOURCE_READY and DELIVERY_ENABLED all false.
  The status endpoint reports test_available separately from monitoring availability.
  Neither /start nor opening Settings sends a message; the user taps Send test.

Enabling requires both server flags and a verified private /start. The monitor
additionally requires a current heartbeat, configured webhook and successful
test message. Multiple opted-in subscriptions can run together; identical source
filters share polling. /start never re-enables old searches after /stop.
A new/re-enabled search records a creation-time cutoff without replaying old ads.
No silent import of device-local notification preferences is implemented.

## Deployment and monitoring limits

1. Choose/authorize hosting and review its current costs before resource creation.
2. Provision PostgreSQL and an HTTPS API. Add secrets through the provider UI,
   never chat, source control, front-end JS or request logs.
3. `TELEGRAM_CONFIGURE_WEBHOOK=true` explicitly opts into setup at API startup.
   It first verifies the expected bot username and getWebhookInfo. A different
   webhook is a conflict and is never replaced. Empty/our URL can be configured
   with secret_token and allowed_updates=["message"], preserving pending updates.
4. Verify live valuation and freshness before setting SOURCE_READY=true.
   Manual search remains separate and never feeds delivery.
5. Connect Mini App to API: load official Telegram SDK, validate initData server-side,
   explicitly confirm each cloud subscription and request write access/start as needed.
6. Set MONITOR_ENABLED=true, SOURCE_READY=true, DELIVERY_ENABLED=true only with
   the paid always-on API and reviewed request caps. Existing saved searches stay off.
7. Set TELEGRAM_TEST_ENABLED=true for the explicit Mini App test. For a Telegram-only
   check this can follow step 3 with all monitoring/delivery flags still false.
   In Settings send the test to the verified private chat. After the monitor rollout,
   enable one saved search. A successful test is enforced server-side before activation.

`backend.monitor` runs two API lifespan loops: source work and Telegram dispatch,
with threads for blocking calls and a shared stop event. A database lease prevents
parallel source work across deploys. No additional paid worker is created; the
existing $7 API is reused. All notification flags still default off.

Creation-time windows use official `created_after`, `created_before`, `order_by=7`
and 50-ID pages. Each response is committed before evaluation. Multi-page windows
are verified with another pass, recovering page shifts due to removals. Fixed
upper bounds avoid moving new heads; a two-minute overlap recovers short indexing
delays. Repeated/inconsistent pages remain visibly paused, without advancing the
checkpoint. Restarts resume the saved window and page. Activation boundaries split
shared windows so a new subscriber never inherits an earlier subscriber's backlog.

Jobs run in short fair steps between discovery requests, without a three-car cap
or five-minute queue expiry. Identical listings share valuation across matching
subscriptions, using recent monitor evidence only (up to 60 seconds). Candidate
details are fetched fresh after long waits; cached peers remain limited to 15
minutes. Insufficient comparisons are recorded as unvalued, never a false bargain.
An alert queued longer than five minutes returns to valuation before dispatch;
removed/non-qualifying cars lose their old match evidence and are cancelled.

The base interval is 60 seconds. The target grows with distinct filter groups to
reserve half the configured hourly/daily allowance for details and valuations.
With caps of 900/hour and 3,000/day, one group targets 60 seconds, two 116 seconds.
Catch-up pages, overlap/activation slices and valuations consume extra calls;
the global hard caps still govern every request. These are targets, not an instant
delivery or full-coverage guarantee. Source indexing delays beyond the overlap,
provider inconsistencies and exhausted quotas remain explicit rollout risks.

Separate additive monitor tables record seen IDs, enable epochs, filter match
evidence and heartbeat. Subscription changes or /stop invalidate old work. No
automatic reset of quota or cursor occurs on restart. Monitor state is removed
when its saved search is disabled/deleted; sent-delivery dedupe remains. No raw
seller data or tokens are stored in monitor records. Source errors defer work;
quota limits include failed calls and pause polling until capacity returns.

Worker entry: `python -m backend.worker`; one invocation attempts at most one message.
Dispatch selects each user's matching, not-yet-queued listings and attempts one
message per separate one-second loop tick. Per-chat rate limits, load measurement
and operational-state retention still need review before large-scale use.
Add reverse-proxy request/body/rate limits and database backups.
Use one API worker initially. User creation handles uniqueness conflicts with a
savepoint; PostgreSQL user-row locks serialize existing-user edits.
Production PostgreSQL/concurrency behaviour has not been tested here.

Exactly-once Telegram delivery cannot be guaranteed across a crash after sending.
Rows left in sending/uncertain are not automatically retried; investigate manually.
This chooses avoiding duplicate messages over guaranteed delivery. A message already
in flight at /stop cannot be recalled. Commands are ordered by message date and
update_id, accommodating Telegram's update ID reset after long inactivity.

## References

- [Telegram initData verification](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
- [Telegram webhook secret](https://core.telegram.org/bots/api#setwebhook)
- [Telegram sendPhoto](https://core.telegram.org/bots/api#sendphoto)

Do not expose database/admin ingestion endpoints to the Mini App. Names, tokens and
initData must not be logged. User IDs/filters are personal data: decide retention,
account deletion and the privacy notice before accepting other users.
# AUTO.RIA connection check

## Authenticated search and sample valuation

`POST /api/cars/search` accepts the existing Filters payload and requires valid
Telegram initData. Names are resolved against official AUTO.RIA dictionaries,
cached for seven days. Unknown/ambiguous filters return 422, never a wider search.
Result details are checked again against selected IDs and ranges. Mileage is
converted from the provider's thousands of kilometres into kilometres.

Manual search loads up to eight candidate listings per page, with at most six
additional peers per candidate by default. `RIA_COMPARABLE_SCAN_LIMIT` can raise
the peer scan to 20 after purchasing sufficient quota. Comparable queries include
modification, technical condition and the candidate's mileage window; details are
still checked independently. Scanning stops once five suitable peers establish an
estimate or a mixed sample. `next_cursor` continues through further pages. Each
action spends at most 32 provider requests and has the existing 42-second deadline.
Interrupted details and valuations resume before advancing to further IDs. Cursor
records expire with the original 15-minute observation window and are bound to
the same filters (the deals toggle shares a snapshot). Replaying a cursor uses
its cached response; a stale cursor requires a new search. The UI merges listing
IDs and preserves existing results if loading more fails. There is no automatic polling.
Raw search IDs and sanitized details are cached for 15 minutes. The shared
PostgreSQL budget allows at most 24 calls in a rolling hour, 60 per rolling day,
and 900 lifetime (including a reserve of two for the first connectivity check).
Errors count toward the budget; 401/403/429 block further calls for one hour.
Limits persist across deployment. Only one search can spend quota at a time;
a lease recovers after 90 seconds if the process crashes. Calls made elsewhere
with the same key are not known to this budget. No paid APIs are called.

`GET /api/catalog` returns official makes, regions, body, fuel and gearbox lists;
`?brand=Peugeot` returns that make's full model dictionary. Telegram authentication
is required before any catalog work. Official lists share the seven-day source
cache; aggregated UI dictionaries are cached for one day. Model loads are lazy
and serialized in the UI, and an old response cannot replace a newer selection.
Saved searches load their missing dictionaries before restoring all filters.

`RIA_CATALOG_ROLLOUT_CHECK=true` opts into the once-only `catalog-pagination-v1`
startup check: warm the dictionaries, check Peugeot models and two Volkswagen /
Khmelnytskyi pages. It uses at most 32 calls within the existing global budget.
Peer valuation is deliberately skipped in this coverage check; it never caches
unvalued UI snapshots or writes to the notification pipeline. Its sanitized result
is available in `/api/source-status` as `catalog_check`.

## Market valuation audit

Missing modification IDs can now be recovered from the official
[generation/body modification catalog](https://docs-developers.ria.com/en/used-cars/parameters/modifications).
Only a full `autoData.modificationName` matching exactly one catalog ID for that
generation and body is accepted (case and whitespace normalization only). Existing
IDs are never overwritten. The lookup requires otherwise complete identifiers
and eligible condition. Missing names, ambiguous matches and differing engine,
gearbox or drive suffixes cannot manufacture a match. Seller descriptions and
VIN are never used. The same strict comparator and five-peer/15% rule still apply.

Catalogs use the shared seven-day cache and request budget. Candidate cards load
before optional valuation lookups; a lookup interrupted by the cap can continue
through the existing cursor. Detail/snapshot cache namespaces are versioned so
old parsed records do not hide the new field or explanations. The Mini App now
distinguishes insufficient vehicle data/condition, too few peers and mixed prices.

`RIA_VALIDATION_RUN_ID` selects an explicit once-only check. The default
`RIA_VALIDATION_PROFILE=golf` retains the original Volkswagen Golf check and its
32-call cap. `RIA_VALIDATION_PROFILE=popular-v1` checks Volkswagen Passat, Audi A6
and Mercedes-Benz E-Class sequentially: at most 32 AUTO.RIA calls per model,
96 total, within the existing shared hourly/daily/total budgets. Each model uses
the production search, detail parsing, post-filtering and estimator; it examines
up to eight candidate cards with the configured peer scan limit. This does not
call the provider's separate valuation product or submit anything to delivery.

`RIA_VALIDATION_PROFILE=eligible-v1` uses the same three models and 96-call cap,
but asks the provider for undamaged, customs-cleared vehicles in Ukraine. Returned
details are still independently checked. `candidate_scope=undamaged` makes clear
this is a targeted supported-condition check, not an all-market coverage sample.
At most one real candidate per model also checks the catalog resolver against its
existing listing-provided modification ID using a separate copy; the production
candidate is never modified by that diagnostic. Inspect `catalog_resolution_checks`
alongside medians. Any disagreement requires investigation before relying on the
resolver. Use a new explicit run ID only deliberately.

The approved `eligible-20260917-1` run has now completed: 96 calls, 24 candidate
cards, three matching catalog diagnostics and one qualifying Passat price sample
($8,700 versus a $11,700 five-peer median). Sixteen valuations remained pending
at the cap; the overall result is partial. This ID must not be reused to request
another run. See the [supported-condition report and evidence](docs/valuation-audit-eligible-2026-09-17.md).
The Mini App release 20260917-27 and backend were deployed; monitoring and
automatic delivery remain disabled.

The `popular-v1` check `popular-20260917-1` completed on 2026-09-17 with status
`partial`: 24 candidates, one median and 55 provider requests. The
[report and retained evidence](docs/valuation-audit-2026-09-17.md) explain each
coverage limit. This ID has already been used; any further live run needs a
deliberately selected new ID and bounded scope. Do not reuse an old run ID with
another profile: status reports `profile_conflict` and
the original record is preserved. A claimed ID never automatically runs again,
including after a failure or restart. No schema change or new service is needed.

The audit runs as a background startup task so health/API requests can be served.
Each model retains the production request deadline and database budget lease.
Shutdown signals it to stop before further requests; quota/authentication failures
stop the remaining models. Source-detail caches within their 15-minute lifetime
can be reused; observation timestamps distinguish their age. Reopening the public
`/api/source-status` only reads the recorded progress and spends no provider calls.

`valuation_check.queries` contains sanitized candidate/peer evidence for each model:
accepted IDs/prices, rejection reasons, duplicate/self entries, independently
recalculated median, exact 15% deal threshold and agreement with the estimator.
Missing required attributes, unverified condition, fewer than five eligible peers
or a mixed price sample produce no deal threshold. No seller details, VIN, raw
provider bodies, authentication or personal saved searches are included.

`samples_checked` means each model produced at least one independently checked
estimate without interrupted work; it is a sample check, not proof of complete
market coverage or resale value. `partial` exposes missing evidence or limits;
`calculation_mismatch` requires investigation before enabling delivery. Review
the underlying listing details as well as the computed median before rollout.
Run `pytest backend/tests/test_valuation_audit.py -q` for lifecycle, caps, evidence,
privacy, partial coverage and shutdown checks.

### Moving beyond the free test allowance

Buying a provider package does not change the application caps. After purchase
and verification of the provider balance, set ALL three server-only variables:
`RIA_REQUESTS_HOURLY_CAP`, `RIA_REQUESTS_DAILY_CAP`, `RIA_REQUESTS_TOTAL_CAP`.
Omitting all three keeps the original free caps. Partial, non-integer, zero, or
inconsistent caps fail startup. Use the same values on the API and future worker.
The total cap is an absolute ceiling on the persisted request counter: it never
resets on a deploy, date change, new API key, or package purchase. Increase it
deliberately by the verified additional allowance, retaining a reserve. Provider
cooldowns and the shared database budget still apply under paid caps.

`GET /api/source-status` includes `budget` with local hourly/daily/cumulative usage,
caps and remaining requests. This read spends no API requests and reveals no key
or user data. Counters include failed calls and the initial two-call reserve.
They are NOT the provider's balance: calls outside AUTODeal are unknown.

Launch sizing example (not a measured performance promise): one distinct filter
checked once a minute needs 43,200 search calls in 30 days, before listing details,
valuation and catalog lookups. Two such filters need 86,400; three need 129,600.
A 100,000-call package needs measured headroom. Shared identical filters save
polls; multiple distinct filters automatically increase the target interval.
At two-minute intervals the base counts halve.
Actual cadence also depends on provider indexing, response time and new-car volume.
This budget configuration does not start monitoring or enable delivery. The
current manual search still uses its bounded sample and 15-minute snapshot cache.
The separate monitor bypasses that cache; the owner's end-to-end delivery check
is still required before enabling a subscription.

An operator can set `RIA_VALIDATION_RUN_ID` (1–40 letters/digits/dashes/underscores)
to request a once-only live Volkswagen Golf valuation check at the next deploy.
It uses production filters and estimation, the shared quota/cache/lease, and a
hard cap of 32 upstream attempts. A committed unique claim prevents retries on
restart, failure or concurrent deployment. Use a new identifier only deliberately.
`valuation_check` in `/api/source-status` exposes sanitized listing/comparable
summaries, missing vehicle fields and numeric provider rate-limit headers if sent.
No raw seller data, VIN, descriptions, cookies or secrets are stored. This check
does not ingest listings, send messages, register a webhook, or enable delivery.
Provider response headers can confirm its hourly rate; they do not establish the
remaining package balance. A successful sample is not a validated appraisal.

On 2026-09-17, the first paid-allowance check returned three real Golf listings but
only two suitable peers for its eligible candidate. The follow-up, with a 20-car
scan cap and precise condition/mileage queries, verified five other comparable
listings for Golf 2011 #39818198: asking prices 7800, 7950, 8590, 7990 and 7999 USD;
median 7990 USD versus the candidate's 8950 USD. It correctly did not qualify as a
deal. Two other candidates stayed unvalued due to missing condition/modification
data. These are historical observations, not current prices. Both checks used 17
new upstream calls in total, sharing cached details. AUTO.RIA did not provide the
allowlisted quota headers, so this does not independently verify its account balance.
Delivery remained disabled; no paid Render resources were created by these checks.

Candidate cards are loaded before spending requests on peer valuation, so a
valuation quota failure preserves available matching cards. Error and search
responses include a quota reason and wait in seconds, taking all rolling limits
and provider cooldowns into account. The lifetime cap never promises an automatic
reset. `/api/source-status` exposes this read-only quota state for diagnostics.

Successful candidate results are also retained as sanitized snapshots for up to
24 hours from the original observation time. Identical filters reuse the snapshot
for 15 minutes without network calls, including when the deals toggle changes.
During a quota pause or temporary source failure, an older matching snapshot may
be returned with `cached=true`, `stale=true` and its original `checked_at`. Different
filters never receive that snapshot. Snapshots older than 24 hours are unavailable.
Old market estimates/discounts are removed, and stale cars cannot pass the deals
filter. The UI labels historical prices and warns that availability may change.
Refresh replaces the snapshot; viewing it never extends the observation time.
Snapshots contain no seller details, credentials or Telegram data.

The provider's legacy median API is deprecated:
https://docs-developers.ria.com/en/used-cars/average_price/median_average_price
Instead, a conservative sample median needs five distinct OTHER active listings,
with the same make, model, generation, modification, body, fuel and gearbox,
year within one year and mileage within max(30,000 km, 20%). All must explicitly
be undamaged, in Ukraine and customs-cleared; missing data prevents an estimate.
Peer queries deliberately omit buyer price and region limits. A max/min price
ratio over two rejects a mixed sample. This is a small sample of asking prices,
not a validated appraisal or transaction prices. The UI states these limits.
Only an unrounded comparison of price <= median * 0.85 qualifies for deals.
Insufficient data, source errors or quota exhaustion NEVER fabricate a discount.

An additional once-only deployment check uses Volkswagen / Khmelnytskyi, without
the deals filter, through the same shared quota/cache. Its summarized result is
under `filter_check` in `/api/source-status`. It cannot be triggered by a visitor.
Existing connection-check status/preview describes the original one-time sample.

The UI now calls authenticated search instead of rendering demonstration cars.
Empty/partial results are labeled as a limited sample. Delivery remains disabled;
search results never write to the `listings` / `deliveries` tables.

Set server-only `AUTO_RIA_API_KEY` in Render. On startup, a one-time check calls
the documented used-car search (one newest active passenger-car ID) and info
endpoints, at most two requests in total. No paid valuation endpoints are used.
The claim is committed in the new `source_probes` table before any network call;
failures, process crashes and redeploys do not automatically retry. If a check
fails, investigate its status before explicitly arranging another attempt.

`GET /api/source-status` reads the cached check without contacting AUTO.RIA.
It exposes only a fixed status, timestamp, requests reserved, and a small public
listing preview. It never returns the key, upstream errors, seller data or VIN.
The preview is a historical connectivity sample, not a current search result.
The `checking` state can remain after an interrupted startup; it is not a retry loop.
`connected_empty` means a valid empty search, not verified listing details.

This does not connect Mini App filters, calculate a market price, ingest delivery
listings, start a worker, or change `SOURCE_READY` / `DELIVERY_ENABLED`.
Both flags must remain false during integration. Source attribution:
[AUTO.RIA](https://auto.ria.com/).

Official API contracts:
- https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto
- https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info
