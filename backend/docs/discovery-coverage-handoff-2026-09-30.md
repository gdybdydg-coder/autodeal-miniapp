# Discovery coverage handoff — 2026-09-30

## Verified release at 19:54 UTC / 22:54 Europe/Kyiv

Current deployed main: `901d96fe1f7e10196155ef6dd329da9d62f2a250`, tested tree
`0f1392a7cc7a3f5c8e2eba7af0851561b97e4ef2`. Render
`dep-daumfo59fdbs739haga0` became live at 19:50:59 UTC. All **951 backend
tests passed** (265.07s); 108 selected recovery/monitor/quota/fallback tests
passed. Local/remote trees matched before non-force main update. Render had no
automatic deployment after 146 seconds of rechecking; one manual deployment
was started, with no duplicate.

Post-deploy health: PostgreSQL connected, delivery available, no runtime error
logs. All 61 groups watching/successful, oldest cursor lag 110s,
needs_attention=false; valuation and Telegram queues empty. Provider calls
were available under unchanged 4500/hour, 90000/day, 1102160 total limits:
2750/hour, 32670/day, total used 228316, local remaining total 873844.
This is local accounting, not the provider's package balance.

The new shared source retained its baseline `1790793196.5574977` and accounting
across the restart: watching, 63 cycles, 46 API-validated publication jobs,
113 extra paid requests, 123 waiting candidates, cumulative overflow 195.
Validated jobs are not a count of profitable cars or sent messages. Category
rejections 19, no eligible subscriptions 34, mismatched addition date 1,
unavailable details 1. Last unexpected_html is historical; successful collection
resumed afterward. Whole-market recall and net request savings remain unproved.
Two-page sampling, bounded queue/budget and source failures remain limitations.

Active window/initial inclusion remain false, confirmed-only true. /stop,
recipient epochs, sent/uncertain claims, saved filters and minDiscount are
preserved. The two historical recoveries were not repeated. Diagnostics still
select Leaf, whose six-request VIN/date probe is complete and is never reopened.
Night polling remains enabled at 3600s; day target 110s, evening target 60s
(current 61-group budget-adjusted interval 66s).

This final handoff update changes documentation only on the WIP branch; the
deployed main above remains the exact tested code/tree. Later source behavior
must be checked anew; this is a timestamped sample, not a delivery-time guarantee.

The b613bbdf live runtime snapshot is recorded in
[discovery-coverage-verification-2026-09-30.md](discovery-coverage-verification-2026-09-30.md).
It includes the now-confirmed Leaf diagnostic, live fallback processing and
the subsequent source errors/queue limitations. The current recovery follow-up
is described below; the fixed snapshot retains the code/deployment of its stage.

## Recovery from a stale frozen publication window

The live incident later showed successful primary retries but a 306s-old cursor
even when all 61 groups were watching. A frozen window's catchup flag reflected
its opening time, so completing it after an error/quota wait could add a normal
poll interval before moving to fresh publications. The follow-up checks window
age at completion: after closing a window older than the planned interval,
continue immediately from its preserved end. Short normal HTTP latency still
uses the planned wait. No cursor gaps, filter/activation resets, old-catalog
searches or delivery claim changes are introduced.

Four new regression cases cover connection/upstream/quota waits, exact frozen
window replay, accounting failed requests, immediate bounded catch-up, shared
evaluation/dispatcher deduplication and /stop during the wait. Selected catch-up,
monitor, parallel, quota-resume and fallback tests: **108 passed**. Full backend
regression: **951 passed in 265.07s**, no failures or skips. The Starlette
deprecation warning predates this change. Before the follow-up deployment,
the source recovered by ordinary retries: all 61 groups watching, cursor lag
115s, needs_attention=false, empty Telegram delivery queue and quota available.
The catchup change avoids the extra planned wait on the next such incident;
it does not claim to prevent upstream failures or prove maximum delivery delay.

The initial investigation below was saved as an undeployed WIP. The September
30 follow-up validated the idle-slot scheduler: **892 backend tests passed**.
The failing seven-recipient test was inspected: all seven durable Delivery rows
existed (three sent, four pending), and independent dispatch sent the remaining
four. The test now checks all persisted recipients and runs the production
delivery batch. Five added tests cover four simultaneous due feeds, unchanged
poll deadlines, one shared detail/AI evaluation, last-request hourly/daily/total
caps, the global lease, and /stop during concurrent search.

## Live fallback follow-up

