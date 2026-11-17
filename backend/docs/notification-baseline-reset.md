# Reset notification history for the configured owner

The outage backlog is intentional cursor catch-up, not just rows in `listings`.
Reset discovery and notification authorization together, without deleting cars or
changing saved searches. Both deployed code branches require the fix before a
production reset. Do not merge or run `--apply` until the owner confirms the plan.

## Inspection and current limits

Inspected the deployed API commit `41fab55` and owner-worker commit `22f5436`,
their ORM schemas, source adapters, queues, recovery paths and Render metadata.
Render reports both services suspended. The database is available, but its
external allowlist rejected the connector's read-only SQL connection. No live
rows or counts were inspected, and no production data/environment was changed.
Actual counts must come from the dry run through an existing authorized database
connection, preferably the same-region Render internal connection. Do not open
the database to all IP addresses to run this operation.

## State inventory and exact effects

| Store/table | Relevant fields/records | Reset effect |
| --- | --- | --- |
| Postgres `source_probes` | `notification-baseline-<owner ID>` | Insert/update `status=active`, `checked_at`, `requests=0` for a new row, `result.baseline_at`, `result.reset_id`, `result.plan`. No other probes are overwritten. |
| `monitor_watches` | Owner's `search_id`, `epoch`, `window`, `initialized`, `status`, `next_poll`, `checked_at` | Use existing `reset_watch`: rotate enabled-search epochs, empty windows, set starting/poll state. Disabled watches are removed. |
| `monitor_memberships` | Owner's search rows: `epoch`, `feed_id`, `started_at` | Recreate enabled memberships using unchanged filters, new epoch and explicit cutoff. Remove disabled memberships. |
| `monitor_seen` | Owner's search/source discoveries | Delete old interests through normal `reset_watch`; new discovery re-establishes interests only for the current epoch. |
| `monitor_matches` | Owner's search/listing matches and fingerprints | Delete old authorizations, keeping `searches` unchanged. |
| `monitor_feeds` | `started_at`, `cursor`, frozen pagination `context`, `next_poll`, `checked_at`, `status` | Reset to cutoff/empty context only for feeds with no other users' memberships. Shared cursors and other users' epochs stay intact. |
| `monitor_active_windows` | Exclusive feeds' `window`, `next_poll`, `checked_at`, `status` | Empty supplemental head state for those feeds. |
| `monitor_jobs` | `source_id`, `state`, `reason`, `next_run`, retained `result` | Cancel previously pending jobs referenced by the owner's old interests only when no current pending interest remains. Shared jobs/evidence stay available to others. |
| `deliveries` | Owner's `state`, `retry_at` | Change `pending`, `sending`, historical `owner_pending` to `cancelled`, `retry_at=0`. Preserve IDs, listing links, message IDs, sent/uncertain/failed history and unique deduplication claims. |
| `listings`, `delivery_timings` | Stored cars, publication/transport evidence | Retain all rows. New evaluations store publication evidence in the existing `car.pipeline` JSON; send-time checks apply even if an old queue is revived. |
| `source_probes` recent-publication queue | `recent-publications-v1`: `result.baseline_at`, `pending`, `seen` | Shared queue remains; owner's current epochs and persistent delivery cutoff prevent old work from authorizing a notification. |
| `source_probes` OLX states | `olx-owner-monitor-20261006-v1`, `olx-monitor-ad-*`, valued/batch/canary controls and `olx-owner-car-*` send ledgers | Keep source budgets/holds/receipts. All car send paths check the owner's cutoff against identity-verified source creation dates, including queued old cards. Monitor rejects old work before valuation. |
| `full_scans`, `scan_items`, `market_cars`, `valuation_peers`, `source_cache`, `source_budgets` | Manual search/market caches and quota accounting | Retain. Manual search must not authorize notifications; discovery/valuation/observation timestamps cannot substitute for publication. |
| Owner disk `/var/data/sorted-v1/state.sqlite3` and configured secondary-recipient stores | `checkpoints`, `jobs`, `peer_previews`, `sent`, `identity`, `detail_delays` | Worker reads each recipient's Postgres cutoff. On first observation, atomically cancel pending `jobs`, clear source `checkpoints`, store the cutoff marker in existing `checkpoints`. Keep all `sent` claims/receipts, identity, previews and delays. Only the reset recipient is affected. |
| Owner disk browser caches/JSON | Search/browser reports, peer caches, source transport budgets/holds, status/handoff files | Retain. Each candidate must prove post-cutoff publication before entering and before leaving the existing queue. A cached observation cannot authorize old cars. |

