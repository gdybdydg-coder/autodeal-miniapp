# Fast search and full catalog access

The observed release-28 scan collected 3,200 of 35,156 Volkswagen IDs before
evaluating any candidate. Enumeration-before-valuation was an application
ordering issue. A broader constraint remains: fresh details and price comparisons
for an uncached market cannot be retrieved in a one-second API response.

The prepared release-29 change returns already checked public cars from our own
database and evaluates an early candidate while ID collection continues. It
preserves all-page scanning, the existing budget caps and notification settings.
It does not download a full source database, establish a continuous market feed,
or guarantee subsecond arrival of new ads. No new live AUTO.RIA calls were used
to develop or test the change; fixtures supply the source responses.

## What AUTO.RIA documents

Official pages checked on 2026-09-17:

- [Products](https://developers.ria.com/products?s=ua) lists used-car search,
  details by ID, median pricing and other APIs. No full-market dump or change
  feed is explicitly offered in that product list.
- [Pricing](https://developers.ria.com/payment/) lists the 100,000-request package
  with 5,000 requests per hour. Enterprise request allowances and prices are
  individual. This does not itself confirm bulk-export access.
- [Partner program](https://developers.ria.com/partners/become_our_partner/)
  offers custom APIs, individual limits and expanded access, with examples
  focused on listing management. A full-market export remains unconfirmed.

The official contact is **info_developers@ria.com**. No inquiry has been sent.

## Questions to settle before a full-market import

1. Is an initial snapshot of all active used-car listings available for AUTODeal?
2. Can we receive new/changed listings and removal/sold status incrementally?
   What is the supported frequency and typical delivery delay?
3. Can the data be retained in our database for commercial filtered search,
   comparative price estimates and opt-in Telegram alerts?
4. Are listing IDs, variant identifiers, condition, price, mileage, location,
   timestamps and source URLs included? No seller contact details are needed.
5. What setup/recurring cost and request accounting apply to snapshot and updates?

## Target flow after the data agreement is confirmed

Import a resumable initial snapshot, then upsert changes by source listing ID.
Track source coverage and update age explicitly; record deletions and sold cars.
Compute comparable prices from eligible, recent cars before the user's request.
Serve indexed filter queries from PostgreSQL and match newly evaluated deals to
opted-in saved searches. Deduplicate notifications and monitor update lag.

A fast local query and fast source-to-bot notification are separate measurements.
A one-off dump without continued updates is not sufficient. Set latency targets
only after the actual feed, dataset size and host performance are measured.

## Local validation

The full suite passed 131 backend and 50 frontend tests. A final focused check
adds coverage for an older scan reading newer shared data and removed listings.

A synthetic local database contained 35,000 cars. An authenticated FastAPI start
response returned 50 cached matches while the new scan had checked zero cars.
The initial response took 46.3 ms; 20 subsequent reads had a median of 24.4 ms,
a p95 of 29.0 ms and a maximum of 37.9 ms. The filter matched 9,003 cached cars,
with pagination returning only the first 50 in each response.

These are SQLite/in-process TestClient measurements using synthetic records.
They exclude mobile networking and do not measure Render/PostgreSQL, upstream
AUTO.RIA speed, complete-market coverage or notification delivery. Live provider
requests used for this benchmark: zero.
