# AUTODeal backend — subscriptions for new worthwhile cars

## AUTO.RIA API lower market boundary minus 5% (release 20260918-50)

Telegram notifications now support the owner's provider-based policy: the lower
boundary of the **listing-specific paid AUTO.RIA AI range**, multiplied by
**0.95**, with each subscription's saved discount applied to that adjusted value.
Production enables it with `RIA_AI_PRICE_ENABLED=true` and server-only
`AUTO_RIA_USER_ID`, reusing `AUTO_RIA_API_KEY`. No credential belongs in Git.

The live API response supplies `price.USD` and `avgValueRange`. The adapter derives
a symmetric range from these **two** provider fields and floors its boundaries
to whole dollars; it never guesses the range from an average alone. It requests
`omniId` for the candidate with a 168-hour period. Native-app parity is not claimed:
periods, refresh time and rounding can differ. The Telegram card discloses the
lower boundary and the separate 5% adjustment. See [schema, evidence and limits](docs/autoria-lower-bound-integration.md).

Each uncached new candidate uses one budgeted AI request (20-second timeout,
no inner retry, 60-second shared cache), instead of notification-comparable
searches. Missing/malformed ranges, method permission failures and timeouts
preserve a fresh informational card without an invented valuation. Old pending
proofs are refreshed only for current unsent interests; sent/uncertain claims and
stopped epochs remain untouched. No historical jobs are reopened by the upgrade.
Repair notices, fresh positive prices, /stop and existing quotas are preserved.
`RIA_ACTIVE_WINDOW_ENABLED=false` and `FULL_SCAN_ENABLED=false` remain in force.
Legacy peer valuation is retained only for existing manual-search callers and
for explicitly disabled AI mode, not as a hidden AI-notification fallback.

Optional `RIA_AI_PRICE_PROBE_ID` requests one read-only quote at startup. Its
unique durable claim prevents repeats, including on redeploy. It uses the same
budget and never fetches listing details, creates jobs or sends Telegram cards.

Validation: 405 backend tests and 65 frontend tests pass, including the complete
provider-to-dispatch path and all existing delivery safeguards.

The following sections describe earlier releases; their notification pricing
formulas are superseded while the AUTO.RIA AI policy is enabled.

## Repair candidates are disclosed instead of hidden (release 20260918-49)

At the owner's explicit request, damage and technical-condition flags on a
**candidate** no longer prevent a matching notification. `reference-v4` can
calculate the same conservative lower-quartile reference from eligible peers;
the card discloses the source's repair/damage marker and that repair costs are
not included. Comparable peers still require clear source-condition flags, so
damaged peer prices are not silently mixed into the reference. Parts-only,
abroad and customs exclusions are unchanged. No seller text is inferred as a
mechanical diagnosis. The exact comparison policy remains `asking-v5`.

When a price reference is unavailable, `listing-price-v3` can deliver the freshly
priced repair candidate with an explicit condition notice and no fabricated
market/discount. Proof replay checks the condition notice against the retained
source flags. Saved filters and minimum discount for priced alerts, /stop,
sent/uncertain claims, fresh positive prices and all source quotas remain intact.
`informational-v3` does not reopen finished historical jobs in fresh-only mode.

Vito incident `40292766`: the bounded diagnostic found one matching current
subscription, no monitor job/seen interest, technical-condition ID 3 and no
generation ID. Exact-ID `created` and `published` queries for the active interval
both returned no match, despite a recent public-page display timestamp. Repair
admission addresses the second blocker; it does not claim that the publication
API discovered this listing. The existing once-per-ID operator recovery can
process this explicitly requested car through the normal worker. It does not
enable old-candidate scanning or reset previous recovery/delivery claims.

Validation: 324 backend tests and 65 frontend tests pass. Added coverage includes
repair cards with/without reference peers, explicit condition disclosure, notice
tamper rejection, minimum-discount enforcement, /stop during comparison and a
once-only recovery with missing generation outside the publication window.

## Supported lower prices despite an expensive upper tail (release 20260918-48)

Historical release notes; candidate repair handling is superseded by release 49.

