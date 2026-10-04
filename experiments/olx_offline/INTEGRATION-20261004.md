# AutoDeal OLX: continuation on current main, 4 October 2026

Status: **isolated branch/test integration; no OLX production launch or Telegram sends**.
Parent main/live: `57e357d59896655791650600ad9f6615c48444f5`.
Imported only `experiments/olx_offline` from latest prior OLX branch commit
`6e5506e61000e8a00e3074e474f95952b15c1adb` (3 October). The older report
`AutoDeal-OLX-research-20261002.txt` was read fully; its `d84a4e9` and 28-case
totals are historical. Latest pre-change baseline was **228 cases**, all passed.
No current production payment/filter/quota/promotion/schedule/OLX code was replaced.

## 1. Competitor: observed versus advertised versus unknown

Official public profile: https://telegram.me/BullAutoRadarBot, observed
2026-10-04 09:28:08 UTC / 12:28:08 Europe/Kyiv. Name BullAutoRadar and exact handle
are observed. Its promises about early deals, market analysis and 24/7 alerts are
advertised; no actual car message, menu, OLX link or timestamp pair was available.
14 public searches did not locate a concrete demonstration. That does not prove
the features absent. Other Bull/Radar accounts were rejected as different bots.

| Capability | Competitor proof | Existing AutoDeal OLX gap | Implemented here | Verification |
|---|---|---|---|---|
| OLX source/link | User report; independently unknown | Offline parser only | Bounded real HTML facts, safe OLX button | 90 real IDs on two partial catalogue pages; button tests |
| Filters/all cars versus deals | Unknown | Synthetic maps; every mode required an estimate | Current Filters bridge, separate explicit opt-in, onlyDeals respected | Known mismatches, missing optional fields, filter edit, 31 backend cases |
| Market reference/% | Advertised analysis, formula unknown | Experimental Q25; no real accuracy | Independent OLX-only peers, latest verified vehicle evidence, card provenance | Three-method frozen synthetic comparison, real labels = 0 |
| UAH/USD | Unknown | FX pure module already present | Shared caller-owned quote; full card date/source/approximation | Decimal tests; current live NBU request failed 403 |
| Photos/features | Unknown | No market evidence in old card; broad HTTPS photo rule | Escaped complete card and OLX CDN restriction, text fallback | 15 renderer cases |
| Bumps/republication/price edits | Unknown | Date semantics not verified | Retain negative newness holds; real refresh example documented | BMW creation 28 Sep versus refresh 4 Oct; restart/refresh tests |
| Customs/parts/donor | Unknown | Full text price scan stopped after 4000 characters | Eligibility retained; price scan covers complete bounded description | Real Audi rejected; real BMW/Mazda allowed eligibility; UA/RU tests |
| Duplicate/speed behavior | Unknown | Proven two-worker duplicate | Atomic claims, current-policy rechecks, uncertain outcome retained | One fake sender invocation after two workers; real latency unknown |

Evidence: `evidence/competitor-public-20261004.json`. No Telegram session, purchase,
administrator contact, permanent competitor forwarding or branding copy occurred.

## 2. Prototype behavior and proven fixes

`backend/olx_test_adapter.py` is a separate, unregistered integration boundary.
It refuses a non-SQLite or unlabelled engine and refuses a non-strict purchase
policy. It reads actual backend `paid_source_access.allowed`, User.ready and
Search.enabled before planning, every page, queueing and local fake transport.
Source opt-in uses explicit existing search IDs outside the current Filters/DB;
old searches have no automatic OLX opt-in. Switch is default off. It has no
Settings/.env/provider/Telegram client and writes no backend rows.

One invalid dictionary search (`brand="84"`) was proven to abort the entire paid
cohort. It now holds only that search with a reason; the next paid client works.
SQL access failures are technical holds, not nonpayment. Administrator role alone
does not grant access; a genuine confirmed paid owner follows the same ordinary
path. Unpaid-first does not poison paid users. Expiry, /stop, disabled search,
removed opt-in, kill switch, changed filters and threshold are rechecked.

The old pipeline was proven to invoke two fake senders for one listing/recipient.
Atomic pending-to-claimed transitions and unique tokens now prevent that. A
second worker does not rewrite another active attempt. Expired unattempted claims
can recover; attempted/ambiguous outcomes remain uncertain without automatic replay.
Current search, source, price, listing fingerprint, proof age and policy are
rechecked immediately before the injected sender. Queue capacity includes claims.

Known full-price/eligible/new rows can use onlyDeals=false while unknown valuation
stays visible. Missing fuel/gear/photo alone does not deny this mode. The deal mode
requires an estimate meeting the current user threshold. Full price/publication
uncertainty remains a hold, never a fabricated deal.

