# Paid-client delivery and owner copies

The owner requested automobile notifications for matching paid clients and a
copy in the configured administrator's chat. The deployed predecessor excluded
the administrator from paid-source access together with test/service accounts.

## Behavior

- Client collection and delivery still require a current owner-approved manual
  payment, current entitlement, ready chat, enabled search, matching filters and
  current valuation. No unpaid/test account starts source work.
- After Telegram accepts a paid client's card, the owner receives one copy per
  source listing. Copies require a durable client receipt and an internally
  created marker. No owner search, purchase, entitlement or readiness is added.
- The owner remains excluded from buyer statistics and source API work. The copy
  uses the existing car data, photo transport, queue, per-chat spacing and retry
  handling. It does not obtain another AUTO.RIA quote or refresh an old price.
- A copy is labelled with the observation time in Europe/Kyiv. Its immutable
  saved snapshot is historical; it is not represented as a freshly checked car.
- `/stop`, blocked-chat handling and the separate owner-copy switch are checked
  immediately before delivery. An ambiguous timeout remains `uncertain`.
- Multiple matching client receipts generate one owner delivery through the
  existing unique `(user_id, listing_id)` constraint. Restart retains queued
  cards and does not resend accepted or uncertain cards.

The dispatcher reconciles at most 50 recently accepted receipts once per second.
The five-minute reconciliation window repairs a short interruption before copy
creation. It does not replay a backlog after a long outage. Paid-client queue
processing continues if the owner-copy reconciliation fails.

## Specifically requested recovery

This release restores only AUTO.RIA 40514216 to the owner, once, if a saved
paid-client acceptance exists and the owner's chat is ready. Its original
price/valuation are visibly dated. This does not resend it to clients who
already received it or bypass another client's filters. The persistent recovery
claim prevents repeating this operation on later deployments.

The startup verification reads actual database records inside the backend and
logs aggregate paid/matched clients and delivery states for this listing. No
recipient IDs, receipts, tokens or seller contacts enter these logs.

## Tests

Run with a test Python environment containing `backend/requirements.txt`:

```sh
python -m pytest backend/tests/test_owner_car_notifications.py -q
python -m pytest backend/tests -q
```

Fixtures use temporary SQLite, a fake source and fake Telegram responses.
Coverage includes two paid/one unpaid clients, 200 matching paid clients sharing
one quote, owner without any paid clients, stop/expiry/filter changes, duplicate
enqueue, restart, rate limits, uncertain timeouts, immutable stale copies,
source failure isolation and the single requested recovery. No production seed
payments, real source requests or client test sends are used.

Local verification on 2026-10-03: 22 new cases passed; the full backend suite
passed 1,637 cases in 263.98 seconds. PostgreSQL compilation of the correlated
receipt query also passed. These are isolated checks; production acceptance
must be verified separately after deployment.

## Disable and rollback

`OWNER_CAR_NOTIFICATIONS_ENABLED=false` disables owner copies without disabling
paid-client monitoring. The default is `true` for strict production mode with a
configured administrator. Non-production/legacy mode does not create copies.
Pending copies are cancelled by the ordinary worker when this switch is off.

Alternatively deploy the predecessor commit
`4c3bc850234b4ac4752b0f3456cd39e150e5d0f5`. No schema migration or payment/search
rollback is needed. Existing client receipts and deduplication keys remain
untouched. Do not delete saved copy/recovery records to force a repeat.
