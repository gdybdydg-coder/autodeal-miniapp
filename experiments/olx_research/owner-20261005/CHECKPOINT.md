# Owner-only OLX continuation — 2026-10-05, final verified checkpoint

Production commit: **3a5586a5259e62b371754502e72ae1625d7db25f**.
Render deployment: **dep-db1lklm0tbcc73bee2rg**, live 08:06:56.179154Z.
Verified health release and source diagnostics at 08:08:03Z.
**OLX OFF; real new OLX Telegram calls/receipts: 0.** Permission for the owner is
true; technical readiness is false; permission for other clients is false.
This is a foreground continuation under the new explicit user authorization.
Night state and research/f95f1c1 remain stopped and unchanged; the research branch
was not merged. Only selected OLX modules, tests and additive application wiring
were transferred to main. This evidence directory remains on the feature branch.

## Owner and isolation

Preflight 08:06:53Z: protected owner identity matches; genuine approved current
payment, ready status and one active search #4 verified. Saved fingerprint:
aa3dc66cadd6d6343f55f3930823173faf45741059cac1968b66a411b8a99be2.
Filters are unchanged: 1000–7000 USD, Vinnytsia/Ternopil/Khmelnytskyi/Chernivtsi,
threshold 10%, only deals. Configured recipient count is exactly one; active OLX
recipients are zero because the switch is false, search pin/deadline are zero,
profile absent and no new state is initialized. Both old OLX switches remain false.
The real getChat private-chat check is implemented immediately before every send;
it was not executed for a new live send in this run. Administrator role cannot
substitute for approved paid access. No fictitious subscription or filter edits.

Separate source namespace, ledger, single executor, budget and stop are prepared.
/olx_status and /olx_stop are deployed; /stop still prevents I/O and delivery.
Prepared canary schedule: 600s day, 3600s 23:00–08:00 Europe/Kyiv; 12 GET/hour,
60 GET/day, 160 MiB/day, 2 MiB/detail, 4s spacing, 20s timeout, no automatic retry.
Three total canary attempts maximum, including uncertain/rejected outcomes.
Old source+ad+recipient ledger keys are preserved. Ambiguous photo outcomes do
not produce a text fallback or repeated send. Continuous discovery is not installed;
a fixed reviewed candidate list is not a running continuous observer.

## Reused night evidence and fresh data

Night modules: attribute corroboration, body policy, FX, valuation, frozen controls,
stability and source parsing. Historical night data: 71 complete observations,
0 estimates, max 4 compatible advertised IDs, min 8. No night real Telegram sends.
New bounded focused A5 1.6 TDI search returned HTTP200 / 32 distinct cards.
Fourteen full details returned HTTP200: 13 refreshed known IDs plus one new ID,
72 unique saved observations overall. Selection was by complete characteristics,
not low asking prices. Three controls were frozen before refreshed full prices;
this was not blind to old stored prices. Frozen targets/crossposts are excluded
from reference membership by the evaluation module.

Confirmed fault 1: Russian OLX oblast labels did not match the owner's Ukrainian
saved labels. Reproduced 4 fail / 1 pass; explicit four-oblast equivalence fixed
only OLX matching. No new geographic region or saved-filter modification.

Confirmed fault 2: complete visible description936658970 reports a windshield
crack and body dents after hail, while generic running/minor-wear attributes hid
this damage. Reproduced 5 fail / 5 pass; after correction 10/10 condition cases pass.
Whole-car eligibility remains allowed and owner filters still match, but the
candidate now has 0 compatible same-condition peers. Negated and completed repairs
are distinguished; condition claims and uncertainty remain visible. Repair costs
are not inferred. Older unavailable HTML is not certified damage-free.

Current asking-price estimates: 0/72; fresh estimates 0/14. Maximum compatible
advertised IDs in the exploratory pool: 3/8, not independently proved physical cars.
Frozen controls: 3 loaded, 2 eligible, 0 estimated, maximum 2 reference peers.
Asking prediction error metrics are unknown and no method winner exists.
Median, trimmed mean and weighted median cannot be selected by profitability count.
Only 1/14 fresh validated VIN-derived claim; 0 manual independence reviews.
No independent sale/profit labels; sale accuracy and FP/FN remain unknown.

Two owner-filter matches are retained with insufficient valuation data:
936658970, displayed6500USD, explicit damage, no matching repair cohort;
935196654, displayed6700USD, explicit power missing (not guessed as105).
Their displayed prices are not market values or proof of 10% profitability.
All IDs/URLs, inclusion/exclusion reasons, prices, timestamp/FX provenance, frozen
memberships and sanitized receipts are in adjacent JSON. No raw VIN, plate,
contact, seller description or HTML is committed.

NBU403 hold respected. Official dated PrivatBank fallback effective2026-10-05,
fetched07:02:57Z:44.9857000UAH/USD. USD displays were not converted again.
No actual UAH Telegram card was sent. A further observed public A5 page11 GET
(reserved61e15ef) timed out at connection deadline, HTTP0, body0bytes; no retry,
bypass or new observations. All reservations remain charged. Foreground totals:
16 OLX GET /25,665,345 body bytes; reserved16 /36MiB of60 /160MiB. FX1 GET /2,836bytes.
The saved342 pending entries contain0known matching 1.6diesel/manual/A5/year2009–2011/
240–300kkm rows; missing attributes were not inferred. Unused budget remains;
this is not proof no other vehicles exist. Bounded public HTML works for the
observed pages; ongoing source authorization/completeness remains unverified.

