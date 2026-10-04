# One authorized owner-only OLX package

Base production commit: `0028f54c0501134b4c132f3b890797ba3d531985`.
The owner's instruction of 2026-10-04 21:30 Europe/Kyiv authorizes at most
three real examples only to their private bot chat, including clearly labelled
examples with unknown valuation. It does not authorize a customer OLX source.

## Reused work and isolation

Reuse `olx_owner_canary`'s bounded official public-page fetch, regional page
selection and filter comparison; existing detail/search parsers, current paid
source policy, `SourceProbe` persistence and the existing Telegram transport.
Reuse the independently reviewed dismantling grammar patch from
`2c585150b165d137b33a1eac96b17aa51e7b067b`; no other experimental branch changes.
No payment/schema migrations, synthetic production users, RIA provider calls,
source-budget changes, webhook changes or second Telegram poller.

New state: `olx-owner-batch-20261004-2130-v1`. Previous observation and two-card
package remain untouched. Owner+source-ID reservations are independent of the
new batch ID and also consult the old accepted/uncertain/sending records.

Default off. Enable only `OLX_OWNER_BATCH_ENABLED=true` and an explicit future
Unix `OLX_OWNER_BATCH_UNTIL`. The stored deadline is the earlier of that value
and first initialization plus 600 seconds; restarting never extends it.
Hard caps: three reserved card attempts (including unknown results), ten OLX
GETs, 40 MiB reserved downloads, one category page and at most nine details.
Actual received bytes are recorded. A source GET reserves 4 MiB first; complete
details must also fit the existing 2 MiB parser limit. Requests use the existing
5-second socket timeout and 20-second absolute body budget, with four-second
pauses between detail calls. No retry, alternate IP, proxy purchase or CAPTCHA
bypass. 401/403/429 terminate the package; 404/410 skip that individual candidate.
The package always ends after its bounded candidate list and does not become a
continuous feed. One isolated thread task runs; AUTO.RIA has its own task/queues.

The owner must match `SUBSCRIPTION_EXPECTED_ADMIN_ID` as well as configured
admin ID. The current confirmed purchase, entitlement, ready state and pinned
enabled searches are checked before source work and before each Telegram call.
`getChat` must prove the same positive ID and private chat before each card.
Current filters are reread after that check. A matching private chat and positive
message ID are required for an accepted receipt. Ambiguous results never retry;
there is no photo fallback that could create a duplicate. This does not prove
the person read the message or received a phone push.

`/olx_stop` persists a pause even before first initialization. It does not change
AUTO.RIA, paid access or searches. `/stop` additionally blocks the shared ready
predicate. `/olx_status` shows attempts and accepted counts. An environment merge
`OLX_OWNER_BATCH_ENABLED=false` disables this package after redeploy. Keep the old
`OLX_OWNER_CANARY_ENABLED=false` during this package so disabling the new package
cannot select an old observer. Preserve all probe/receipt records.

## Price and text

Cards start with `🧪 ТЕСТ OLX — лише для власника`. They show source attributes,
the corroborated visible asking price, place, photo if available, original link,
and explicitly unconfirmed market value and newness. They never claim a deal.
The exemption concerns valuation/newness only; payment, category, price conflicts
and known user-filter contradictions remain enforced. No saved threshold changes.

Public detail `displayValue` may be USD while `regularPrice` and JSON-LD are UAH.
Both amounts are preserved separately. A corroborated dollar display is already
USD and is not converted again. It is labelled as OLX's price, not the proven
currency originally entered by the seller; that field remains unknown.
The existing dated NBU normalizer supports UAH / UAH-per-USD with provenance and
no double conversion. The live package currently has no verified daily FX cache,
so UAH/EUR examples are held rather than assigned an invented rate. Missing fuel,
gearbox or photo does not by itself exclude a car. The complete description,
source ordinary-sale flags and full visible price are inspected; deposits,
parts, dismantling, uncleared cars and contradictions are held/excluded.

## Research boundaries

Official OLX FAQ says Partner API accesses only authorized-account adverts,
not other sellers' catalogue. Public HTML is a technically observed channel,
not a confirmed production data licence or completeness guarantee. Help terms
pages returned only Loading/CSS Error, so their current substantive terms remain
unverified. Default category sorting was relevance with promoted/organic cards;
offset page links and refresh labels do not establish publication order.

Official RIA AI documentation supports parameter-based POST
`/auto/ai-avarage-price/`, with user_id/api_key and method-specific permission.
Body requires langId, period and params; categoryId, brandId, modelId plus an
additional parameter are required. For meaningful OLX comparison use verified
generation, body, fuel, engine, gearbox, year, mileage and condition mappings.
An OLX ID is not an AUTO.RIA ID. This is a paid method; no request is made here.
Three distinct verified parameter sets would require up to three assessment
calls plus any uncached dictionary lookups. Account-specific cost/permissions
must be confirmed before a separate budget approval; no UAH price is invented.
Saved RIA observations may be considered only with matching characteristics,
freshness and permitted reuse, not by copying a random car's result.

The existing own-market module compares robust median, lower quartile and
weighted median, with no automatic 5% RIA discount. Eight eligible matching
adverts are a research minimum; known duplicates/self are excluded. It reports
spread, source dates and low experimental confidence, not realized sale prices.
Current live examples do not form an independent comparable cohort and lack
verified original-price/generation/vehicle evidence. No method is yet validated
on an independent real holdout. Synthetic accuracy is not commercial readiness.

Sources checked 2026-10-04:
- https://developer.olx.ua/ua/articles/faq
- https://developer.olx.ua/api/doc
- https://help.olx.ua/olxuahelp/s/article/правила-сервісу-olxua-V1
- https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai
- https://docs-developers.ria.com/en/used-cars
- https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_data.pdf

## Verification and rollback

Offline regression: `PYTHONPATH=.:experiments/olx_offline python -m pytest -q
backend/tests experiments/olx_offline`. Existing backend socket fence forbids
real network access, including during collection. Tests use temporary SQLite,
real policy/parsers and fake OLX/Telegram only.

Before activation, verify the exact deployed tree and disabled batch diagnostic.
Then enable only the OLX package, record actual source IDs/times/message IDs,
terminal state and subsequent unchanged RIA configuration, queue and paid cohort.
Keep private live observations out of this repository. Source/Telegram logs are
evidence of acquisition/acceptance only, not newness, profitability or readiness
for customer rollout. Prefer stopping only the batch over rolling back the bot.
