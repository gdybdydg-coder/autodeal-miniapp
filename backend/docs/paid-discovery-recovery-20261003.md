# Paid AUTO.RIA discovery: night delay and retired HTML interests

## Verified starting point

Production/main `e4657cc6c91fc8c48a62654176f16bcca383d064`, Render
`srv-dal2h35g1s2s73e0sj80`, deploy `dep-db0ik66gekts739s8nn0`.
Local parent `ebaf242` has the identical tree
`bf36f3af60226379df5cfae8bbc85dbdc7943ddc`.

The October 3 20:03:25 UTC / 23:03:25 Europe/Kyiv private production snapshot
showed three current confirmed paid clients, three enabled/ready searches,
three shared groups, a healthy source process and a 3600-second night interval.
The last successful primary poll was at 19:59:37 UTC. The last accepted client
car message was at 18:14:35 UTC / 21:14:35 Kyiv and its owner copy at
18:14:51 UTC. Acceptance means Bot API acknowledgement, not reading.

That snapshot recorded three cancelled search/car interests. This alone does
not prove that a particular screenshot or all clients were affected by the
bug. Direct read-only PostgreSQL access remains unavailable because the
database's external allowlist is empty. No firewall or secret was changed.

## Reproduced defects and changes

1. Strict paid discovery inherited the legacy 23:00–08:00 hourly timer even
   with three groups and ample configured capacity. Only strict paid discovery
   now requests a 60-second night interval. Day stays 110 seconds, evening 60.
   Rolling hard caps and the planning reserve of 25% remain unchanged. Large
   cohorts or small configured caps still lengthen the interval safely.
   Unpaid/stopped searches still cannot create source groups or HTTP work.

2. HTML-origin interests expired or disabled before valuation were marked
   cancelled. Later dated primary API discovery skipped those seen rows,
   permanently preventing normal primary evaluation for that search epoch.
   Dated primary discovery can now promote only specifically retired,
   unclaimed HTML interests. Saved legacy valid expired proofs are supported.
   Missing/invalid proof and unrelated final outcomes are not reinterpreted.

3. Retirement preserves its reason and source proof. A parallel primary
   confirmation wins over stale HTML cancellation. Cancellation cannot rebuild
   a delivery match from an old quote. Private diagnostics distinguish retired
   proof from ordinary filter/discount rejection.

No sent, uncertain, pending, failed or cancelled Telegram claim is reopened.
No whole-catalog rescan or old-payment backfill is added. Cursors, watch epochs,
filters, subscriptions, price calculation, primary pagination and retries are
preserved. Recovery requires a result from the ordinary dated primary window;
this is not a promise to recover already missed ads outside that window.

## Validation

`backend/tests/test_paid_discovery_recovery.py` uses temporary SQLite, fixture
AUTO.RIA/Telegram and the backend network fence. Tests cover the 23:00 transition,
winter/DST and budget scaling, expired/disabled/legacy HTML recovery, restart,
per-recipient claim deduplication, no bypass for unpaid, stopped or disabled
searches, and concurrent primary promotion over expired HTML proof.

Before the fix, the identical tree to production failed all four isolated
night/retirement reproduction cases (35 other cases deselected, 1.98 seconds).
The retirement cases include two paid recipients sharing one detail and quote;
promoting the shared job for one must not strand the other's retired interest.
After-change regression: 616 cases passed across 22 affected suites in 139.15
seconds, including all 39 new recovery/budget cases. One existing Starlette
dependency deprecation warning remains. Live receipt limits are recorded in
the private result report; synthetic receipts are not live delivery evidence.

## Costs and rollout

At three groups, the one-page/poll, standard 24-hour planning estimate changes
from 1910 to 3502 shared publication searches/day (rounded upward), around
1592 more. This is a planning estimate, not measured provider charges. It
excludes extra pages, dictionary misses, details, pricing and retries, which
all pass the existing durable local caps. No quota was purchased or increased.

The limited two-page public intake keeps its independent hourly/daily API and
HTTP ceilings and its original wall-clock schedule. It does not establish
whole-market coverage. Ads that don't pass the saved user discount/filter,
lack a trustworthy provider range, or cannot be retrieved are not guaranteed
messages. The provider lower bound minus 5% and user threshold are unchanged.

No database DDL migration is needed; retirement names fit existing string
columns. Deploy only the reviewed files to the existing Render main service,
after checking current remote head and workflow/deployment configuration.
Do not force refs or launch a second build while an automatic one is running.

Rollback: redeploy the recorded previous paid-gated release through a reviewed
revert commit; do not revert the current paid-access gates, mutate payment
history, delete claims, reset cursor/epochs or purchase quota. New HTML retirement
state already existed in that release. Rollback restores the old night delay,
so use it only for an observed regression and retain the incident evidence.
