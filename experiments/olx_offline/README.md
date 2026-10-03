# OLX Auto — isolated offline prototype, 2026-10-02 Kyiv

Status: OFFLINE CODE + TESTS; NO LIVE OLX DATA OR PRODUCTION INTEGRATION.
Base production commit: 43084b2b7e8551e4cabff3ee3eb2fb90142dc402.
No earlier OLX prototype was found in checked local paths or remote branch names.
Render read-only check: auto-deploy branch main; preview generation off. This WIP
branch does not trigger the configured production deployment.

## 1. Access decision — blocked for the needed public search

Official sources inspected:
- https://developer.olx.ua/ua/articles/faq — question 6: API cannot access other
  people's advertisements; it manages the authorized account's own ads.
- https://developer.olx.ua/ua/articles/getting-access-to-api — registered app and
  OLX review required before client credentials. No credentials were requested/read.
- https://developer.olx.ua/doc/regulations.uk.pdf?v49= — app/API approval and terms;
  not every public site function is available through the API.
- https://developer.olx.ua/api/doc — available documentation shell; no supported
  general-search endpoint or public-car pagination contract established.

FAQ also states no sandbox and partner API limits of 4500 requests per 5 minutes,
with temporary blocking when exceeded. This is NOT a scraping permission or a
budget for this prototype. Actual OLX listing/API requests: **0**. Documentation
reads only. No CAPTCHA, proxy, account creation, hidden endpoints or paid service.
Required next step: OLX-approved public-car feed/access agreement and its documented
fields, pagination, freshness and rate limits. Do not substitute guessed routes.

## 2. Implemented fixture contract

`prototype.py` uses explicit synthetic dictionaries, not an asserted OLX API schema.
Source + ID, URL/title/price/currency, brand/model/year/region/mileage/fuel/gear/photos,
published/updated/bumped/first-seen dates remain separate. Missing fields stay null.
Fixture category whole_passenger_car allows an entire damaged/non-running/parts car;
parts, rental, services and unknown category never become passenger cars by title.
Missing optional fields do not reject; known mismatches do. Unknown currency,
placeholder/nonfinite prices and down payments have explicit unresolved states.
No guessed exchange rates. No production valuation formula import/change.

Shared collection is cached once per source ID in local SQLite. Multi-page intake
is atomic; page/cursor/conflict/timeout errors retain prior progress and durable
error history. Max 20 pages / 1000 rows per run. Initial snapshot is a baseline;
subsequent fresh events require explicit verified publication time within one hour.
Updates/raises do not create new-publication events. Overlap is bounded to one hour;
late indexing beyond it is not proven covered. Pagination/checkpoint tokens are
fixture abstractions, to be replaced ONLY by a verified source contract.

## 3. Valuation and duplicates

All records remain profitability_unconfirmed. No AUTO.RIA estimate is assumed to
exist for an OLX ID. `fanout` evaluates user-filter candidates only and has zero
Telegram sends. A future research estimate would require a licensed, representative
sample matched by model/year/engine/gear/mileage/region, stale/outlier/downpayment
exclusion, minimum comparable count, dated currency conversion and measured error
on a held-out sample. Asking prices are not completed-sale prices. No fitted model
or verified estimate exists here; no number is presented as confirmed market value.
AUTO.RIA lower-bound-minus-5% and user minDiscount remain unchanged in the bot.

Source+ID is exact identity. Cross-source brand/model/year/region/price/currency
agreement produces only a possible-duplicate hint. Both ads remain; no fuzzy deletion.
VIN/photo/seller matching has not been collected or implemented; privacy/access
permission and false-positive testing would be required first.

## 4. Verification and synthetic load

```
python -m unittest discover -s experiments/olx_offline -v
python experiments/olx_offline/benchmark.py
```

11 tests passed with TCP/DNS fenced. Covers parsing/incomplete values/categories,
known filter conflicts/currency, publication versus bump, old ID/new publication,
baseline, pagination cycle/conflict/budgets, rollback/restart, duplicate retention,
and 200-user fanout. Benchmark output gives runtime/memory/SQLite size; not a live
throughput prediction. Synthetic assumption: 500 cached cars, 20 shared fixture
pages, 100 or 200 enabled users; zero OLX/Telegram calls. Real latency/quota/scale
cannot be inferred without a permitted source and measured rate limits.

## 5. Future rollout / off / rollback — not executed

1. Resolve data access and terms; validate taxonomy and source contract on a small
   approved sample, with hard request/byte/time budgets and explicit stop on denial.
2. Connect a separate staging store/queue with mock Telegram; keep OLX feature off
   in production. Test migrations, auth, expiry, /stop, durable claims and backoff.
3. Prove publication freshness/valuation validity/dedup precision. An unknown estimate
   must never enter a confirmed-deal channel. Review missing and rejected pairs.
4. Seek owner approval of exact code, access contract, costs, rollout and rollback.
5. Only after approval: canary to owner-selected test audience with source-labelled
   cards, measured quality/error rates and a separate kill switch.
6. Disable intake and dispatch before rollback; retain durable sent/uncertain claims,
   cursors, raw provenance and audit. Never replay unknown send outcomes or auto-enable
   stopped searches. No deletion of production AutoRIA rows.

Journal: access research completed -> needed official API search unavailable ->
offline normalization/store/filter/recovery prototype completed -> 11 tests passed.
Acceptance now: independently reviewable offline prototype, not live OLX support.

## Продовження 2026-10-02

Новий ізольований pipeline та результати: [RESEARCH-20261002.md](RESEARCH-20261002.md).
Старі файли вище — історичний прототип. Живого OLX-адаптера немає.
Запуск усіх тестів: `python -m unittest discover -s experiments/olx_offline -v`.
Повний offline benchmark: `python -m experiments.olx_offline.scenario`.

Наступне продовження: [актуальність оцінки та захист черги](HARDENING-20261002.md).
Поточний набір: 37 тестів. `deliver_fake` тепер вимагає явний `now`;
порівняння потребують підтвердженого `checked_at`.

Наступний етап: [перевірка ціни та збереження причин](PRICE-REVIEW-20261002.md).
Поточний набір: 46 тестів; старі benchmark залишено як історичні вимірювання.

Перевірка контрольної вибірки: [evaluation guide](EVALUATION-20261002.md).
Поточний набір: 58 тестів. Інструмент вимірює вибірку; повноту живого OLX не доведено.

Реальний доступ і HTML parser: [LIVE-SOURCE-20261002.md](LIVE-SOURCE-20261002.md).
Поточний набір: 66 тестів. Один прямий HTTP200 дав52 картки в обмежених2MiB;
це неповна відповідь, не підтвердження повноти/стабільності або дозвіл на запуск.

Прямі деталі: [DETAIL-AND-TERMS-20261002.md](DETAIL-AND-TERMS-20261002.md).
Поточний набір74 тести; detail parser перевірено на1 реальній неповній відповіді.

Current continuation (3 October): see [DATES-GEOGRAPHY-20261003.md](DATES-GEOGRAPHY-20261003.md) for real source-date/region evidence, negative newness gates and remaining channel/valuation blockers. Historical test totals and live samples elsewhere are dated results, not current readiness guarantees.