`reference-v3` fixes a false-negative in the prior spread guard: one expensive
peer could suppress the entire p25 estimate even when the cheaper observations
agreed. Exact `asking-v5` comparisons stay unchanged. A wide sample can now
supply an explicitly **indicative** lower-price reference if at least three AND
a strict majority of the cheapest eligible peers fit within the existing 2:1
spread bound. P25 still uses **all** eligible observations: no cheap or expensive
prices are removed. A sparse cheap tail or a wide lower band remains unpriced.
If the exact cohort was mixed, the fallback uses only that exact accepted cohort;
adding broader, more expensive peers cannot raise its quartile. Known vehicle
conflicts, freshness, condition exclusions, saved thresholds and delivery claims
remain enforced. No provider request or historical candidate scan is added.

Synthetic regression only: `900, 1300, 1600, 1800, 6000` now yields an indicative
$1,300 lower quartile; a $950 candidate is 26.9% below it. Previously the entire
sample was rejected. These prices are not a reconstruction of the owner's Lada
until verified against retained server evidence. Cards disclose a wide price
spread, the observation count and the absence of completed-sale data. Unpriced
cards now explain an unsupported price spread, and their acceptance logs retain
the public source ID, reason codes, peer count and prices for diagnosis.

Optional `VALUATION_AUDIT_RUN_ID` (lowercase letters, digits and hyphens, max 40)
performs one operator-only replay of up to 20 retained mixed-price evaluations
from the last 24 hours. A durable `SourceProbe` claim prevents repeats. It uses
the original evaluation time, makes **zero** provider calls, never queues jobs
or notifications and never modifies deliveries/subscriptions. Only public car
fields and price evidence are logged; no recipient IDs, credentials, seller text
or VINs. Replay results are historical analysis, not current deliverable quotes.

Validation: 319 backend tests and 65 frontend tests pass. Regression coverage
includes an expensive outlier, unsupported low bands, exact-cohort preservation,
evidence replay/tamper rejection, saved-discount enforcement, deduplication,
zero additional requests with cached peers, Telegram wording and bounded
operator diagnostics that cannot alter sent/uncertain deliveries or quota.

## Conservative asking-price reference (release 20260918-47)

Historical release notes; wide-price sample handling is superseded by release 48.

`asking-v5` and `reference-v2` replace the median used for notification thresholds
with the **lower quartile (p25)** of the same eligible, distinct fresh peer prices.
For sorted prices, the zero-based position is `(n - 1) / 4`, with linear
interpolation between neighbouring prices. For five peers this is the second
lowest price: one very cheap peer cannot set the reference. There is no arbitrary
percentage haircut or manually imposed target price. Identical peer prices yield
the same reference as before; p25 cannot be higher than their median.

