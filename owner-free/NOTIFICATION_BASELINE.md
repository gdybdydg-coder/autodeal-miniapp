# Owner-worker publication baseline

This accompanies the API's `fix/notification-publication-baseline` change.
Deploy both fixes before applying the one-time owner reset. Do not resume this
worker's old build after resetting Postgres: it cannot enforce the new cutoff.

The API dry-run/reset tool is `python -m backend.reset_notification_history
--owner --baseline <fixed ISO-8601 UTC instant> --reset-id <fixed ID>`.
It is read-only without `--apply`. Review the counts and obtain owner approval
before applying. All Render and local workers/bridges must be stopped first;
use the existing backup process for Postgres and `/var/data/sorted-v1`.

The worker's existing read-only Postgres connection reads the owner's
`source_probes` row `notification-baseline-<Telegram ID>`. No new environment
variables, credentials, paid source calls, service, or database migration are
needed. The owner identity and other user settings remain unchanged.

On its first corrected cycle the worker synchronizes the cutoff to the existing
SQLite `checkpoints` table using reserved key `notification-baseline`. In one
transaction it cancels pending `jobs` (reason `notification_baseline_reset`),
clears old source checkpoints and writes the cutoff marker. Subsequent cycles
with that cutoff do nothing. A backward cutoff fails closed. The separate
recipient databases are only reset if that recipient has an explicit cutoff.

`sent` (including uncertain/attempting claims and message IDs), `identity`,
`peer_previews`, `detail_delays`, source access holds, transport quota counters,
peer caches, handoff/activation files and filter logic remain intact. New jobs
still use the existing queue and the same at-most-once send ledger.

Cards require source publication evidence strictly after the cutoff at intake
and before transport. Explicit `data-add-date` is preferred. Recognized source
age labels use conservative lower bounds including rounding/clock allowance;
an observation time, updated label, promoted placement or unknown date never
proves new publication. Unknown dates are held out instead of risking old
alerts. Rounded ages can delay eligibility. The first head snapshot may enqueue
proven post-cutoff cars, including ones published during deployment preparation;
it excludes everything else without moving the explicit baseline forward.

Verify with the worker offline tests:

```sh
cd owner-free
PYTHONPATH=repo python -m pytest tests/test_notification_baseline.py -q
node --check repo/experiments/free_search/sorted_monitor.mjs
```

The API PR includes the complete Postgres/table inventory, dry-run/apply runbook,
rollback/idempotency checks and production-access limitations. Live disk/queue
counts and an actual post-cutoff car delivery must be checked after approved
deployment through the authorized production connection. No production reset
or Telegram send was performed while preparing this change.
