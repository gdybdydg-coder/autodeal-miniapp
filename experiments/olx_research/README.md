Final night result: [morning readiness summary](MORNING-READINESS-20261005.md). A fail-closed release gate records that stages5–6 remain blocked while stages7–8 are complete only as an isolated prototype. 411 tests +150 subtests; market coverage remains0/71 and production/Telegram remain untouched.

# OLX: isolated review, 2026-10-04

Latest: [overnight checkpoint](NIGHT-CHECKPOINT-20261005.md), [overnight rules](NIGHT-20261005.md), and `night-state.json`. Current saved cohort now requires corroborated drive/power for valuation; historical numbers below remain dated checkpoints.

**Not connected to the bot. No deployment or Telegram authorization.**
Base and unchanged production: `afaf9cee348db64f0558e5d113b2895c48366421`.
The continuation changes `experiments/olx_research` and fixes the isolated
`experiments/olx_offline/detail_snapshot.py` parser. Existing backend, frontend,
workflows, configuration and database schema are untouched.

See [STAGES-1-8-20261004.md](STAGES-1-8-20261004.md) for the latest continuation.
Numbers below describe the earlier 22:20 research checkpoint, not the current run.

## Earlier checkpoint: reuse and additions

Reuse the existing `olx_offline` detail/search parsers, eligibility, decimal amount
parser, date policy primitives and percentile function. Existing NBU-only FX,
median/Q25/weighted market, queue/lease tests remain unchanged.
New: multi-provider FX policy; pre-cap candidate filtering with reasons; explicit
source-declared A5/body/condition evidence; regular sale + optional exchange
corroboration; median/trimmed mean/weighted median comparison; query preparation
for a later RIA budget; persistent local replay and preview records.

The new asking-price policy is a **research proposal**. Full visible detail +
matching public state + ordinary sale + no price/category conflicts can support
an experimental comparison of advertised asking amounts. It does not change the
old `price_kind` or `full_price_verified`, prove the currency originally entered
by the seller, validate seller claims, or authorize production recommendations.
A5 is recorded only when explicitly named in a matching Skoda Octavia title;
year alone never supplies a generation. Ambiguous A4/Tour/A5 titles are held.
Liftback and body-repair condition stay distinct from other body/condition pools.

## Proposed FX choice for approval

1. Dated direct NBU `exchange_site` official USD reference.
2. PrivatBank dated archive `saleRateNB == purchaseRateNB`, base UAH/code 980,
   USD row, exact requested Kyiv date. Do not substitute its cash buy/sell fields.
3. Monobank public USD/UAH row (840/980), timestamp, positive ordered buy/sell.
   Use `(buy + sell) / 2` as an explicitly **bank-derived reference midpoint**.
   Its precise transaction channel is not established by the API description;
   do not call it the NBU, cash, or guaranteed transaction rate. Store buy/sell and
   show the conversion sensitivity range too.
4. A previously saved valid quote; do not refresh its acquisition timestamp.

One selected quote normalizes **all UAH entries in a comparison**. No per-car
provider selection and no mixing of bank/official bases in the cohort. USD is
never converted again. `input_amount/input_currency` identify observed input;
`seller_original_currency` remains null where not known. Decimal precision 50;
round only display. EUR/unknown currencies remain pending, independent USD work
continues. Source has no automatic HTTP implementation: a bounded transport must
be explicitly injected; replay uses files only.

Initial proposed sanity bounds: 10–200 UAH/USD; reject >15% jumps against a still
valid saved quote; reject >10% bank spread. These detect anomalies, not fraud.
Same-day quotes on weekdays, acquisition age <=6h. On Saturday/Sunday, an
explicitly dated Friday-or-later quote can be retained with source age <=72h
and cache acquisition age <=24h. No implicit carry into Monday or an unverified
holiday. No future quote/date/acquisition time. Persisted 5-minute cooldown
covers successes and failures; each provider at most one attempt per selection.
This is a single-coordinator research cache, not a distributed production lease.

Actual checks on 04.10.2026 (no paid API):
- NBU: HTTP403, 55 bytes. No repeated requests or bypass.
- PrivatBank archive: HTTP200, 2836 bytes; date04.10.2026;
  `saleRateNB=purchaseRateNB=44.8333`. Its separate cash fields were44.6/45.2.
- Monobank: HTTP200, 8491 bytes; source timestamp00:01:13 Kyiv;
  buy44.8 / sell45.1998; midpoint44.9999. Available as third provider;
  the chosen real cohort uses PrivatBank's NBU reference.

## Market methods and evidence

Default research minimum remains eight compatible adverts, excluding the target
and known crossposts. Match brand/model/generation/body/fuel/gearbox/engine,
year ±1, mileage ±30000km, condition, freshness <=30days. Same-time identity or
known-vehicle conflicts are held; new invalid crossposts cannot restore older
eligible records. Distinct adverts do not prove distinct physical vehicles:
`independent_vehicles_verified=null` when identity evidence is insufficient.

