# Current paid access: transport boundaries and first activation

## Baseline and permitted scope

Production baseline for this follow-up: `161cd47ce11dfdaedb79c1ea7a5aa658b7c83979`.
The preceding quota-wait fix remains in place. The owner's October 3 instructions
authorize fixing and deploying paid AUTO.RIA access/delivery, not marketing,
OLX, a new payment method, new services or larger API caps.

## Concrete changes

- Approved manual UAH requests must have reached their recorded confirmation
  time as well as retaining both a paid expiry and current operational access.
  Existing old and new owner-confirmation writers use this same trusted record.
  No approval, entitlement or payment history is rewritten for rollout.
- Every uncached strict source operation needs an eligible, scoped server policy;
  one unrelated paid account cannot authorize an unbound diagnostic or worker.
  Scheduled policies test enabled searches, /stop, current watch epoch and current
  paid access. Authenticated UI reads cannot spend on a stopped account; cached
  dictionaries remain free. Existing shared scans, budget reservations, leases,
  pricing and cadence remain unchanged.
- On a new paid period, owner confirmation retains enabled saved filters and
  rebases their watch at the approval second. Disabled searches and /stop remain
  unchanged. Renewals of a current paid period retain the watch and paid days.
  Queued unpaid-period car sends are cancelled; shared listings and other
  recipients are retained. Second-resolution publication dates leave at most
  one ambiguous boundary second; later overlap is retained for index delay.
- The delivery worker reads a fresh clock after acquiring the user lock and
  checks database access at each actual Telegram request, after photo preparation
  and again before an explicitly rejected photo can fall back to text. Known
  pre-transport access read failures defer; a denial cancels. Timeouts after
  dispatch remain uncertain, never automatically replayed. A /stop mutation
  serializes with the worker's user lock: a request already in progress cannot
  be revoked. A newly confirmed access grant uses that lock too.
- Explicit one-shot delivery invocation uses the same strict engine policy as
  the server, rather than legacy entitlement-only access. The already authorized
  administrator copies remain copies of confirmed paid-client receipts, without
  a new paid query or implicit administrator purchase bypass.

## Compatible accounting migration

Add only `ria_api_authorizations` (attempt ID, fixed reason, SHA-256 group token).
The existing application `Base.metadata.create_all` creates it before workers
start. There is no ALTER/DROP/backfill of an existing table. Authorization and
the existing reservation commit together before HTTP; cache hits produce neither.
Old attempt rows lack this context and are explicitly counted as such, not
treated as unauthorized or reconstructed from guesses. Prospective transport
summaries count each actual HTTP attempt once, with category, retry relation,
reason and distinct groups. Neither group tokens nor identities are published.
Local observations are not provider invoices or verified remaining quota.

SQLite exercises additive creation twice and preservation of old rows; PostgreSQL
DDL is compiled locally. A PostgreSQL production rollout must additionally verify
the real startup and first scoped attempts. No public database firewall change
or production payment fixture is required.

## Evidence and reproduction

`test_paid_source_boundaries.py` adds paid approval, renewal, /stop, first-period
baseline, future confirmation, many unpaid searches, real photo/text transport
boundaries, unavailable access, forged identity, unscoped calls, cache accounting
and additive schema checks. Other affected suites retain pagination, restart,
parallel leases, source errors, multiple regions, optional fields, confirmed
provider lower bound minus 5%, user discount and owner-copy behavior.

The same fixture stream with two paid clients and zero or 50 unpaid clients uses
three shared discovery requests, one detail and one quote, and delivers the one
car independently to both paid recipients. The previous deployed tree passes
that same demand comparison. These are synthetic count/timing checks, not a
measurement of live coverage, provider billing, publication-to-phone latency,
or future daily savings. The prior tree fails the new first-activation and both
expiry-at-transport cases; the fixed tree passes them.

## Deploy, verification and rollback

Publish only these affected backend files on the existing repair branch, verify
workflow/Render targets and a fresh main head, then fast-forward authorized main.
Observe auto-deploy first; if the API ref update produces no deploy, request only
one build after confirming no queued/building deployment. Check live commit,
DB, private startup cohort/decision summaries and normal receipt logs without
extra paid test scans or client messages. Do not equate health=200 with delivery.

Rollback target is the baseline above, still containing strict paid-source gates.
Retain the additive authorization table and all newer payment/receipt data. Do
not clear accepted/uncertain deliveries, source accounting, filters or grants.
The supplementary 1,000-call rolling daily ceiling and one-hour publication TTL
still can limit intake; this release must not be described as complete market
coverage or as recovery of candidates already expired/discarded before it.
