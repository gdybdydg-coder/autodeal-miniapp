# Supported-condition valuation check — 2026-09-17

The approved release was published and deployed successfully. The single live
check used **96 of the authorized 96 AUTO.RIA requests** and completed at
**2026-09-17 13:36:16 UTC**. It found one real price candidate meeting the exact
15%-below-median rule. The overall result remains **partial** because 16 candidate
valuations were interrupted by the per-model request cap.

Run: `eligible-20260917-1`; profile: `eligible-v1`.
Backend commit: `10e444ed030430587159c7f0591fd2ec189c5af9`.
Render deployment: `dep-dalup5qd0e5s738mupug` (live).
Mini App release: `20260917-27` (published assets verified).
[Retained sanitized evidence](valuation-audit-eligible-2026-09-17.json).

The provider query requested undamaged, customs-cleared cars in Ukraine. This
targets the estimator's supported condition and is not representative of all
listings for each model. Local detail checks still apply to every returned car.

| Model | Candidate cards | Completed medians | Pending valuations | Calls |
| --- | ---: | ---: | ---: | ---: |
| Volkswagen Passat | 8 | 1 | 7 | 32 |
| Audi A6 | 8 | 0 | 5 | 32 |
| Mercedes-Benz E-Class | 8 | 0 | 4 | 32 |
| Total | 24 | 1 | 16 | 96 |

Of the other seven completed candidate checks, three lacked required vehicle
identifiers and four had fewer than five eligible peers. Pending candidates must
not be reported as having no suitable analogs or as non-deals. The audit does not
resume or retry automatically; no further provider requests were started.

## First qualifying price candidate

[Volkswagen Passat 2012 #40444164](https://auto.ria.com/auto_volkswagen_passat_40444164.html):
asking price **$8,700**, 244,000 km, modification `2.0TDI DSG (140 к.с.)`.
The source reported eligible condition. The listing page and physical condition
were not independently inspected; this is an API-level price candidate.

| Other listing | Year | Mileage (km) | Asking price (USD) |
| --- | ---: | ---: | ---: |
| [38493060](https://auto.ria.com/auto_volkswagen_passat_38493060.html) | 2013 | 256000 | 9500 |
| [37229849](https://auto.ria.com/auto_volkswagen_passat_37229849.html) | 2013 | 210000 | 10500 |
| [40301155](https://auto.ria.com/auto_volkswagen_passat_40301155.html) | 2013 | 226000 | 11700 |
| [39040100](https://auto.ria.com/auto_volkswagen_passat_39040100.html) | 2012 | 263000 | 12400 |
| [40108995](https://auto.ria.com/auto_volkswagen_passat_40108995.html) | 2013 | 207000 | 12900 |

All five share make, model, generation, modification, body, fuel and gearbox with
the candidate and satisfy the existing year/mileage and condition requirements.
Nine other retrieved peers were rejected for a different or missing modification.
The independently recalculated median is **$11,700**; the exact deal threshold is
**$9,945**. The candidate is **25.6% below the sample median**. These are historical
asking prices, not completed sale prices or a guarantee of resale profit. Fresh
details and subscription filters must be checked before any actual notification.

## Catalog verification

Each diagnostic removed the known ID from a separate copy and resolved its full
modification name in the official generation/body catalog. The production
candidate kept its original ID. All three catalog results matched the IDs already
provided by the listing:

| Model | Listing ID | Listing and catalog modification ID |
| --- | --- | --- |
| Passat | 40444164 | 9804 |
| A6 | 40443603 | 164629 |
| E-Class | 40131634 | 157924 |

This confirms the lookup on real provider data. It does not mean three original
listings were missing an ID; missing names still cannot be resolved. All eight
completed candidate comparison reports agreed with the production estimator.

## Deployment verification and next work

The database health check returned HTTP 200 with PostgreSQL connected and
`delivery_available=false`. Monitoring remained offline. The sampled application
error log contained no new errors. No Telegram message was sent by the audit.
The deployed code passed 112 backend tests and 41 frontend checks before release.

The next valuation task is request efficiency and completion: investigate why
the provider returns many peers outside the requested modification, preserve
strict local checks, and reuse eligible peer data across nearby searches. Audi
and Mercedes still lack a completed live median in this check. No additional paid
resources or provider valuation product were enabled. A fresh real-car delivery
test remains a separate launch step.
