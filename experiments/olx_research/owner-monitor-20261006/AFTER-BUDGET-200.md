# OLX owner monitor after authorized200/day — 06.10.2026
Production: c07bac70601b5a254542f71bd06abab64399e47b.
Deploy dep-db2efprncjis73ee3j8g live15:23:09 Europe/Kyiv.
Explicit daily allowance200, hourly20, owner only; saved approval15:10:46.
First paid-enabled cycles:15:17:41 and15:28:47. Runtime confirms200/day.
At15:28:47: active search1, baseline157, pending35, excluded8.
All eight reviewed candidates excluded with old_or_unconfirmed_source_creation.
This reason combines preexisting creation and missing/conflicting date evidence;
it does not prove which subreason applies to each saved exclusion.
RIA calls0, FX calls0, Telegram accepted0. No new live recurring quote/send observed.
The sender is armed through OLX_OWNER_MONITOR_ENABLED=true; no forced readiness.
Old focused/batch senders remain off. Existing payment, search and delivery history retained.
155 offline tests pass, including the exclusion log reproduction/fix regression.
AUTO.RIA current policy, quota limits, schedule and parallelism4 unchanged.
Actual AUTO.RIA delivery accepted15:24:49, discovery→Telegram20.439s; pending/sending0.
Next server cycle15:38:47 (30-second polling can add delay).
Schedule600s08–23,3600s23–08 Europe/Kyiv; free source40 GET/hour600/day2GiB cap.
These caps can defer work; bounded newest windows are not exhaustive coverage.
RIA daily cap includes dictionary and valuation calls; failed reservations stay spent.
No purchases or provider-balance assertions. Lower bound×0.95, threshold10 unchanged.
Stop: /olx_stop; status:/olx_status. /stop is also respected.
Hosted SQL unavailable due empty external IP allowlist; no networking changes.
Next: verify fresh candidate mapping, actual quote and receipt when a candidate qualifies.
Do not send below-threshold, old baseline or uncertain-source cars to manufacture delivery.
