# Zero-paid API: fixture-only visible evidence adapter

Checkpoint: 01.10.2026, about 04:35 Europe/Kyiv. Production remains unchanged.

Added `experiments/free_search/visible_adapter.py`. It has no fetcher, API,
database, backend, Telegram or delivery imports. It accepts caller-supplied HTML
fixtures only and retains four allowlisted facts: listing ID, category, oblast
and visible creation date. Seller name, phone, VIN, description, photos and raw
HTML are neither returned nor persisted.

Category and oblast must be exact pre-title breadcrumb labels. Similar footer
or recommendation links after the first H1 are ignored. Conflicting categories,
regions, IDs or creation dates fail closed. The adapter joins these facts with
the prior add-date candidate and public JSON-LD details only when all listing
identities agree and the visible creation calendar date matches the Kyiv date
of the card's actual add timestamp.

An old numeric ID is allowed when genuine later add-date evidence passes the
existing baseline/dedup stage and the visible creation date agrees. Update
timestamps, first appearance and preview repricing are not substitutes for the
add date. A changed preview price is recorded as a non-authoritative issue.

Only active USD detail evidence supplies the current positive price. Known
non-passenger categories remain explicit and are blocked by the filter gate.
Missing optional characteristics remain unknown; damage remains a notice.
Abroad/customs stay unknown because the bounded public observation did not
establish a stable structured field. The adapter and gate never mark a result
ready for delivery; confirmed valuation and `minDiscount` remain unresolved.

Verification: 17 adapter tests passed, covering positive and negative category
fixtures, footer false positives, conflicts, identity/date joins, old-ID fresh
publication, hidden-script spoofing, update/reprice separation, currency/availability, missing optional
facts, damage and dependency/resource bounds. The existing 12 filter-gate tests
also passed in the selected run (29 total). No production code or full backend regression
was involved.

Next step: build bounded queue/restart state entirely offline around sanitized
fixtures, including `/stop`, epoch changes, sent/uncertain claims and stopped
subscriptions. Do not start continuous public collection while the terms
ambiguity documented in `zero-paid-api-source-audit-2026-10-01.md` remains.
