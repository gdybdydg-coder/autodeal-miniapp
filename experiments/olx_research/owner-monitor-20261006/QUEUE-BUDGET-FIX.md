# OLX queue budget correction — 2026-10-06

Observed production c07bac70601b5a254542f71bd06abab64399e47b: at 15:39, 15:49 and 15:59 Europe/Kyiv discovery exhausted the shared 40 OLX GET/hour budget and prevented pending detail processing. At the next hourly reset, Telegram accepted OLX 937002546 (sendPhoto, message 17651, 16:11:19 Kyiv); this preceded this correction. It proves acceptance, not reading.

Correction: discovery subset capped at 20 of the existing 40 GET/hour; discovery budget errors do not skip pending processing; validated observed canonical search redirects are cached; old creation dates matched by visible ID and URL can exclude preexisting ads before detail I/O; recent known candidates have queue priority. Recent search metadata never authorizes delivery: full detail, filters, fresh paid owner access, valuation and send guards remain required. Unknown creation stays unknown. Source holds remain persistent.

Validation: 164 isolated tests passed. A saved real complete search snapshot from 14:10 Kyiv yields 52/52 identity-matched creation dates; this is saved evidence, not current ad availability. Tests cover budget partition, queue starvation, redirect reuse, matching/mismatched/future dates and preexisting pending cleanup. No AUTO.RIA code or configuration change. OLX RIA allowance remains 200/day and 20/hour; existing source cap remains 40/hour, 600/day. Existing state and receipts are preserved.

Deployment and post-deployment observations must be recorded separately; tests alone do not prove live operation. Next: verify production SHA, AUTO.RIA invariants, next normal OLX cycle and Telegram receipts. No forced replay or historical baseline reset.