The older full-price context cap incorrectly held every complete description over
4000 characters while never inspecting its tail. The new bounded full scan checks
131072 characters, including deposit/monthly/parts-price conflicts at the end.

Market comparison could retain an older verified crosspost after newer invalid
price/FX/spec evidence. Seven regression subcases failed before correction. The
latest verified vehicle evidence now takes precedence and equal-time conflicts
hold the vehicle. OLX targets select only OLX peers; no RIA paid valuation/cache.

## 3. Real independent acquisition channel and limits

Public HTML currently works for bounded acquisition and offline parsing. Direct
research budget: **9 OLX GET + 1 dated NBU GET**, zero paid RIA or Telegram calls,
no login/proxy/CAPTCHA/rate-limit bypass or automatic retry. OLX bodies total
18,847,906 bytes. The first conservative category sample was truncated; the later
explicit 4 MiB cap yielded complete HTML, not a complete category.

Two fully downloaded newest pages: 4,050,495 and 4,045,226 bytes, 52 cards each,
40 organic + 12 promoted each, **90 unique IDs / 14 overlaps**. Pagination showed
up to page 25; pages 3–25 were not collected. JSON-LD misses 32/52 cards on page 1
and all cards on page 2; DOM card extraction is necessary. No completeness/recall
claim. Collection remains partial, and durable progress does not seal it complete.
Observed cache headers were search max-age=300 + stale-while-revalidate=60 and
detail max-age=600. No sustained freshness or detection-latency measurement.

Official FAQ q6 says partner API accesses only the authorized owner's ads:
https://developer.olx.ua/ua/articles/faq. It is not an all-seller search/feed.
https://developer.olx.ua/api/doc and API terms were inspected. Robots disallows
RSS/contact/account paths; public category/details are not disallowed there, but
robots is not contractual permission. Current general rules page returned
Loading/CSS Error. Regular commercial collection/reuse permission is unresolved.
No invented RSS/internal endpoint or paid provider was substituted.

Evidence: `evidence/channel-summary-20261004.json`; raw seller-bearing HTML stays
outside the repository. Sanitized facts only: `fixtures/observed-detail-facts-20261004.json`.

## 4. Real exclusion, currency and valuation examples

| Real listing | Displayed price | Eligibility result | Newness/valuation limit |
|---|---|---|---|
| Audi 935428104 | $1499 | Excluded: declared customs No + uncleared title/text | No deal sent; older creation than refresh |
| BMW 936238090 | $2350 | Allowed after whole-car/category/full-description review | Display says today; created 28 Sep, refreshed 4 Oct; not a new-publication proof |
| Mazda 936781638 | 25500 UAH | Allowed after complete 413-character description review | Source created 4 Oct 12:13:34 Kyiv, refresh 12:14:35; publication semantics/original seller currency still unverified |

Links are saved with facts. They are asking/display prices, not sale prices.
Observed ad state for BMW has regularPrice 105508 UAH and displayValue $2350;
Audi 67301 UAH/$1499; Mazda 25500 UAH/25500 UAH. No source field independently
establishes the seller's original amount/currency. Parser now records a complete,
identity-matching, active ordinary-sale `observed_asking_display` proof and keeps
`original_seller_currency_verified=false`, `full_price_verified=false`.
This uncertainty is retained rather than relabelled as the original seller price.

Current NBU request at 2026-10-04 09:39:55 UTC returned 403; exact dated URL and
body hash are in `evidence/nbu-20261004-metadata.json`. Mazda has no invented USD
conversion. Shared FX code performs USD=UAH/(UAH per USD), rejects unknown/stale
quotes, retains the amount/currency/date/source and rounds only display. A
synthetic 294000 UAH / 42 = 7000 USD full-flow case passes. Changing the fictional
rate to 40 updates normalized price to 7350 without another seller/new-ad alert.
These rates are tests, not current NBU quotes. The card labels observed OLX display
amounts separately and never calls them the original seller price when that
provenance is unconfirmed.

Real comparable cohorts = 0; independent real quality labels = 0. All three live
examples remain profitability unconfirmed; generation and trustworthy peers are
not fabricated. Minimum 8 eligible matched asking-price ads, fresh reviewed full
price/FX, exact critical characteristics, year/mileage bounds and IQR filtering
remain research rules. Asking-price IQR is not a sale-price confidence interval.
No AUTO.RIA minus-5% rule was copied; extra margin = 0.

Frozen varied synthetic quality set (17 scenarios, 10 positive, 6 negative,
1 truth unknown; independent human/real labels absent):

