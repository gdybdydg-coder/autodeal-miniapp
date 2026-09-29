# Identical discovery request sharing (2026-09-29)

The monitor coalesces active feeds that have identical brand, model, region,
price and year constraints but different body, fuel, transmission or mileage
filters. Those optional fields were already excluded from upstream discovery
to allow ads with missing characteristics. Each subscriber's full filters,
minimum discount and valuation evidence key remain independent.

Regrouping runs under the monitor lease and user locks. Frozen pagination must
finish first; error/quota backoffs are not bypassed. The merged feed keeps the
earliest committed cursor and normal overlap, plus all subscription activation
times, epochs, seen records, matches, jobs and delivery claims. `/stop` remains
authoritative. No schema migration, historical scan or claim reset is needed.

When supplemental discovery is enabled, feed snapshots must agree before a
merge. This preserves initial-baseline and arrival semantics. Dormant feed rows
remain for diagnostics but are not scheduled without active members.

`monitor.compatible_searches` exposes the full subscription-filter group count
and the current discovery group count. Their difference measures removed
groups, not exact provider billing. Pagination, activation-time slices, retries,
details and valuations still consume requests. Migration can temporarily replay
the normal overlap; per-subscription deduplication still applies.

Different price/year/region ranges are deliberately not unioned. Such broader
queries require separate coverage and page-cost validation. Schedules, quota
caps, eligibility and Telegram delivery behavior are unchanged.