## Tests and deployment evidence

Backend2057passed in318.24s; offline282passed in0.672s; saved-real replay3passed
in0.05s. Owner/condition targeted57passed in2.26s, overlapping backend cases.
Earlier combined2327+150subtests is historical. Network fences precede collection;
fixture transports and synthetic full cohorts are not live source/send evidence.
Every confirmed fix follows reproduction, test, fix, check and selected deploy.
Current production code exactly matches tested selected backend files. Default-off
runtime/preflight and filtered OLX error logs verified (0 matching errors).
The new owner profile was not installed or unlocked by a readiness flag.

AUTO.RIA208 preexisting backend files are byte-identical to afaf9ce; only app.py
has additive OLX settings/lifecycle/command wiring. Runtime valuation policy,
schedule, budget and recent processor limits, strategy and paid rules equal
baseline. Four watching groups, no genuine paid approval blocked, complete search
memberships; delivery queue pending0/sending0 at08:08. No environment updates,
paid RIA requests for OLX, payment/history/filter/webhook/getUpdates changes.

Actual ordinary AUTO.RIA acceptance40491864 at07:44:23.782993Z (10:44 Kyiv) for
the owner has saved approval/access covering acceptance. It occurred after5430
live deployment and before the final3a5586 live deployment; no new post-final
qualified acceptance was observed at this checkpoint. Acceptance is not reading.
Client acceptance40521478 at07:22:25Z follows the firstc488 default-off deployment.
Current monitoring/health cannot substitute for a new actual receipt.

## Exact blockers and next step

Owner permission persists. To enable valued cards, acquire a denser compatible
current cohort for the unchanged owner search, >=8 independently reviewed analogs,
>=3 estimable frozen asking controls and a stable method. Current code conservatively
requires corroborated unique VIN-derived keys and distinct photo reviews for profile
independence; this requirement is not satisfied by the current observations.
A whole-car repair candidate remains retained rather than silently discarded.
Do not fabricate missing power/condition/value, weaken min8, refill controls from
known prices, replay old samples, or enable other clients. No automatic resumption,
continuous observer or new ChatGPT background promise. Stages for wider rollout
and client delivery still require new authorization.


## New owner authorization recheck — 2026-10-05T12:03:21+03:00

User permission to enable sending is retained. This is not a readiness override.
Main/live remains3a5586a5259e62b371754502e72ae1625d7db25f; no production code/env/deploy
changes in this follow-up. OLX stillOFF; accepted/rejected/uncertain OLX receipts0.

New broad publicly observed A5 diesel query and its four observed pagination pages
allHTTP200:173 distinct advertised IDs across this moving relevance window. It is
not a complete catalogue or physical independence proof. Exactly two uncollected
explicit1.6diesel/manual/year2012/254–256k detail URLs were frozen as references and
reserved before full prices (5990bc3); bothHTTP200. Earlier3controls retain their
frozen membership. No retries, credentials, challenge bypass, Telegram or paid RIA.
New sourceGETs7 /21,384,005body bytes. Foreground episode totals23GET /47,049,350bytes,
reserved23GET /60MiB within60GET /160MiB; FX1/6. Budget is NOT exhausted. Lease released.

Current global asking-price coverage0/74 full saved observations; maxcompatible3/8.
Frozen controls3, eligible2, estimated0. Three method error metrics remainunknown;
no winner or sale/profit accuracy is fabricated. New936387379(Vinnytsia) displays
8500USD, above owner's7000 ceiling, and lacks explicitpower. New936646018(Zaporizhzhia)
displays8300USD, wrong region/above ceiling; globally one compatible older ad,
zero compatible peers within the refreshed16fixture. Unknowns remainunknown.
Earlier commentary incorrectly described the Vinnytsia candidate as matching
filters before checking its full price; explicitly corrected after the pricecheck.

Six saved-real assertions pass under a network fence before imports (runpy,0.05s);
three are new, three overlap previoussavedtests. Production pytest unavailable in
this runtime; no new full backend run claimed. Failed harness/denominator attempts
are recorded in enable-verification.json. Prior2057backend/282offline results are
historical verified evidence, not new live-delivery proof.

Latest structured runtime confirms protected owner's genuinepaid access, ready
status, one enabled search,4paid/filtergroups and unchanged RIA schedule/limits.
Real ordinaryRIA receipt40521711 accepted2026-10-05T11:08:55.149+03:00, after final3a5586adeployment.
APIacceptance does not prove reading. AUTO.RIA code/config/state not modified here.

Next: a verified denser compatible cohort with>=8independent refs and>=3estimable
separate controls, or a newly supported source/attribute hypothesis. Do not blindly
repeat the current5pages, reduce8, fillpower, activate old samples, or create a fake
profile. Remainingbudget37GET/100MiB reserved-cap headroom is available only for
a justified new source hypothesis; no background work is promised. Ownerpermission
persists, otherclients unauthorized.