Compare on the same screened cohort: median; IQR-screened trimmed mean
(trim max(1,n//10) at each end); weighted median by year/mileage/age/region.
IQR is an empirical asking-price range, not a statistical confidence interval.
Median is a predeclared research baseline, not an accuracy winner. Discount is
(reference−asking)/reference×100, no AUTO.RIA−5%. There is no automatic customer
recommendation. Unknown attributes hold valuation while missing optional
fuel/gear/photo alone do not constitute a filter contradiction.

Fresh bounded sample: 15 OLX GET (one complete search HTML +14 detail attempts),
three HTTP410, eleven complete current details. Raw HTML stays outside git.
At most18GET/64MiB/20minutes was allowed for the foreground probe; four-second
pauses, no redirects/retries/CAPTCHA. One failed detail did not discard later
valid cars. 401/403/429 would halt that source. Full HTML is not proof of complete
catalogue coverage. Search:29card rows/27unique,2promoted/25organic; relevance.
Source-created/refresh timestamps are observed, publication semantics unverified.

After the sale/exchange correction: **0/11 real estimates** at the required
minimum. Three A5 1.9 diesel/manual wagons have only two compatible peers each:
933794336 ($4400,380k km),934885570 ($6500,370k),935807909 ($4700,365k).
Do not treat their wide three-ad range as a verified market appraisal. A5 needing
body repair (935797647) is not pooled with sound-body cars. Uncleared935624887
is excluded. Other body/mileage groups are not enlarged by incompatible cars.
No independent real labeled holdout: false-positive/false-negative rates unknown.
`assess_dataset` can report coverage and confusion for separately supplied,
independent labels; labels are never inferred from the asking price itself.

Three actual UAH examples, observed display currency (seller-origin unknown):
| OLX ID | Asking UAH | NBU reference via PrivatBank | USD, rounded only for display | Market |
|---|---:|---:|---:|---|
|932803277 Mazda626|24999|44.8333|≈558|generation / adequate cohort missing|
|936594906 ToyotaCorolla|310000|44.8333|≈6915|generation / adequate cohort missing|
|936618692 MercedesW123|40000|44.8333|≈892|generation / adequate cohort missing|

RIA parameter method is documented, paid and requires method permission.
`ria_plan.prepare` creates a credential-free, non-executing proposal only after
reviewed dictionary mappings; it never uses an OLX ID as omniId. Mileage is in
thousands of km, engine volume in litres. `similarCars/statisticData` need their
own comparability and result validation. Actual OLX RIA calls:0. Account price,
quota units and permission unknown; budget is not approved. At most one
assessment per distinct verified query plus uncached dictionary lookups would
be proposed; no invented UAH cost. The current RIA cache's listing-specific
omniId observation is not an interchangeable OLX appraisal.

## Offline verification and replay

From repository root, with the existing Python test dependencies:

```sh
PYTHONPATH=.:experiments/olx_offline python3 -m experiments.olx_research.verify
PYTHONPATH=. python3 -m experiments.olx_research.replay \
  --input experiments/olx_research/examples/synthetic-input.json \
  --state /tmp/autodeal-olx-review.db \
  --output /tmp/autodeal-olx-review.json
```

The first command fences DNS and sockets before collection. The second is an
explicitly labelled fixed synthetic fixture, not live ads. Reusing the same
state on a second run should create no repeated previews. No backend app startup,
.env, bot token, database URL, paid transport or real Telegram is used.

Real observations were separately replayed with two **fake paid** users and one
**fake unpaid** user:20 local previews, two withheld records (the same uncleared
car for both paid users), zero real Telegram calls. Restart:zero new previews.
The preview includes unknown valuation/newness and `delivery_authorized=false`.
A local preview does not prove a new publication, profitability or live delivery.

## Staged approval and minimum future changes

Owner review → new explicit approval → bounded owner-only test → recorded
Telegram result → separate client decision. Current OLX flags remain off.
The previous exhausted package/history must not be reset or reused.

Before a production test, agree the price-evidence/FX policies, acquire a valid
analogue cohort and independent labels, verify source permission/stability and
newness semantics. Then review a minimal adapter diff: pre-cap selection and
hold reasons, shared FX provenance, separate source cache/ledger, owner-only
paid/active/stop guard and resource caps. No whole experimental branch merge.
Reuse the existing isolated stop switch; propose no configuration changes now.
No new package ID, activation/deadline or message count is approved by this work.
Stop OLX independently on regression; do not roll back working AUTO.RIA.

Official sources:
- https://developer.olx.ua/ua/articles/faq
- https://developer.olx.ua/api/doc
- https://api.privatbank.ua/ (current public documentation JS bundle inspected)
- https://api.monobank.ua/docs/index.html
- https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai
- https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_data.pdf

Verification result: **346 tests and 150 subtests passed**, DNS/socket fence
active before collection. See `examples/real-evidence-summary.json` for the
sanitized current observation receipts and cohort limits; raw HTML, seller
contacts and production records are not published. Synthetic fixtures are
separate and are not independent real market validation.

Confirmed isolated corrections: candidate filtering/dedup before cap; ordinary
sale plus optional exchange accepted only with matching full-page evidence;
negative FX cooldown after cache expiry; malformed detail URL recorded and
skipped without losing later valid details (two failing regressions before fix).
These corrections have not been transplanted to production.
