# AUTODeal beta readiness — 2026-09-24

The admin-only `/stats` reply can show the rolling 24-hour request count,
remaining local lifetime allowance and a rough runway at the current pace.
It reads existing accounting and makes no provider request. It is available
only when the intended owner's `ADMIN_TELEGRAM_ID` has been configured on the
service; the provider's own balance and billing renewal must be checked in
the developer account separately. Current monitoring cadence remains unchanged.

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

## Quota and speed decision (checked again at 07:28 UTC)

With the configured 900/hour, 12,000/day and 90,000 lifetime local caps, the
existing scheduler reserves half the hourly/daily rate for listing details and
quotes. Its target polling interval by *distinct discovery filter group* is:

| Distinct groups | Target interval per group | Consequence |
| ---: | ---: | --- |
| 7 (currently active) | 101 seconds | All seven watching; most recent observed cursor lag 69 seconds. |
| 20 | 288 seconds | More than four minutes between checks per group. |
| 100 | 1,440 seconds | 24 minutes; this is **100 different filters**, not 100 customers. |
| 200 | 2,880 seconds | 48 minutes for 200 different filters. |

The intervals are scheduling targets, not guarantees of a Telegram push time.
At the latest snapshot local accounting reported 7,677 calls over 24 hours and
45,431 calls remaining. At an unchanged rate, that is roughly 5.92 days. To
cover 30 days with that balance, average spend would have to stay below 1,515
calls/day, about 80% lower than observed. Even with **zero** listing-detail or
market-quote calls, seven groups alone could be polled no faster than about
once every seven minutes within that 30-day average. Real vehicle calls demand
more headroom. Monitoring 100/200 users with identical filters still shares
search calls; 100/200 unique filters does not.

The discovery window already excludes old listings, identical filters already
share polling and a matching candidate uses at most one paid market-range
quote per current evaluation. No confirmed redundant request was found whose
removal would preserve the fresh-publication coverage. Do not silently lengthen
the polling interval, merge owners' saved filters, enlarge the caps or purchase
an additional package to make this table look better. Choose and fund a trial
scope compatible with the target alert speed, then measure it with a few opted-in
customers before promising 30 days.