Fallback commit `38d8ee4d55289be66acba9fbd44a47c47d96527d`, tree
`94f6c34f4cdd3fe173d0e336fa4b4b89242925b0`, was tested with 937 passing backend
tests and deployed as `dep-daulbd41nsns73eoe1bg`, live 18:33:20 UTC. The flag
was enabled through a safe environment merge; that update automatically created
the single deployment, so no additional manual deploy was triggered.

The baseline completed at about 18:33:16 UTC. After eight cycles, 18 fresh
candidates were queued but none had reached details and no fallback API call
was spent. Primary batches continuously filled all four slots, so the idle-only
intake branch could starve. All 59 primary groups were successful, sampled cursor
lag was 91–98s (needs_attention=false), and Telegram delivery remained healthy.

The follow-up permits one shared intake after a completed full primary batch,
at most every 15s with a durable throttle. All selected primary searches/quotes
finish first. Due primary valuation backlog, catch-up/error feeds and cursor
lag over twice the planned interval (minimum 120s) suppress the extra step.
Each actual call still obeys the unchanged global ledger, 20% primary headroom
and 200/hour, 1000/day extra cap. Bounded selected diagnostics may use the same
healthy post-batch opportunity. No epochs, delivery claims or accounting are reset.
Ten additional regression cases cover saturation, operation ordering, restarts,
primary pressure, hourly/daily/total gates, /stop and diagnostic scheduling.
The final full backend regression passed **947 tests in 264.95s**; the selected
fallback, diagnostic and parallel-discovery run passed 60 tests. No failures or
skips; the one Starlette deprecation warning predates this change. Before moving
main without force, compare the remote tree with this tested local tree, then
check whether Render started an automatic deployment before a manual trigger.

The scheduler change was deployed as `b918c958ee0a01573fd3d5a77ae47aa585931b96`,
tree `a995fc703687a47c06f8c6b41e1757e96989ba11`. Render deployment
`dep-dauki8s9v7es739u8ih0` became live at 17:39:51 UTC. It changes no
configured cadence, hard caps, epochs, delivery claims or active-window policy.
Post-deploy cursor lag samples were 86s and 100s, needs_attention=false, with
empty queues. Active groups rose from 54 to 56 through ordinary user activity;
these are samples, not a worst-case latency guarantee.

The follow-up also implements an opt-in shared dated-publication fallback, in
`backend/recent_publications.py`; see `backend/RECENT_PUBLICATIONS.md` for its
evidence, source-clock assumption, bounds and limitations. Full backend
regression: **937 passed** (284.36s). Selected fallback/diagnostic tests: 57
passed. It does not enable active-window scanning or reuse the old HTML shadow
as production evidence. The feature starts with a durable baseline when the
operator enables `RIA_RECENT_PUBLICATIONS_ENABLED`.

Mazda 32704870 was now diagnosed: no job/seen record, 15 matching active
subscriptions, absent in documented created/published VIN searches since
September 18, found using the same VIN filter without date bounds. Six counted
requests; no VIN retained. This proves the date-search omission, not a seller
action or exact initial publication time.

The explicit diagnostic selectors now select Leaf 40369208. Startup of the
configuration-only deployment `dep-dauks1e0tbcc73bmheig` (live 18:00:34 UTC)
was blocked by a busy source lease. The follow-up code retries only unclaimed
selected probe stages on at most six idle or healthy post-batch ticks; durable used probes
and their caps are never reset. Leaf remains unconfirmed until these logs are
read after the new code is live.

HTML shadow 20260929-v1 completed: 274 successful cycles, 558 reserved HTTP
requests, 821861263 downloaded bytes, 8192 unique IDs, 200 baseline IDs,
3032 IDs also in existing API jobs and 2835 post-baseline overlaps. It created
no notifications/valuations/jobs and used zero paid API calls. This does not
establish complete recall or net request savings.

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

## Initial WIP and validation (superseded)

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

## Original proposal — implemented in the follow-up

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

The original requirements were a durable initial baseline, bounded candidate queue, /stop/activation
cutoffs, restart-safe deduplication, global shared details/AI quotes, and explicit
rolling HTTP/API budgets. Yield to production load, respect robots and deny/429
responses; never use cookies, hidden endpoints or CAPTCHA/IP bypass. Do not
scan the old catalog or periodically reevaluate old prices. The follow-up
implementation adds these safeguards and requires matching official/API and
HTML addition dates; details are in RECENT_PUBLICATIONS.md.

On the September 30 follow-up, the user explicitly selected inclusion of fresh
repeat publications under an old ID, provided there is fresh addition-date
evidence, official details/valuation and subscriber-filter validation. Updating,
raising or repricing an old ad is not sufficient evidence. Historical active
catalog scanning remains forbidden.

## Operational safeguards (initial snapshot)

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
