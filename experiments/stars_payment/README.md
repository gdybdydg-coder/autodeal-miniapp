# AUTODeal: owner-only Stars payment pilot — 2026-10-01

Separate offline prototype based on main 901d96fe1f7e10196155ef6dd329da9d62f2a250
(tree 0f1392a7cc7a3f5c8e2eba7af0851561b97e4ef2).
WIP discovery head verified 2c5ce7fb94028b9abffae569b2a0acf2a7b4c72f.
Render latest live deployment remained dep-daumfo59fdbs739haga0 on the same main.

No backend imports, production routes, billing gates, env changes or deployments.
No Telegram HTTP calls, real charges, AUTO.RIA calls or subscription changes.
Owner ID used in tests: 777292211, supplied previously by the user; deployment
must confirm against the existing authenticated admin configuration.

Implemented: default disabled; private-owner-only invoice payload builder;
owner checks on precheckout and payment processing; currency XTR and exactly
1 Star; unique random order token; 15-minute validity; local SQLite payment
record and simulated 30-day expiry; atomic duplicate-charge prevention across
restart. Invoice/precheckout never grant access. No renewal or delivery logic.
Forwarding invoice safety requires server checks, not merely hiding a button.

Verification: `python -m unittest discover -s experiments/stars_payment -v`
6 tests passed, using only stdlib and temporary local SQLite.

This is NOT a working payment UI in the owner's existing bot and is NOT ready
to deploy. The user authorized a private test; this checkpoint avoids changing
the running bot while the prior production freeze remains in place.

Next steps before a narrowly scoped live pilot:
1. Integrate an authenticated webhook and owner command/callback in a reviewed
   backend change; keep public commands and users' access unchanged.
2. Define durable database orders, payment update replay (including late
   successful events), concurrent invoice/checkout behavior and expiry handling.
   This offline prototype rejects events after 15 minutes, so delayed paid
   events need reconciliation before any real charges are enabled.
3. Implement /paysupport, terms acceptance and refundStarPayment with a ledger.
4. Test webhook authenticity, non-owner callback/deep-link/forwarding attempts,
   failed and uncertain invoice transport, duplicate/late updates, refunds and
   restart without altering searches, /stop, claims or RIA budgets.
5. Use Telegram's dedicated test environment for simulated Stars. A 1-Star
   invoice in ordinary Telegram spends real Stars; communicate this clearly.
6. Full offline backend regression and review, then explicit resolution of the
   production freeze before installing any visible pilot in the working bot.

Official reference checked 2026-10-01:
https://core.telegram.org/bots/payments-stars
