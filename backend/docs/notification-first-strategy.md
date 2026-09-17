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

## Current limitations found in the code

This is a code review, not a successful live notification test.

1. The notification monitor is disabled in production. Release 29 still starts
   the full-scan worker when an API key is present; changing this document does
   not stop active scans.
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

### 3. Deliver to multiple opted-in users

Replace the global one-subscription pilot slot with fair scheduling and explicit
capacity based on available provider requests. One valuation can serve several
matching users; a user receives the same source listing once even when multiple
subscriptions overlap. Recheck /stop and subscription ownership before delivery.
Decouple the delivery queue from provider discovery/valuation delays.

Acceptance: multiple users and filters work; overlapping filters do not duplicate
alerts; disabling a subscription or /stop prevents queued messages from sending.
No messages are enabled for users who have not opted in.

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

The strategy and code findings are recorded. No production settings or delivery
flags were changed by this analysis; no AutoSpect interaction was completed.
The runtime transition above remains to be implemented and verified.
