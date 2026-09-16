# AUTODeal backend — staging, delivery disabled

The GitHub Pages Mini App retains device-local drafts and now offers explicit
server saving through https://autodeal-api.onrender.com. The Telegram SDK supplies
raw initData; the API validates the signature and user ownership. No automatic
import occurs. Cloud saves always use enabled=false. A separate cloud list supports
read-back, restoring filters, and confirmed deletion. Open from a Telegram Mini App
button, not a normal browser link. No session data or bot token is persisted by the
client. Render hosting has been provisioned; authenticated end-to-end cloud saving
still requires an owner test inside Telegram. No webhook or worker is configured
by this integration. Real listings and valuation are still absent.

Frontend checks: `node --test cloud-test.cjs`, `node filter-test.cjs`,
`node storage-test.cjs`. The DOM harness checks behavior, not rendered visual layout.

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

- GET /health — read-only database probe: 200 with database=connected and
  database_type, or 503 with database=unavailable. Does not return credentials,
  user data or database error details. Reports delivery_available but does not
  verify the Telegram token, listing source, schema integrity or delivery.
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
# AUTO.RIA connection check

## Authenticated search and sample valuation

`POST /api/cars/search` accepts the existing Filters payload and requires valid
Telegram initData. Names are resolved against official AUTO.RIA dictionaries,
cached for seven days. Unknown/ambiguous filters return 422, never a wider search.
Result details are checked again against selected IDs and ranges. Mileage is
converted from the provider's thousands of kilometres into kilometres.

The test search inspects at most three candidate listings, with at most six
additional peers per candidate. There is no pagination or automatic polling.
Raw search IDs and sanitized details are cached for 15 minutes. The shared
PostgreSQL budget allows at most 24 calls in a rolling hour, 60 per rolling day,
and 900 lifetime (including a reserve of two for the first connectivity check).
Errors count toward the budget; 401/403/429 block further calls for one hour.
Limits persist across deployment. Only one search can spend quota at a time;
a lease recovers after 90 seconds if the process crashes. Calls made elsewhere
with the same key are not known to this budget. No paid APIs are called.

The provider's legacy median API is deprecated:
https://docs-developers.ria.com/en/used-cars/average_price/median_average_price
Instead, a conservative sample median needs five distinct OTHER active listings,
with the same make, model, generation, modification, body, fuel and gearbox,
year within one year and mileage within max(30,000 km, 20%). All must explicitly
be undamaged, in Ukraine and customs-cleared; missing data prevents an estimate.
Peer queries deliberately omit buyer price and region limits. A max/min price
ratio over two rejects a mixed sample. This is a small sample of asking prices,
not a validated appraisal or transaction prices. The UI states these limits.
Only an unrounded comparison of price <= median * 0.85 qualifies for deals.
Insufficient data, source errors or quota exhaustion NEVER fabricate a discount.

An additional once-only deployment check uses Volkswagen / Khmelnytskyi, without
the deals filter, through the same shared quota/cache. Its summarized result is
under `filter_check` in `/api/source-status`. It cannot be triggered by a visitor.
Existing connection-check status/preview describes the original one-time sample.

The UI now calls authenticated search instead of rendering demonstration cars.
Empty/partial results are labeled as a limited sample. Delivery remains disabled;
search results never write to the `listings` / `deliveries` tables.

Set server-only `AUTO_RIA_API_KEY` in Render. On startup, a one-time check calls
the documented used-car search (one newest active passenger-car ID) and info
endpoints, at most two requests in total. No paid valuation endpoints are used.
The claim is committed in the new `source_probes` table before any network call;
failures, process crashes and redeploys do not automatically retry. If a check
fails, investigate its status before explicitly arranging another attempt.

`GET /api/source-status` reads the cached check without contacting AUTO.RIA.
It exposes only a fixed status, timestamp, requests reserved, and a small public
listing preview. It never returns the key, upstream errors, seller data or VIN.
The preview is a historical connectivity sample, not a current search result.
The `checking` state can remain after an interrupted startup; it is not a retry loop.
`connected_empty` means a valid empty search, not verified listing details.

This does not connect Mini App filters, calculate a market price, ingest delivery
listings, start a worker, or change `SOURCE_READY` / `DELIVERY_ENABLED`.
Both flags must remain false during integration. Source attribution:
[AUTO.RIA](https://auto.ria.com/).

Official API contracts:
- https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto
- https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info