All `users` (including readiness, IDs and Telegram command offsets), `searches`
(including filters, names, enabled flags, fingerprints and `after_listing`),
subscriptions, all billing/payment/access/reminder/admin records are untouched.
No foreign rows are deleted, no tables are truncated, no listing IDs are reused.
Publication discovery uses the existing filters and matching logic.

## How the boundary works

The per-user cutoff persists across restarts, new searches and source refreshes.
Cars must have a source publication time strictly later than the cutoff. An
official AUTO.RIA search explicitly constrained to a post-cutoff publication
window is also sufficient when no source date is available; a known old or
invalid date overrides that window proof and fails closed. Naive AUTO.RIA
addition dates use the existing explicit Kyiv/DST policy. Discovery requests
round the lower bound upward to provider whole-second precision, preventing a
fractional cutoff from reopening a pre-cutoff second.

The checks run at valuation fan-out, enqueue, stale-price refresh rejection and
immediately before Telegram transport. Active-window first-seen evidence alone
does not qualify. OLX requires identity-verified `createdTime`. The browser
worker uses explicit source addition dates or a conservative interval from a
recognized source age label; update/promoted/unknown labels do not qualify.
Rounded age labels can delay eligibility until publication is provably after
the cutoff; unknown publication dates remain excluded. No filtering or pricing
threshold changes are introduced.

The owner's first browser head includes proven post-cutoff cars even if they
were published while deployment was being prepared; it does not silently move
the cutoff to restart time. Sent/uncertain ledgers remain the final dedupe
authority. Repeating the same reset ID and timestamp does nothing. A reset ID
cannot be reused with another timestamp, and a later reset cannot move backward.

## Production procedure (confirmation required)

1. Review both PRs and confirm the affected owner, exact UTC cutoff and effects
   above. Back up Postgres and disk state using the existing backup procedure.
2. Stop **all** API/discovery/delivery/OLX and cloud/local owner workers (including
   any PC bridge). Both Render services were already suspended at inspection;
   do not assume there is no local copy. Wait for the monitor lease to expire.
3. Deploy both corrected branches while keeping workers stopped. The owner
   worker must never resume its old binary after the reset.
4. Through an authorized connection with the existing production environment:

   ```sh
   python -m backend.reset_notification_history --owner \
     --baseline 2026-11-17T18:47:50Z --reset-id owner-reset-20261117
   ```

   This is a read-only dry run by default (Postgres transaction enforced
   read-only). Review `searches`, `seen_interests`, `matches`, `unsent_deliveries`
   and the verified owner ID before proceeding. This timestamp was captured
   during the request; confirm it or explicitly choose another before apply.

5. After confirmation, repeat the same command adding `--apply --writers-stopped`.
   The single transaction locks the owner/control, validates the fixed cutoff,
   applies the changes and commits together. A failure rolls everything back.
   The flag acknowledges external workers are stopped; it cannot detect every
   independent PC/browser process.
6. Resume corrected services. The owner worker atomically synchronizes its local
   checkpoint/job state from the Postgres cutoff on its first cycle. Before any
   send it checks the persistent cutoff again. Existing source holds remain.
7. Check dry run again: `already_applied=true`. Inspect the cutoff, owner delivery
   states, feed cursors, current epochs and local checkpoint marker; observe a
   genuinely new matching car through normal discovery and delivery. Do not
   manufacture or replay a historical notification as verification.

No Postgres migration, new table, environment-variable change, extra queue or
new service is needed. Existing JSON columns/tables store the boundary. The
owner SQLite marker also uses an existing table. Both code deployments and the
approved one-time data operation are required. A cutoff is not a retry queue:
after real sends resume, moving it backward or replaying old claims is unsafe.

## Validation

Offline tests exercise old queued/listed/search-result cars, source-date
contradictions, new arrivals, restarts, duplicate prevention, shared recipients,
reset retries, transactional rollback, owner identity and every protected table.
Browser-worker tests cover old/unknown/promoted publication evidence, first-head
post-cutoff arrivals and preservation of SQLite identity/sent/uncertain ledgers.
No production Telegram messages or paid source requests were used in tests.

Existing `test_notifications.py` has five API access-denied failures on the
unchanged deployed commit as well. They are unrelated to the reset and are not
fixed here. Production row counts and post-deployment delivery remain unverified
until database access, approval and deployment are available.
