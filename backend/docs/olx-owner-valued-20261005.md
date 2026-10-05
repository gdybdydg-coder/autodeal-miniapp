# OLX valued owner canary — 2026-10-05

The owner explicitly authorized only their personal chat. AUTO.RIA remains unchanged.
Selected asking-price, attribute, currency and evaluation modules were ported from
research f95f1c150ff0240cdb2e69a6e83af5c55d13b29d; the research branch is not merged.

## Deployment state

Default OFF; no profile is installed. Startup preflight reads only the protected
configured owner's genuine confirmed purchase, ready status and enabled searches.
No payment/search/delivery writes or source requests are made by that diagnostic.
`technical_ready=true` is not an accepted override. Client authorization is always false.

Enabling requires `OLX_OWNER_FEED_ENABLED`, one pinned `OLX_OWNER_FEED_SEARCH_ID`,
a future `OLX_OWNER_FEED_UNTIL`, protected owner identity and both older OLX switches OFF.
The profile must contain reviewed real current details, a previously frozen split,
8+ compatible unique VIN-derived advertised vehicles with distinct photos reviewed,
3+ independent asking-price controls, the lowest holdout MAPE method (maximum 15%),
and complete leave-one-out stability (maximum 5%). Asking prices are not sale labels.
A target must be current within 300s, match the unchanged pinned search/discount
threshold, and pass full-page offer/category/currency/attribute checks.

## Independent state and transport

Uses a new SourceProbe namespace; no AUTO.RIA queues/records are modified.
A separate single executor runs at 600s daytime / 3600s 23:00–08:00 Europe/Kyiv.
Source caps: 12 GET/hour, 60 GET/Kyiv day, 160 MiB/day, 2 MiB/detail,
4s spacing, bounded 20s source transport, no automatic retry. 401/403/429 or
challenge => persistent OLX-only hold. `/stop` still blocks all source work.
`/olx_stop` pauses only this namespace. `/olx_status` reports only OLX.

This first canary is capped at THREE total attempts, including rejected/uncertain.
It uses an explicitly reviewed candidate URL list; continuous discovery is not
implemented and must not be described as running observation. Older ledger keys
are consulted and never reset. GetChat verifies the exact protected private owner;
paid access and search fingerprint are rechecked immediately before send.
Accepted/rejected/uncertain receipts are separate; no timeout retry or photo fallback.

## Verification before any enable

Full offline regression: 2294 passed + 150 subtests (333.70s), before final targeted
sender/profile checks. Expanded owner/new+legacy transport suite: 123 passed (6.66s).
All tests use temporary databases and fixture transports behind an import-time
network fence. Synthetic controls do not prove real market coverage or delivery.
No real OLX Telegram sends are authorized by test successes alone.

## Current real-data verification after disabled deploy

2026-10-05: protected owner verified; confirmed paid/ready; one active search #4,
1000–7000 USD, Vinnytsia/Ternopil/Khmelnytskyi/Chernivtsi, threshold 10%.
New public focused A5 1.6 TDI search HTTP200 (3,624,200 bytes), followed by
14 complete details HTTP200 (22,041,145 bytes) under committed reservations.
13 refreshed existing details and one new ID => 72 unique saved details, 0 estimates,
maximum4/8 compatible peers. 8/14 have explicit power; only1/14 has a validated
public VIN-derived claim. Three separately frozen controls:2 eligible,0 estimated,
maximum3 peers. Asking error metrics remain unknown; no method winner selected.
No real OLX Telegram calls/receipts. Do not enable on the strength of synthetic tests.
Fresh official dated PrivatBank fallback:44.9857000 UAH/USD, effective2026-10-05,
acquired07:02:57Z. NBU403 hold was respected; no repeated request to NBU.

Reproduced and fixed OLX-only mismatch of Russian vs Ukrainian names of the four
allowed regions:4 failures before /5 pass after. No geographic range was added,
and saved filters were not edited. Expanded new+legacy owner suite:135 passed.
Source regular-use authorization and complete publication semantics remain unknown;
bounded publicly accessible HTML is not a demonstrated commercial catalogue API.
A continuously discovering observer is not installed. Code/preflight are deployed
defaultoff; authorization for owner testing is separate from technical readiness.
