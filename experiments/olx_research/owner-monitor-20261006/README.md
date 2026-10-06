# Owner-only recurring OLX monitor — 2026-10-06

User explicitly requested full owner monitoring at14:08 Kyiv. Current production at start:92b6292857fb9b1948bca952e928de14091b04c5; one-shot probe completed, focused send switch false, regular feed false. No new paid allowance may be inferred from the old30 calls/6 AI probe cap (spent24/4).

Implementation uses existing paid-confirmed and active-search rules, all currently supported owner searches, a separate durable SourceProbe queue, atomic lease, hourly/daily call/byte limits and source+ad+recipient send receipts. /stop and /olx_stop remain effective. Old batches are not rearmed. Formula remains provider lower bound x0.95 and owner's own threshold. Condition is not a filter. NBU then Privat official rate then valid cache; USD is not converted twice.

Source evidence: public category page fetched06.10 at14:10:41 Kyiv,HTTP200,4039010bytes,cap4MiB,no retries. Explicit current oblast links: /uk/transport/legkovye-avtomobili/vin?currency=UAH (Vinnytsia), /ter?currency=UAH (Ternopil), /khm?currency=UAH (Khmelnytskyi), /chv?currency=UAH (Chernivtsi). Observed newest sort created_at:desc. Up to3 public pages per oblast until previous-window overlap. Gaps are recorded, not called complete coverage. Initial snapshot is a baseline; previously created/refreshed/raised old ads do not become new publications. Raw HTML is scratch only.

Schedule:600s08:00–23:00 and3600s23:00–08:00 Europe/Kyiv. Free source budgets:40OLX GET/hour,600/day,2GiB reserved bytes/day;FX12/day; requests pause4s,timeout20s,public search4MiB/detail2MiB. Source401/403/429/CAPTCHA persists hold across restarts; no bypass.

Production rollout: initial switch off, then owner collection only with OLX_OWNER_MONITOR_RIA_DAILY_LIMIT=0. This does NOT authorize/schedule paid valuations or prove any new Telegram delivery. A positive daily allowance requires explicit approval. Proposed allowance100 AUTO.RIA requests/day,20/hour, including dictionaries and AI; maximum3000 requests per30days; no purchases. Existing probe totals must remain intact. Once funded, queued candidates are re-fetched, re-filtered and quoted before any owner send. Other clients have no recipient path.

154 isolated tests passed: actual subscription policy with synthetic source/Telegram transports, baseline/new/restart dedup, wrong-owner/unpaid/stop/no-I/O, source hold, atomic lease, persistent budgets and default-zero paid allowance, pagination overlap, timezone, NBU fallback, UAH conversion and no USD double conversion, lower-bound-minus5 regressions. Synthetic success is not live delivery evidence.

Limitations: first newest windows are bounded, not guaranteed exhaustive discovery. Exact RIA model/attribute mapping must be unambiguous. Unknown/ambiguous necessary mapping stays unassessed; missing fuel/gear/photo alone is not a known filter contradiction. Parameter API range is not proven identical to native app range, and provider quantity is not an independently audited comparable count. Recipient stays one protected owner with current confirmed paid access; no client rollout.

Rollback: disable only OLX_OWNER_MONITOR_ENABLED. No RIA configuration/search/valuation/queue changes. app.py changes only create/await the new isolated OLX worker. Existing tariffs, advertising, webhook, polling and paid access are unchanged.
