# Live verification: owner Stars test, 2026-10-01

Production main: 005897b0a93c7fe0166424341c0564a5fbaf94cb.
Tested and remote tree: 0cc95880c4c2676ac5c0d14a2427ae232ea67bed.
Full offline backend regression: 974 passed, 1 existing warning, 144.73s.

Render deployment dep-dav2570473hc73d7bkj0 became live at 2026-10-01T09:07:41.772737Z (12:07:41 Kyiv). AutoDeploy was configured yes, but repeated deploy listings after the ref update showed no new/queued deployment. One manual deployment was then triggered after rechecking for duplicates, using unchanged build cache and existing service.

Post-deploy /health returned ok=true, PostgreSQL connected, delivery_available=true.
Filtered Render error logs from 09:07 UTC returned no errors in the queried range.
Monitor parallel batch logs at 09:08 UTC show processing after startup. These observations do not prove complete listing coverage or personal notification delivery.

/api/source-status requests timed out both before deployment (15s and 25s). No full status was printed. We did not investigate or change that endpoint in this payment-only scope.

No real payment has been made by the assistant. The owner must send /paytest, read terms, then /paytest_confirm and approve a real 1-Star payment in Telegram. /payment shows persisted status; /refundtest requests a refund. The owner-scoped menu setup result was not independently retrieved; commands can be typed directly.

Paid access is an owner-only pilot record, not a global paywall. Existing public users, /stop, filters, claims and AUTO.RIA accounting are not changed by payment events. The separate discovery WIP fixes/free-search prototype were not merged.
