# AUTODeal beta readiness — 2026-09-24

## Verified without contacting AUTO.RIA or sending Telegram messages

- 426 backend tests and 72 Mini App tests passed. A fresh ad enters one shared
  discovery/evaluation path, fans out to 200 current subscribers, and all 200
  distinct delivery claims can reach a simulated Telegram acceptance. Queuing
  and delivery neither reread old listings nor consume more source calls per
  recipient. Four-worker concurrency is covered separately. SQLite does not
  support PostgreSQL `SKIP LOCKED`, so these tests do not establish actual
  delivery throughput or concurrency under live PostgreSQL and Telegram.
- Regression cases cover more than 50 new ads over pagination/restart, delayed
  indexing, unknown market range, missing optional details, repair/damage/
  parts flags, changing listing prices, saved discount thresholds, overlapping
  subscriptions, `/stop`, uncertain Telegram outcomes and no duplicate sends.
  They do not prove that the provider exposes every fresh listing.

## Live snapshot (2026-09-24 07:13 UTC)

- Running commit: `091f01e32c26130abbc523e27cbcfa2ebe670db1` on Render;
  frontend commit `2c4354865035a174acd422c3508a07a3eb5e5485` on GitHub Pages.
- Health: database connected, delivery available; seven of seven source
  groups watching; no pending jobs or error-level app logs. Supplemental
  historical/active-window checks are disabled. The 24-hour counter reported
  242 Telegram API-accepted messages; this does not prove phone push or unique
  vehicles/users. Last observed discovery-to-acceptance duration was 1.535 s,
  not a distribution or guarantee.
- Local provider accounting: 7,667 calls in the rolling 24-hour window;
  45,507 calls remain of the configured lifetime cap of 90,000. At an unchanged
  daily rate the remaining balance would last about 5.94 days. Thirty days
  would require an average at most 1,517 calls/day even before adding users or
  filter groups. The provider's own balance may differ from local accounting.

## Launch gate and next controlled measurement

Do not advertise an unconditional free month or promise first-second alerts.
First obtain a replacement for the exposed AUTO.RIA key, decide the trial's
start/end and support contact, and reconcile the account balance with local
accounting. A small group of opted-in testers can use their existing saved
filters. During a bounded observation period track newly published matching
IDs, each missing-ad trace, discovery/acceptance latency distribution, 429s,
uncertain sends, queue depth and hourly/daily/total source spending. Never
reset sent/uncertain claims or test with historical replay. Expand group size
only when the measured burn and purchased balance can cover the promised trial.

The current budget cannot support a 30-day promise at the measured rate.
Slowing seven primary polls to fit the balance would delay discovery by several
minutes and conflict with the owner's fresh-alert priority. A new quota plan
or a reduced scope requires an explicit business decision; no caps were changed.
