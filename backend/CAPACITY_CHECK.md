# Capacity checkpoint — 2026-09-29

Runtime inspected: `19b7f156f90594362685ea04e5bb6291c9fcfcda`.
This change adds an offline regression drill and findings; no runtime code or
service configuration change is required by the evidence gathered here.

## Live observations

Render deployment `dep-datnahvlot8c7388ab1g` is live. The health endpoint reports
PostgreSQL connected and delivery available. The 08:30–09:20 UTC measurements
show CPU 0.127–0.146 cores of 0.5 (~25–29%), memory 185.7–219.8 MB of 536.9 MB
(~35–41%), and 5–7 database active connections. These are coarse samples at the
current load, not peak measurements under 200 live subscribers.

At approximately 09:22 UTC there were 24 enabled subscriptions, 23 discovery
groups, no valuation/delivery backlog and available quota. Over the last 24h,
1,994 accepted messages had discovery-to-Telegram p50 13.061s / p95 97.759s and
queue-to-Telegram p50 1.638s / p95 4.836s. These exclude source indexing and the
wait before discovery, and do not establish delivery of a phone push.
No error-level logs were returned for the inspected window starting 08:30 UTC.

## Reproducible offline drill

`python -m pytest backend/tests/test_capacity.py -q -s`

Use existing test dependencies. All provider and Telegram calls are fake; the
test uses a temporary SQLite DB, never the production DB or provider package.

- 200 active users, four new cars, 800 unique queued user/car pairs.
- Exactly four detail requests and four AI quote requests.
- Production `delivery_batch` / `deliver_one`, including normal chat spacing.
- `/stop` after enqueue; Telegram 403, 429 with retry-after, and timeout with
  unknown acceptance; reconnect DB sessions and recreate the worker mid-queue.
- Expected final states: 791 sent, seven cancelled, one failed, one uncertain.
- Every accepted pair is unique; stopped user never reaches the sender;
  unknown acceptance is not replayed; 429 retries respect the wait.

SQLite lacks PostgreSQL SKIP LOCKED, so claim contention may add batches. The
local test duration and simulated clock are not Render/Telegram throughput.
This extends the earlier 800-row enqueue test through actual queue consumption
with a fake sender; it is not a real PostgreSQL process-crash or live-send drill.

## Search capacity at the current schedule

Targets: night 3600s (9h), day 110s (10h), evening 60s (5h), plus supplemental
checks every 300s during 15 daytime/evening hours. One page per poll assumed.
The table is requested traffic BEFORE the scheduler slows down or quota gates
defer requests. Details, valuations, retries and extra pages are additional.

| Distinct groups | Primary/day | Supplemental/day | Evening searches/hour |
| --- | ---: | ---: | ---: |
| 23 | 14,635 | 4,140 | 1,656 |
| 50 | 31,814 | 9,000 | 3,600 |
| 100 | 63,628 | 18,000 | 7,200 |
| 200 | 127,255 | 36,000 | 14,400 |

Current internal caps remain 4,500/hour and 90,000/day; user-reported provider
cap is 5,000/hour. The million-call package is a volume balance, not a higher
hourly allowance. The scheduler already slows 100 groups to an evening primary
interval of 107s; 200 groups produce planned night/day/evening intervals of
6787/214/214s. Supplemental work and actual HTTP requests still pass quota gates.

## Readiness conclusion and boundaries

Queue correctness with 200 synthetic users is verified. Current production
resource usage has headroom, but real 200-user throughput is not established.
The limiting dimension is distinct discovery groups and unique arriving cars,
followed by Telegram send latency, not simply registered-user count.

The dispatcher starts at most four sends per batch and waits at least 0.25s
between batches. Even ideal 800-message throughput therefore takes roughly
50 seconds; real HTTP/DB/photo work and retries add time. This is a theoretical
lower bound, never an ETA or promise of simultaneous delivery.

Before promising the same schedule for substantially more distinct groups:
measure actual pages and unique cars/hour, plan budget for their detail/AI
requests, validate broader search consolidation for coverage, and measure real
queue latency as active load grows. Escalate only on observed CPU/memory,
database or queue saturation. No service upgrade or package purchase is made
by this checkpoint.
