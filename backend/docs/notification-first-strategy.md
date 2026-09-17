# AUTODeal: new worthwhile listings delivered to the bot chat

Decision: 2026-09-17. This replaces the full-catalog acquisition strategy.

## Product goal

A user creates a filter subscription in the Mini App. The server watches for new
matching AUTO.RIA listings, evaluates their price, and sends qualifying cars to
that user's private conversation with @auto_deal_finder1_bot. Monitoring continues
while the Mini App is closed. The initial threshold remains 15% below the median
of suitable comparable listings.

The Mini App manages filters, subscriptions and delivery settings. A large manual
car-search catalog, exhaustive historical scans, and acquisition of the entire
AUTO.RIA database are outside the new launch scope. OLX remains deferred.

## AutoSpect: evidence and access boundary

The user supplied https://t.me/AutoSpect_bot and described its flow: configure a
filter and receive new matching vehicles in the bot conversation. Public search
returned https://autospect.app/ with a Telegram-only notice, a RIA monitoring
section and a create-task action.

The browser rejected access to t.me because permission was not granted. No bot
conversation was opened, no command was sent, no subscription was created and no
end-to-end notification was observed. Do not claim to have verified AutoSpect's
internal implementation, actual latency, coverage, valuation, quotas or pricing.
A short user-provided recording of task creation and an example incoming alert
would enable a more detailed interaction review without account access.

## Existing implementation we can retain

- `Filters`, official dictionaries and Telegram-signed authentication.
- Server-saved `Search` subscriptions, ownership checks and stop controls.
- `monitor.py`: initial baseline, new-ID detection, per-subscription seen state,
  filter rechecks, price evaluation and evidence that a subscription matched.
- `worker.py`: private-chat delivery with photo, price, comparison and listing
  link; deduplication by user/listing; /stop recheck immediately before sending.
- Conservative handling of uncertain Telegram send outcomes and provider quotas.
- Public-card and comparable caches. They support new-car valuation without
  requiring the full source market to be downloaded.

## Original limitations found before the transition

This is a code review, not a successful live notification test.

1. The notification monitor is disabled in production. Release 30 separately
   disables the old full-scan worker by default and pauses its active jobs.
   This retirement does not activate notification monitoring or delivery.
2. `app.check_enable()` and `Monitor.tick()` permit one active subscription
   globally. This is a pilot limitation, not the intended product capacity.
3. `Monitor.poll()` checks at most three pending cars per cycle. Pending cars
   expire after five minutes, so a burst can lose evaluation coverage.
4. The monitor reads one 50-ID window. A window without overlap pauses with
   `window_gap`; it needs bounded continuation through new pages to the previous
   checkpoint, rather than a full-history crawl or silent omission.
5. Discovery, price evaluation and delivery currently share a cycle. Slow
   evaluations can delay discovery and message delivery.
6. A first-seen ID is not, by itself, proof of a newly published listing. Verify
   provider ordering and publication-time semantics; distinguish new postings
   from promoted, edited or newly matching older cars.
7. The existing estimator requires enough technically comparable cars. Missing
   data must remain unvalued, with a recorded reason, rather than a made-up deal.

## Implementation order

### 1. Stop the old full-scan product flow

Disable starting/resuming mass scans and pause active full-scan jobs, preserving
their saved data. Remove manual catalog scanning as the Mini App's primary action.
Make filter creation, save-and-enable, pause, edit and delete subscriptions the
main navigation. Keep an honest connection/readiness status and a route to /start.
Archived cars need not be deleted to change strategy.

Acceptance: reopening the app or editing filters causes no whole-market scan;
already running mass scans stop consuming provider requests; saved filters remain.

### 2. Make discovery and evaluation durable

Poll only active filter groups, initially grouping identical canonical filters.
Store a baseline/checkpoint on activation. Fetch new pages only as far as needed
to reach prior overlap, with a bounded, resumable gap-recovery state. Persist new
IDs before evaluation so restart, bursts and quota waits do not lose them.

Give each newly discovered listing a durable valuation job. Remove the total
three-car-per-cycle bottleneck; use fair, short work steps governed by the actual
request budget. Reuse a fresh detail/valuation across matching subscriptions.
Before sending after a long delay, recheck availability/price and label or expire
old opportunities explicitly; never silently claim current coverage after a gap.

Acceptance: a burst larger than three cars and a window larger than 50 IDs are
handled with recorded coverage; process restart does not duplicate or lose jobs;
first activation and promotion of an old ad do not cause an old-listing blast.

### 3. Verify worthwhile-price classification

Verify the existing conservative estimator against representative real listings,
including generation/modification, condition, gearbox, fuel, year and mileage.
Explain unvalued outcomes instead of treating missing comparable data as a deal.
Measure usable sample coverage and API cost; retain the >=15% rule and enough
comparable listings. Do not rerun the already-exhausted one-time validation audit.

Acceptance: defensible bargains and ordinary/uncertain prices are distinguished,
with traceable comparable evidence. The multi-user scheduler, shared valuation
jobs, deduplication and independent dispatch are implemented as part of step 2.

