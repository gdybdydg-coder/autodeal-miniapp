# Discovery coverage handoff — 2026-09-30

The initial investigation below was saved as an undeployed WIP. The September
30 follow-up validated the idle-slot scheduler: **892 backend tests passed**.
The failing seven-recipient test was inspected: all seven durable Delivery rows
existed (three sent, four pending), and independent dispatch sent the remaining
four. The test now checks all persisted recipients and runs the production
delivery batch. Five added tests cover four simultaneous due feeds, unchanged
poll deadlines, one shared detail/AI evaluation, last-request hourly/daily/total
caps, the global lease, and /stop during concurrent search.

The scheduler change is ready for ordinary main deployment. It changes no
configured cadence, hard caps, epochs, delivery claims or active-window policy.
The original production baseline at the start of this follow-up remains
`297a9a7920b9af4a1af06002ae1ff8217a8388d3`, tree
`3782b3e204dab97ee5a2dc2338591612e5658a32`.

## Incident evidence

The bot is running, not globally stopped. The configured owner's Telegram chat
had an accepted delivery at 18:43:34 Europe/Kyiv on September 30. This is Telegram
API acceptance, not proof of a device push. All 53 feeds have successful searches,
but the oldest cursor sometimes lags over 300 seconds and reports attention.
Quota was available; delivery queues were empty in sampled checks.

Reported listings, identified from the competitor screenshots:

- Mitsubishi Colt 2009, Novo Selytsia, USD 3300: `40369158`.
- Mazda 5 2010, Ternopil, USD 5300, plate BO1329CP: `32704870`.
- Nissan Leaf 2013, Ternopil, USD 5200: `40369208`.

Colt has no monitor job, no seen subscriptions, and no delivery record. Its
current details matched 8 active subscriptions. A bounded, documented VIN
diagnostic found it without date constraints, but NOT in either created- or
published-date searches covering September 18 to September 30. The three probes
used 1 + 3 + 2 = 6 counted API requests. No VIN or credentials were retained.
This proves a date-search omission, not the original publication date or a
particular seller action. Public indexes had the same IDs before September 30,
while their current detail pages display September 30.

Mazda's owner trace is also not_observed. Its API diagnostic did NOT run because
the source lease was busy. Leaf's precise database/API path is unconfirmed.
Do not present Colt's result as proof for all three.

## Current partial change and validation

The scheduler implementation changes backend/monitor.py. Selection reads up to four due feeds;
after reserving the existing due-search and valuation slots, otherwise idle
slots can process independent due feeds. Poll deadlines, quota gates, global
leases, epochs and delivery deduplication are retained. This targets observed
search lag without increasing scheduled polling frequency.

Initial WIP validation (superseded by the 892-test follow-up above): the base code passed 71 selected parallel/active-window/HTML-shadow tests before
the edit. After the edit, parallel + monitor tests returned 54 passed, 1 failed:
`test_shortened_main_poll_delivers_new_listing_without_rechecking_old_ads[7-68]`.
Only 3 of 7 expected recipients were sent by the test helper. Hypothesis: the
legacy drain helper sends one message per work tick and exits with independent
delivery work still queued after faster discovery. This is NOT yet proven.
Inspect delivery rows and test the independent dispatcher; do not weaken a real
delivery regression. Add explicit four-feed overlap, quota and /stop tests, then
run the complete regression suite before merging. No full suite was run for
this partial change. Main's previous 887 passing tests are not its validation.

## Proposed next work — NOT implemented

Investigate a shared fallback using two public /uk/last/hour/ pages, instead of
adding one paid active-page search per filter. Existing shadow is observation
only and must never be silently treated as a production source.

A live public-page sample had 100 cards: 98 organic and 2 paid cards. Of the
organic cards, 86 had add dates under one hour old; 12 were older. Cards expose
separate data-add-date and data-update-date fields, plus USD preview price.
First-seen ID alone, an updated timestamp, or inclusion in last/hour is NOT
adequate new-publication proof. Exclude promoted/new-car cards and old additions;
confirm passenger-car category, current positive price and full subscriber
filters through the official API before valuation/delivery.

Design a durable initial baseline, bounded candidate queue, /stop/activation
cutoffs, restart-safe deduplication, global shared details/AI quotes, and explicit
rolling HTTP/API budgets. Yield to production load, respect robots and deny/429
responses; never use cookies, hidden endpoints or CAPTCHA/IP bypass. Do not
scan the old catalog or periodically reevaluate old prices. No parser, production
collector, fallback intake or configuration for this proposal exists yet.

On the September 30 follow-up, the user explicitly selected inclusion of fresh
repeat publications under an old ID, provided there is fresh addition-date
evidence, official details/valuation and subscriber-filter validation. Updating,
raising or repricing an old ad is not sufficient evidence. Historical active
catalog scanning remains forbidden.

## Operational safeguards

Render service srv-dal2h35g1s2s73e0sj80, workspace
tea-dakrf02fngtc73dvk4dg. Live deploy at this handoff:
dep-dauisn8jo6nc738jl8t0, live September 30 15:45:32 UTC, same production commit.
This deploy changed only diagnostic environment selection, not code.
RIA_OWNER_TRACE_LISTING_ID and RIA_DIAGNOSTIC_LISTING_ID currently select
32704870. They are bounded diagnostics, not recovery or discovery features.

Active-window and initial inclusion remain OFF. Confirmed-deals-only remains ON.
Kyiv polling targets: 23:00–08:00 3600 s; 08:00–18:00 110 s;
18:00–23:00 60 s. Software caps: 4500/h, 90000/day, 1102160 absolute total.
Do not reset accounting or confuse local remaining capacity with provider balance.

HTML shadow run 20260929-v1 samples two global pages every 300 s for 24 h,
ending September 30 about 21:18 Kyiv. It creates no jobs/quotes/notifications
and makes zero paid API calls. Its partial overlap cannot prove whole-market
coverage or money saved. Check its current aggregate status and deadline.

Do not print secrets or full source-status. Read only monitor, launch.activity,
budget and quota. No database firewall changes or credential extraction.
Never reset stopped subscriptions, epochs or sent/uncertain claims. Do not repeat
Yaris 39767288 or Volvo 37319411 recovery. Update main without force only after
tests; compare remote and tested local trees. Check automatic Render deployment
before triggering a manual one. The user authorizes justified fixes and normal
deployment on the existing service; no automatic purchases.