Example only (not a reconstruction of the owner's screenshot): asking prices
`3300, 3500, 4000, 4500, 4900` previously produced a $4,000 median. The new reference
is $3,500, so a $3,550 listing does not pass a 10% threshold. Offline replay of the
two previously audited, real retained samples changes their references from
$13,300 to $13,200 and $11,700 to $10,500. The historical audit files are preserved.
These are asking-price observations, not observed completed sales or a prediction
of resale proceeds. A small or unrepresentative sample can still overvalue a car.

Both exact and indicative cards say "Обережний ціновий орієнтир" and "Нижче
орієнтира", disclose peer count and that completed sale prices are unknown. The
broader tier continues to disclose its uncertainty. All matching, freshness,
sample-dispersion, known-vehicle deduplication and exact saved `minDiscount`
checks remain in force. Evidence retains the median for audit, p25 and its method;
dispatch recomputes p25 and rejects all legacy median proofs. A pending fresh
delivery may revalidate normally; finished history is not reopened by a policy
upgrade when supplemental discovery is disabled. Sent/uncertain claims, /stop,
subscriptions and epochs stay unchanged. Cached legacy medians are not relabelled.

This calculation uses the same observations and adds no provider requests. New
publication discovery, disabled old-candidate scanning, the shared eight-call
comparison cap and all quota limits stay unchanged. Accepted priced notifications
log public listing ID, policy version, p25, median, count and peer prices without
recipient IDs or credentials. Historical activity counters include both policies.

Validation: 303 backend tests and 65 frontend tests pass, including an ordinary
price that the median would classify as discounted, exact decimal thresholds,
strict/indicative delivery wording, legacy evidence rejection, no historical
replay in fresh-only mode, deduplication and offline retained-sample replay.

## Indicative market prices for new alerts (release 20260918-46)

Historical release notes; the pricing statistic below is superseded by release 47.

Notifications first try the existing five-peer `asking-v4` comparison. When it
cannot value an otherwise eligible new car, `reference-v1` can show a labelled
**indicative** median from at least three distinct, fresh comparable listings.
It matches brand/model and every known generation/body/fuel/gear/engine field;
missing candidate attributes are disclosed rather than guessed. Known engines
allow different trim/modification IDs in this broader tier. Without an engine
capacity, a known modification must still match. Year tolerance is ±2 and known
mileage tolerance ±max(60,000 km, 40%). Peers must have explicitly eligible source
condition; a candidate of unknown condition is labelled, and explicit adverse
condition remains excluded. Self/relisted VINs, stale/invalid prices, conflicting
known attributes and samples with a max/min price ratio over two are rejected.

The card shows an indicative market price, the percentage below that estimate
and peer count. Each subscription's exact `minDiscount` applies to both price
tiers, including at final dispatch. With no usable reference, the fresh-price
informational card remains available without a fabricated price or percentage.
Delivery recomputes the reference evidence and checks known changes/removals.

Both comparison tiers share at most eight additional provider requests and an
eight-second window for starting them; an in-flight request retains the provider
adapter's eight-second timeout. This is not a first-second delivery guarantee.
Only one first page per tier is used. An interrupted comparison keeps already
retrieved peers, so three usable observations survive a later upstream failure.
Fresh comparison observations are reused across jobs; user budget and region do
not restrict peer prices. A lack of peers never creates an endless retry job.

This does not enable historical candidate discovery or revalue delivered cars.
`RIA_ACTIVE_WINDOW_ENABLED` and `FULL_SCAN_ENABLED` remain false, existing exact
policy and notification version markers are unchanged, and quota caps, /stop,
subscription epochs, sent/uncertain claims and filters are preserved. No schema
migration, paid valuation product or new service is required. Aggregate activity
adds `reference_estimated`; accepted reference notifications log only public
listing ID, indicative median and sample size, never recipient IDs or credentials.

## Fresh publications and multiple regions (release 20260918-45)

The owner's 2026-09-18 direction is to spend discovery quota on newly published
cars. Keep `RIA_ACTIVE_WINDOW_ENABLED=false` and `FULL_SCAN_ENABLED=false`.
The main publication-time monitor, short indexing overlap, subscription activation
checkpoints and current price verification remain in place. Disabling the active
window retires its pending valuation jobs and prevents both newly queued and
already queued supplemental cards from being sent or refreshing old prices.
Sent/uncertain delivery records and stopped subscriptions remain untouched.
Comparable-price requests still serve valuation of new candidates; no historical
candidate sweep or periodic old price-drop checking runs in this mode.

The main form now offers checkbox selection of one or multiple regions. The
existing `filters.region` accepts a string or a list of region names, with OR
semantics. One group sends `state[0]`, `state[1]`, etc. with corresponding
`city[i]=0` in a single AUTO.RIA search. Details must match one of the selected
region IDs; a missing region is not treated as an optional unknown attribute.
Region order/duplicates canonicalize to one subscription. Empty and single-region
lists canonicalize to the previous string representation, preserving legacy
fingerprints, saved drafts and active monitor checkpoints.

Editing an existing subscription to multiple regions follows the normal
save/reactivate flow. The release does not silently merge separate subscriptions
whose other filters or activation times might differ. Provider caps stay at their
configured values. Discovery still polls according to its quota-aware interval;
this release does not promise first-second publication-to-phone delivery.

Validation covers four regions in one feed, exact region matching (including
missing/out-of-scope locations), old single-region compatibility, persistence and
duplicates, and switching off supplemental jobs/queued cards while new alerts
continue and sent/uncertain records are preserved.

Provider syntax: https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto

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

Publication-time windows use official `published_after`, `published_before`, `order_by=7`
and 50-ID pages. Each response is committed before evaluation. Multi-page windows
are verified with another pass, recovering page shifts due to removals. Fixed
upper bounds avoid moving new heads; a ten-minute overlap recovers short indexing
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


### Alert coverage and latency (20260917-44)

Incident diagnostics found six unvalued arrivals due to insufficient peers (three
also reached the comparison scan cap), and five rejected on source condition.
`launch.activity.unknown_breakdown` now reports fixed aggregate reason codes,
rejected-peer reasons and sample sizes, with no listing or subscriber identifiers.

Valuation `asking-v4` accepts a peer with no modification ID when explicit engine
capacity matches and generation, body, fuel, transmission, year, mileage, freshness
and condition checks pass. Conflicting known modification IDs remain excluded.
At least five distinct eligible cars are still required. Peer queries use engine
capacity when known so optional missing modification IDs cannot hide candidates.
Previously discovered active unvalued interests are re-evaluated once under the
new policy; already delivered cars and stopped subscription epochs are not replayed.

Discovery follows publication time, including a first publication of an older
draft, rather than creation time. A ten-minute overlapping window recovers delayed
index entries. Listing-ID deduplication prevents repeated publication from sending
another alert for an already seen ID. Unfinished creation-clock page contexts restart
from the existing checkpoint without resetting subscription activation boundaries.

For the user's priority of faster delivery, the operational daily allowance is
12,000 requests with the existing 900/hour and 90,027 absolute ceilings unchanged.
Four distinct subscriptions therefore target 60 seconds between polls. That is
about 5,760 daily discovery requests before pagination, comparisons and other API
usage; the purchased package consequently lasts less time. No new paid service
or automatic package purchase is introduced. API indexing, valuation work and
quota pauses still affect real arrival time.

### Reported listing recovery (2026-09-17)

The operator can set `RIA_RECOVERY_LISTING_ID` to one reported numeric AUTO.RIA ID.
This is separate from the read-only `RIA_DIAGNOSTIC_LISTING_ID` check. Recovery
queues the ID once for currently active interests under the monitor lease; it
does not scan a catalog, change filters, bypass valuation, reset quota, or send
directly. Already queued/attempted deliveries, including uncertain sends, are not
replayed. The normal worker fetches fresh details and applies subscription rules.
Logs named `Notification recovery` record public listing price, valuation reasons,
and aggregate delivery states, never recipient IDs or seller data. An accepted
Telegram message is not proof that a phone displayed a notification.

The incomplete-details notification policy now has its own version marker.
Previously discovered active unvalued jobs with missing optional details are
rechecked once even if their asking-price valuation version has not changed.
Stopped epochs and already delivered listings remain protected by normal checks.

### Informational alerts when valuation is unavailable (2026-09-17)

At the owner's request, matching freshly priced listings are no longer silently
withheld because optional data or suitable comparables are missing. The monitor
sends an explicitly informational card with no market value or percentage, using
versioned `listing-price-v2` evidence. Unknown condition is labelled; explicit
damage/parts/abroad/custom or adverse technical condition remains excluded, even
on an incomplete card. A confirmed valuation still applies the saved discount
threshold. Comparison timeouts/limits can yield information-only alerts after
fresh details have succeeded; failed listing-price retrieval cannot.

Active unvalued interests from the previous notification policy are refreshed
once, never replaying sent or uncertain deliveries. `launch.activity` separates
informational outcomes and condition exclusions from verified valuations; unknown
valuation counts remain visible even after an information-only alert. Missing
mileage on a comparison candidate no longer raises an exception in peer search.

### Bounded active-listing supplement (2026-09-17)

`RIA_ACTIVE_WINDOW_ENABLED=true` adds a lower-priority check for listings whose
publication predates subscription activation, including newly matching older
offers. It requests only the latest 50 active IDs in each existing filter group,
without publication-date bounds, every five minutes. It queues at most one
previously unseen candidate per check. This is **not complete historical coverage**:
no next page is requested, and older cars outside that window can still be missed.
After unseen candidates, checked non-deals in the window may be refreshed after
30 minutes to notice a price change on the same ID. Sent/uncertain deliveries
are never replayed. Cards explicitly identify this supplemental discovery path.

Shared `informational` jobs are also eligible for that bounded 30-minute refresh,
but only for current interests with no delivery record for that user/listing.
An information-only alert to one user must not suppress another user's newly
matching price drop. Regression tests cover both sent and uncertain first-user
deliveries, an active versus stopped second subscription, and repeated checks.

Primary publication discovery and its jobs take priority. Supplemental work
starts only if a full 32-call bounded step fits below half of both rolling
hour/day allowances and within the existing absolute cap. Otherwise it pauses
as `reserved_for_new_publications`; no limits are enlarged. Runtime status labels
this as `latest_active_window_only`, never a full-source coverage guarantee.
The additive `monitor_active_windows` table records only that bounded window.

Live incident evidence: on 2026-09-17 at 22:39:36 UTC the configured recovery
report for AUTO.RIA 39767288 changed to `delivery_states={"sent":1}` with fresh
price USD 6200 and no market estimate (zero eligible comparables). This proves
Telegram API acceptance through the normal worker, not a phone push receipt or
automatic discovery of that ID by the supplemental window.