| Method | TP | FP | FN among decided | Positive valuation unknown | Recall including unknown |
|---|---:|---:|---:|---:|---:|
| Median | 4 | 2 | 2 | 4 | 40% |
| Lower quartile Q25 | 3 | 1 | 3 | 4 | 30% |
| Weighted median | 5 | 3 | 1 | 4 | 50% |

Q25 remains a provisional conservative baseline, not selected for the greatest
number of deals and not validated on the real market. One recommendation has
unknown truth per method. Consistently distorted asking prices fool all methods.
Evidence/tool: `evidence/market-quality-synthetic-20261004.json`, `olx_quality_20261004.py`.

## 5. Files, commands, isolated tests and load

New backend adapter and its tests; new cards/tests, concurrency/tests, observed
asking-display tests, market-quality tests/tool, offline verification harness;
existing pipeline/market/detail/price-review/integration refined. The complete
prior experiment directory is retained, including dated historical evidence.

Run from the branch root, with no production .env or app startup:

```sh
python -m experiments.olx_offline.verify_offline
python -m pytest backend/tests/test_olx_isolated_integration.py -q --tb=short
python -m experiments.olx_offline.olx_quality_20261004
python -m experiments.olx_offline.scenario
```

Actual verification used `env -i`, no inherited secrets, temporary SQLite,
fixture sources/local receipts and external I/O fenced. **281 OLX cases + 31
backend integration cases passed**. **178 adjacent current-main cases passed**
(paid-source production factory with fake transports, owner purchase/restore,
ordinary delivery/read failure, retired copies, poll schedule, active windows).
The only warning was the pre-existing Starlette test-client deprecation.
Independent review reproduced the invalid-first-search bug and verified its fix.
Do not add overlapping agent test totals to these results.

Load is synthetic only: 240 cards, 12 shared fixture pages, 12 peers, 120 USD +
120 UAH using fictional FX. 100/200 users caused the same 12 fixture fetches,
18000/36000 local fake acceptances, no restart requeue. See the saved load evidence
for wall time/memory/SQLite size. No Telegram throttling, actual network, concurrent
market collection or Render load was measured. The current paid-policy integration
also passed 200 paid recipients with one shared fixture fetch and no owner copies.

Server/storage are separate from API costs. No services bought or provisioned.
Future P pages + D cache-miss details every T minutes imply (P+D)*1440/T GET/day;
this is a planning formula, not permission or a measured OLX volume. A bounded
peer bootstrap may collect older permitted peers without new-ad alerts; real
representative bootstrap has not been executed. Research request fee was zero;
ongoing access/compute/storage cost is unestablished. Local SQLite load excludes
photo caching/backups/WAL and does not establish production capacity.

## 6. Real comparison with the competitor

None can be measured: there is no BullAutoRadar car message with OLX URL/ID and
Telegram timestamp, nor independent source-availability timestamp. Public profile
marketing cannot establish speed, filters or price quality. Real cross-platform
duplicates likewise have no verified sample; title/year/price agreement stays a
hint, never automatic suppression. No claim of matching/beating competitor speed.

## 7. Launch readiness, exact boundary and rollback

Code/test integration is reviewable; customer launch **not ready**. Open gates:
permitted sustained source contract, reliable publication semantics and original
price/currency, reachable fresh FX, independent representative real peer/holdout
quality, measured completeness/latency and actual Telegram transport validation.
No current OLX test result claims phone push, delivery, market coverage or SLA.

The branch is separate. Main/live stays 57e357d; Render main auto-deploy and previews
off were rechecked. Night is 23:00–08:00 Europe/Kyiv at 3600 seconds; no RIA
schedule/quota/payment/filter/promotion modifications. No production DB mutation,
client opt-in, Render deploy, daemon or Telegram test sends from this OLX work.

After data gates are solved, the next approval must specify the exact reviewed
commit, permitted channel/request-byte-time budget, test account/search IDs,
duration, zero-extra-paid-RIA policy and separately bounded live Telegram canary.
No such launch approval is presumed. Default-off switch stops only OLX intake,
queueing and dispatch; current rechecks hold outstanding unattempted work. Keep
sent/uncertain history, source IDs, fingerprints and checkpoint on disable/rollback.
Remove the separate unregistered adapter/experiment or revert its integration
commit; no RIA migration or production payment data rollback is needed.

Journal: read handoff + old report -> verify live/main/latest OLX -> reproduce
duplicate and invalid cohort -> bounded public evidence -> independent prototype
fixes and actual policy adapter -> isolated checks + synthetic load -> save branch
and evidence. No future background work is running or promised.
