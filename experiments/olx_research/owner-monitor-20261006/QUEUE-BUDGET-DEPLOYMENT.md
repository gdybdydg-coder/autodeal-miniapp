# Post-deployment evidence — 2026-10-06 16:18 Europe/Kyiv

Production commit f4f65c7d0865ca083a8dfed201036b6057cf5bc5; Render dep-db2f987lk1mc73b7afdg live at 16:17:39 Kyiv. Startup at 16:17:36 preserved active owner monitor, one active search, 200 RIA/day allowance, 53 pending, 157 baseline, one accepted, one below threshold, ten excluded; counters preserved (76 OLX requests, 9 RIA requests). Interval 600s; next normal cycle due 16:21:45 Kyiv. No forced immediate tick, no reset, no replay.

Actual Telegram evidence predates this deploy: OLX 937002546, sendPhoto, message_id 17651, accepted 16:11:19 Kyiv. This is API acceptance, not reading. No post-patch cycle has yet been observed at this checkpoint; budget fix is deployed and isolated tests passed (164), live effectiveness must be checked on subsequent normal cycles.

Fresh public aggregate status compared before/after: AUTO.RIA valuation policy, budget limits, schedule periods and parallelism equal; monitor running, valuation and delivery queues zero at observation. Last available AUTO.RIA acceptance 16:15:56 Kyiv. No claim of uninterrupted delivery based solely on health. No AUTO.RIA implementation files changed in patch.

Next: inspect normal cycle for creation-date cleanup/canonical redirect reuse/detail processing and next receipts; preserve all existing caps and owner access guards. Unknown native-app parity of RIA parameter estimate remains a separate limitation.
