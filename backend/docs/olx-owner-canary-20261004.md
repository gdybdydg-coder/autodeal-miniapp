# Owner-only live OLX observation test

User authorized on 4 October 2026 at 13:00 Europe/Kyiv: integrate/test OLX only
for their account. This is separate from the previous offline branch approval.

Base: main/live 57e357d plus isolated experiment commit 37510df. The only current
runtime change is additive app wiring and backend/olx_owner_canary.py. The RIA
monitor, source budgets, pricing, filters, manual purchases and copy retirement
remain unchanged. Current source policy requires approved current paid purchase,
active entitlement, User.ready and an enabled search belonging to configured
owner; administrator role alone never grants access.

Default off. Enable with only these additional merged Render env variables:
OLX_OWNER_CANARY_ENABLED=true; OLX_OWNER_CANARY_UNTIL=an explicit Unix deadline.
This session's intended canary is at most two hours. The first initialization
pins the owner's then-enabled search IDs, separately from saved filter JSON.
There is no auto opt-in for other users. Current filters/paid access are reread.

OLX gets an independent SourceProbe state/lease/seen-ID list, independent task and
request counters. It polls at most once every 300 seconds, up to one newest public
search HTML plus two unknown details. Hard caps: 40 GET and 80 MiB reserved bytes;
each GET reserves 4 MiB BEFORE its transport, so at most 20 full reservations fit.
Actual reads are also recorded. Initial snapshot is baseline, never a flood of
old cars. Whole catalogue completeness is explicitly false. No RIA request,
valuation cache, external paid model/service, proxy, credential, internal API or
CAPTCHA bypass. Public non-200 responses do not retry inside a request; 401/403/429
stop the canary. Different public fields and timestamps remain unverified.

This is an acquisition/eligibility/filter/valuation-diagnostic stage, not a
working profitable-car feed. Full HTML cannot yet prove original seller currency,
first publication and sufficient independent comparables. Such cars stay
unconfirmed and are NOT sent as deals. One real private diagnostic summary is
authorized by the user and sent only to owner after current-access recheck. The
durable sending reservation is unique; accepted records require actual Telegram
API message_id. Uncertain reservations never replay. API acceptance is not phone
push/read confirmation. No synthetic fixture is inserted into production.

Private commands: /olx_status and /olx_stop. They only handle the configured owner
in their own private chat and verify an optional bot mention. /olx_stop pauses
only the OLX state, leaves User.ready, subscriptions, RIA and payment rows intact.
Existing /stop still prevents source calls and the diagnostic send via shared
ready predicate. Source and diagnostic receipts are logged without IDs, tokens,
filters, raw seller descriptions or contact data.

Isolated tests: backend/tests/test_olx_owner_canary.py, temporary SQLite with
actual strict purchase policy, fake OLX bytes and fake Telegram acknowledgements.
They cover owner-only boundaries, unpaid/expired/review/stop, later activation,
per-request access, 403 stopping, byte reservation, exclusive lease, baseline and
restart deduplication, uncertain send and owner command isolation. Full backend
regression is executed before release; parser tests remain separately fenced.

Disable: /olx_stop for owner or merge OLX_OWNER_CANARY_ENABLED=false. Automatic
deadline/caps also stop intake. Preserve SourceProbe seen, lease and notice
history. Rollback app/module wiring without touching RIA or payment tables.

Release verification must identify the live commit, actual OLX pages/details and
Telegram diagnostic receipt. /health and offline test totals are not sufficient.

Owner-requested test advertisements, 04 October 13:30 Kyiv: the user explicitly
confirmed sending test adverts. `OLX_OWNER_CANARY_TEST_ADS_ENABLED=true` enables
one capped batch of two attempts, only for the same current confirmed-paid owner
and pinned enabled searches. Two grounded regional detail URLs are refreshed
before sending; previous discovery baseline does not suppress this explicit
sample batch. Existing 80 MiB/40-GET/deadline caps remain unchanged.

Require complete identity-matched details, allowed eligibility and corroborated
displayed asking amount. Known contradictions to current named/numeric filters
block the sample; requested regions must be observed in detail. No FX is assumed.
Only this explicitly labeled sample batch may bypass unconfirmed full-price,
original currency, publication and profitable-deal valuation proof. Saved filters
and ordinary onlyDeals/threshold rules stay intact. Test captions state unknown
newness and profitability and use original displayed currency; non-USD budgets
remain visibly unresolved. Samples use first official photo when available and
an OLX link. A separate atomic unique test ledger reserves each attempt before
transport and rereads permissions/filter comparison immediately before dispatch.
No retries/fallbacks for uncertain photo or text attempts; total cap two attempts
including rejected/uncertain ones. Receipts require a positive integer message
ID and a matching owner chat ID if supplied. `/olx_stop` and expiry still apply.
