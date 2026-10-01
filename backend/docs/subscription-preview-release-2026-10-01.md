# Subscription preview deployment verification — 2026-10-01

## Released code and scope

- Production main/code commit: `fd9252f64fc5dbc07e300bc555f89aa4d64f09f7`.
- Tested and deployed tree: `5c49812727f83d701e65efc111b8d3391412c6de`.
- Parent main: `005897b0a93c7fe0166424341c0564a5fbaf94cb`.
- Integration branch: `wip/subscription-bot-preview-20261001`.
- Final offline backend regression: **995 passed**, one dependency deprecation
  warning, **148.96 seconds**. All code edits preceded this final run.
- All eight changed/added release blobs match the tested local files by Git SHA.
  The complete candidate tree was checked recursively and independently computed;
  other repository files are unchanged. No manual/discovery WIP was merged.

The owner's instruction permitted installing an inactive trial in the actual
bot. Public users keep free access. The approved **250 UAH / 30 days** quote is
disabled for collection; `/subtest` is an owner-only synthetic trial. No real
transfer, receiving profile, invoice or receipt upload was activated. The earlier
private 1-Star pilot remains unchanged. No secrets, env changes, new resources,
accounting reset or search/claim modification were introduced.

## Deployment evidence

Render still reported auto-deploy `yes` / trigger `commit`. More than five minutes
after creating/promoting the tested commit, repeated deployment-history checks
showed no matching deployment and no build in progress. The precise reason for
the missing automatic trigger was not established; configuration alone was not
treated as proof of deployment.

One ordinary manual deploy was then requested on the existing service, without
clearing cache or changing configuration:

- Service: `srv-dal2h35g1s2s73e0sj80`.
- Deployment: `dep-dav9vv3ncjis73cenpo0`.
- Created: **2026-10-01 18:01:32.874534 UTC**.
- Live: **2026-10-01 18:02:30.882561 UTC** (**21:02 Kyiv**).
- Commit: exactly `fd9252f64fc5dbc07e300bc555f89aa4d64f09f7`.
- Final history check: **one** deployment for that commit, status `live`.
- Main and integration branch were verified at that code commit after deployment.

This verification record is a subsequent **documentation-only WIP checkpoint**;
it is not an additional production release or deployment.

## Read-only checks after live

`/health` returned successful status, PostgreSQL connected and delivery available.
The bounded application-error log query from deploy creation through
`18:03:28.340805209 UTC` returned zero error entries. This does not prove that
every internal operation or future transaction will succeed.

A 45-second-timeout source-status read succeeded; only `monitor`, `launch.activity`,
`budget` and `quota` were projected. The earlier 15/30-second reads timed out.
Observed post-release state:

- Monitor running, 74 filter groups and 74 successful groups.
- Oldest cursor lag 178 seconds; `needs_attention=false`.
- Three new-publication jobs pending valuation; Telegram delivery pending/sending
  queues empty.
- `confirmed_deals_only=true`; supplemental active-window and include-initial
  flags remain false.
- Programmatic caps remain 4500/hour, 90000/day, 1102160 lifetime.
- Lifetime local use 263293, local remaining allowance 838867; quota available.
- Requested evening interval 60 seconds; reported effective interval 79 seconds
  due to the pre-existing budget adjustment. Day/night targets remain 110/3600.

These are snapshots, not guarantees of full listing coverage or mobile push.
The local request allowance is not the AUTO.RIA account balance.

## Owner check and remaining limits

Send `/subtest` in the private chat with `@auto_deal_finder1_bot`, then use
«Створити тестову заявку» → «Тестова квитанція» →
«Підтвердити тестові 30 днів». No money or real receipt is required. Repeat the
status command to inspect saved trial state. A duplicate approval must not add
another term. This does not activate or restore stopped car searches.

The owner has not yet reported completing this deployed flow. PostgreSQL
restart persistence and actual Telegram button delivery are therefore not claimed
as observed end-to-end results. Offline concurrency and new-engine persistence
passed. Actual collection, public activation and compliant monetization remain
separate work, subject to the official Telegram Stars requirements recorded in
the integration handoff. Do not enable them automatically.
