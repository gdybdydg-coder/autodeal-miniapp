# Zero-paid API: offline end-to-end pipeline

Checkpoint: 01.10.2026, about 07:35 Europe/Kyiv. Production was not changed.

Added `experiments/free_search/offline_pipeline.py`, which composes the existing
isolated parsers, publication proof, HTTP budget, filter gate and durable queue.
It accepts sanitized caller-supplied HTML only. It has no network client,
provider key, database, backend startup, AI service or Telegram sender and
cannot operate the production bot.

The fixture flow is now executable end to end:

1. The first public-card snapshot establishes a no-send baseline.
2. Only a genuine later `data-add-date` creates a candidate. Update, reprice,
   raise or first appearance still do not.
3. One shared detail-work key is transactionally enqueued. If the bounded queue
   is full, publication progress is rolled back so the candidate is not silently
   lost and can be reconsidered on a later snapshot.
4. Terms, robots and public-route gates plus the HTTP request/byte reservation
   must allow the synthetic fetch. A denial, 429 or budget pause creates no
   detail evaluation or delivery claim.
5. One supplied detail fixture is parsed once, joined to category, region,
   visible creation date and active positive USD price, then checked against all
   current saved filters. Known contradictions and known abroad/customs flags
   block. Missing optional fields and damage do not block.
6. One caller-supplied research estimate is shared across all matched searches.
   Each saved `minDiscount` is applied independently. Unconfirmed research
   creates no claims. `/stop`, activation times and epochs are rechecked by the
   durable queue immediately before claims are created.

Pipeline state has a strict restart roundtrip for publication baseline, pending
detail candidates, HTTP ledger, research candidates, stopped subscriptions,
epochs, seen rows and claims. A reserved request remains charged and delayed
after restart. Queue overflow cannot advance the publication cursor. A genuine
later publication under an old ID may be checked again, but an existing user/
listing claim prevents a repeated alert.

Verification: 14 new pipeline tests passed. The complete isolated experiment
suite passed **136 tests** with zero failures. It covers the whole fixture path,
one shared detail and one shared estimate for multiple users, current filters,
different minDiscount values, missing optional facts, known conflicts, terms
denial, 429, transactional overflow, restart, `/stop`, confirmed-only behavior,
update-only rejection and old-ID fresh-publication deduplication. No full backend
regression is claimed because the experiment is not imported by production.

This run made zero AUTO.RIA public-page requests and zero paid AUTO.RIA API
requests. Test permission values that allow the synthetic flow are fixtures,
not authorization for live collection. Continuous collection remains blocked
by the unresolved official-terms conflict documented in the source audit.

Next step: obtain written clarification from AUTO.RIA, then define measurable
7–14 day shadow acceptance criteria before any live zero-paid collector exists:
new-publication recall against the paid path, p50/p95 discovery delay, filter
agreement, overflow/denial rates and estimate error. No Telegram delivery or
paid-path replacement should be enabled until those criteria are approved and
met. Own price research remains experimental and does not replace official paid
AI or the production `confirmed_deals_only` policy.
