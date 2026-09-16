# AUTODeal backend — prepared, NOT deployed

The GitHub Pages Mini App is unchanged. It still stores device-local drafts.
This backend is not connected to it and sends nothing by default.
No bot token, database, webhook, external source or hosting resource has been configured.

## Components

- FastAPI subscription API; PostgreSQL via SQLAlchemy (SQLite only in local tests).
- Raw Telegram initData HMAC validation, one-hour expiry and per-user ownership.
- Private /start and /stop webhook with a secret header; no unsolicited startup messages.
- Trusted internal listing ingestion; NO scraper or valuation algorithm yet.
- Matching of body, fuel, transmission, price/year and mileage, minimum 15% below
  the supplied market estimate. At least five comparables must be declared by the
  future trusted valuation adapter. This is a guard, not a valuation model.
- Delivery deduplication per Telegram user and source listing, across all searches.
- Pending messages recheck consent/filters/freshness immediately before sending.
- 429 retry scheduling, 403 disables delivery. Ambiguous network outcomes are
  quarantined as uncertain rather than retried and potentially duplicated.

## Local development

From the repository root, in a dedicated Python 3.12+ virtual environment:

```sh
pip install -r backend/requirements.txt
pytest backend/tests -q
uvicorn backend.app:factory --factory --host 0.0.0.0 --port 8000
```

Set DATABASE_URL, TELEGRAM_BOT_TOKEN and a random TELEGRAM_WEBHOOK_SECRET of at
least 32 characters through server-only environment variables. PostgreSQL URL:
`postgresql+psycopg://...`. The two delivery flags default to false.
No dotenv file is loaded automatically. Tests use fake tokens, isolated SQLite
databases and injected fake senders; they never contact Telegram.

## API

Authenticated requests supply raw Telegram.WebApp.initData in the
`X-Telegram-Init-Data` header, never a browser-supplied user/chat ID.

- GET /health — readiness/feature flag only, not a delivery guarantee.
- GET /api/subscriptions — this user's searches.
- POST /api/subscriptions — {name, filters, enabled}; filters match the Mini App's
  names and kilometre units (mileage bounds are in thousands). Same filters update
  one subscription. Maximum 20 per user.
- PATCH /api/subscriptions/{id} — {enabled: true/false}.
- DELETE /api/subscriptions/{id} — owner-only deletion.
- POST /telegram/webhook — Telegram-only, secret header required.

Enabling requires both server flags and a verified private /start. /start never
re-enables old searches after /stop. A new/re-enabled search only considers
listings ingested after activation, preventing a flood of old advertisements.
No silent import of device-local notification preferences is implemented.

## Deployment gates — approval required

1. Choose/authorize hosting and review its current costs before resource creation.
2. Provision PostgreSQL and an HTTPS API. Add secrets through the provider UI,
   never chat, source control, front-end JS or request logs.
3. Review existing bot webhook/polling before any change; do not replace an existing
   bot integration without approval. Register this webhook deliberately with
   secret_token and allowed_updates=["message"]. No registration happens on boot.
4. Integrate an authorized real listing source and valuation process, map vocabulary
   to the frontend, validate freshness/source identity. No real data source exists yet.
5. Connect Mini App to API: load official Telegram SDK, validate initData server-side,
   explicitly confirm each cloud subscription and request write access/start as needed.
6. Run a single approved test to the owner's verified private bot chat.
7. Only then enable flags and configure a worker schedule/rate budget.

Worker entry: `python -m backend.worker`; one invocation attempts at most one message.
The initial implementation scans listings, intended for small-scale staging only.
Add incremental ingestion cursors, indexes/batching and per-chat rate limits before
large-scale use. Add reverse-proxy request/body/rate limits and database backups.
Use one API worker initially. User creation handles uniqueness conflicts with a
savepoint; PostgreSQL user-row locks serialize existing-user edits.
Production PostgreSQL/concurrency behaviour has not been tested here.

Exactly-once Telegram delivery cannot be guaranteed across a crash after sending.
Rows left in sending/uncertain are not automatically retried; investigate manually.
This chooses avoiding duplicate messages over guaranteed delivery. A message already
in flight at /stop cannot be recalled. Commands are ordered by message date and
update_id, accommodating Telegram's update ID reset after long inactivity.

## References

- [Telegram initData verification](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
- [Telegram webhook secret](https://core.telegram.org/bots/api#setwebhook)
- [Telegram sendPhoto](https://core.telegram.org/bots/api#sendphoto)

Do not expose database/admin ingestion endpoints to the Mini App. Names, tokens and
initData must not be logged. User IDs/filters are personal data: decide retention,
account deletion and the privacy notice before accepting other users.