Step 3 implementation is recorded in [release 33's review](valuation-review-2026-09-17.md).
It verifies archived real outcomes without spending another provider request;
fresh coverage and production savings must still be measured in step 4.

### 4. Measure and enable a real monitored subscription

Use the owner's explicit subscription for an end-to-end live test. Record source
publication time when verified, discovery time, valuation completion and Telegram
acceptance. Measure delays and request usage, then set sustainable intervals.
Phone push presentation also depends on the user's Telegram notification settings.

The current code's discovery interval is 60 seconds. One distinct continuously
polled filter group therefore uses 1,440 search calls per day before details and
comparisons; shared identical subscriptions do not need separate source polling.
The target interval must account for the number of groups and arrival volume.
Do not promise one-second source-to-phone delivery from a polling design.

## What to store

Keep subscriptions, compact seen-ID/checkpoint state, queued new listings, recent
comparable data, valuation evidence and delivery records. Use retention policies
for old diagnostic/cache data. This is operational monitoring state, not a full
mirror of AUTO.RIA's database.

## Change status

Release 20260917-30 implements the first transition step: the Mini App's main
action creates a saved subscription; old catalog navigation is hidden; starting
or resuming mass scans is disabled by default; startup pauses existing mass-scan
jobs while preserving their data and saved subscriptions. Public source status
exposes the disabled flag and active-job count for rollout verification. Existing
notification readiness and consent checks remain in force. Backend and frontend
regression tests cover the shutdown guards and the subscription-only UI entry.

At release 30 the durable new-listing monitor, multi-subscription scheduling and
a real end-to-end notification test remained outstanding. That release did not
enable monitoring or delivery. No AutoSpect interaction was completed.

### Subscription management follow-up (release 20260917-31)

Step 1 now includes in-place name/filter editing, cancellation, visible per-card
states, confirmed deletion, and existing pause/activation controls. Up to 20
subscriptions can be saved per user. Changed criteria pause the subscription and
invalidate old monitoring evidence; activation stays explicit. Duplicate edits
return a recoverable conflict without overwriting either subscription. Local
drafts stay local, including edits. Backend tests cover ownership, paused edits,
old queued-message invalidation, duplicates and editing at the subscription cap.
Frontend tests cover complete filter restoration, save/cancel/retry, async
navigation and draft preservation. Monitoring/delivery remain disabled, with
the one-active-subscription pilot capacity deferred to step 2.

### Durable monitor (release 20260917-32)

Step 2 now implements shared canonical filter groups and a source-ID valuation
queue in three additive tables: `monitor_memberships`, `monitor_feeds` and
`monitor_jobs`. Existing deployed tables require no column migration. There is
no one-global-subscription cap and no three-car processing/5-minute queue cutoff.
Persisted subscription epochs and the per-user/listing unique delivery constraint
still enforce consent, cancellation and deduplication.

The official [search documentation](https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto)
was checked on 2026-09-17. It specifies `order_by=7` as newest date-added ordering,
`created_after`/`created_before` creation-time filters, and zero-based `page` with
`countpage`. Creation-time filters, rather than publication/update dates, exclude
older promoted/edited ads. The exact live provider behaviour still needs the
opted-in rollout check; synthetic fixtures are not evidence of live coverage.

Each activation records a cutoff rounded up to the next UTC second. Discovery
freezes date bounds, persists each 50-ID page and its recipient epochs, and only
advances the checkpoint after finishing a window. A verification pass for large
windows handles removal-induced page shifts; inconsistent pages leave the cursor
unchanged with a visible error. A two-minute overlap recovers recent indexing
delays. Longer outages are processed in bounded one-hour windows. Activation
boundaries split shared windows so later subscribers receive no earlier backlog.
First activation does not fetch details of the existing catalog.

Discovery and valuation alternate short budgeted steps. Distinct feeds and queued
cars get turns, while identical filters share polling and identical source IDs
share fresh valuation evidence. Candidate details are fetched fresh after waits;
pending work survives restarts and quota cooldowns. Unvalued cars remain explicitly
recorded. A separate loop dispatches messages without waiting on source HTTP calls.
Stale delivery records request a fresh valuation before dispatch; price increases
or unavailable listings invalidate their old match evidence.

The scheduler targets at least 60 seconds between ordinary polls, growing the
interval with distinct group count to reserve half the hourly/day caps for detail
and comparison calls. Catch-up and boundary slices cost extra requests. Hard caps,
provider cooldowns and the cumulative counter remain unchanged. Indexing delay,
provider data quality, workload and quotas prevent a one-second delivery promise.

Regression scenarios cover 117 new cars over three pages, restart after the first
page, head deletion, later insertion, delayed indexing, transient/total quota
pauses, pending work older than five minutes, multiple users and overlapping
filters, late activation, /stop during work, stale queued prices, independent
dispatch, repeated provider pages and insufficient valuation data. All use fake
provider/Telegram responses and spend no production requests.

This release publishes the implementation with `MONITOR_ENABLED`, `SOURCE_READY`,
`DELIVERY_ENABLED` and `FULL_SCAN_ENABLED` still off. It creates no paid resources,
sends no real messages and does not activate any saved subscription. Remaining
launch work: step 3 valuation verification, then step 4 the owner's live monitoring
and delivery check, measured cadence/cost, PostgreSQL concurrency/load behaviour
and operational-state retention before scaling beyond the initial rollout.
