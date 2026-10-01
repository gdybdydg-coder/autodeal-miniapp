# Zero-paid API: offline bounded queue and claims

Checkpoint: 01.10.2026, about 05:30 Europe/Kyiv. Production was not changed.

Added `experiments/free_search/offline_queue.py`, a pure state-machine model
with no network, API, database, backend, Telegram or delivery integration. It
models one shared job per listing, bounded candidate and delivery-claim queues,
activation timestamps, search epochs, per-search seen state and the durable
per-user/listing claim used as final deduplication authority.

The model snapshots matching recipients before one shared evaluation and
rechecks `/stop`, current epoch, enabled state and activation time after that
evaluation. A late join cannot receive an earlier publication. Two matching
searches for one user create one delivery claim. `confirmed_deals_only` is
represented by requiring `confirmed_deal=True`; an unconfirmed valuation creates
no claim.

Before hypothetical network I/O, `begin_send` rechecks that at least one matching
search still has the exact epoch and then persists `sending`. `/stop` disables
the user's subscriptions and cancels pending claims without deleting filters or
changing sent/uncertain/failed history. On restart, an evaluating job returns to
pending, while `sending` becomes `uncertain` and is never blindly replayed.
Every existing claim state, including cancelled or failed, prevents a duplicate.

A genuine later add date can requeue an old listing ID, but previous user claims
and same-epoch seen records prevent duplicate alerts. This is consistent with
fresh-repeat discovery plus delivery deduplication; update/reprice/appearance is
still not publication evidence.

Verification: 19 offline queue tests passed. They cover fan-out, two searches
for one user, late activation, in-flight epoch changes and `/stop`, confirmed-only
suppression, send rechecks/outcomes, restart recovery, sent/uncertain claims,
old-ID fresh publication, bounded overflow, strict serialization and dependency
isolation. No production regression is claimed because nothing is imported by
the backend.

Next step: add a bounded HTTP-budget/backoff state machine for a hypothetical
shared fetcher, including durable reservation-before-I/O, robots/429 denial,
byte caps and restart behavior. It must remain offline and cannot enable live
collection while the service-terms ambiguity remains unresolved.
