-- SELECT-only PostgreSQL cohort snapshot. No receipts, names, tokens or IDs output.
-- Bind snapshot_at to one finite UTC Unix epoch. Bind excluded_ids to the bigint[]
-- returned by purchase_stats.excluded_user_ids(settings) from verified config.
-- Do not invent exclusions, interpolate identifiers, or print bound parameters.
-- Run through a read-only connection/transaction with a bounded statement timeout.
-- This file was prepared offline; its presence is not evidence of a live DB read.
--
-- Lifetime manual buyers = distinct approved positive UAH requests, regardless of
-- expiry. Operational access follows billing.allowed, including gifts/legacy pilot
-- and disabled enforcement. last_approved_expiry is not the entitlement expiry.
-- Anonymous Cxx labels are positions within THIS cohort, not stable public IDs.
-- enabled_filters contain no search names, payment evidence or Telegram IDs.
--
-- Historical limitations: control.enforce at acceptance, old /stop transitions and
-- previous filter snapshots are not fully recorded. A receipt without a covering
-- AccessEvent is NOT proof of an unauthorized send (free launch/pilot may apply).
-- API acceptance is not proof that a phone received a push or a user read the ad.
-- Real client counts/filters require executing this query successfully; do not
-- replace an unavailable DB with zeroes or with synthetic fixture counts.
WITH
p AS (
  SELECT CAST(:snapshot_at AS double precision) AS now,
         CAST(:excluded_ids AS bigint[]) AS excluded
),
cohort AS (
  SELECT u.*, row_number() OVER (ORDER BY u.id) AS anonymous_no
  FROM users u, p
  WHERE NOT (u.id = ANY(p.excluded))
),
buyers AS (
  SELECT user_id, count(*) AS approved_payments,
         max(expires_at) AS last_approved_expiry
  FROM manual_payment_requests
  WHERE state = 'approved'
    AND amount_minor > 0 AND currency = 'UAH' AND days > 0
  GROUP BY user_id
),
payment_times AS (
  SELECT r.user_id, min(a.at) AS first_owner_confirmation_at,
         max(a.at) AS last_owner_confirmation_at
  FROM manual_payment_audit a
  JOIN manual_payment_requests r ON r.id=a.request_id
  WHERE a.action='approved' AND r.state='approved'
    AND r.amount_minor>0 AND r.currency='UAH' AND r.days>0
  GROUP BY r.user_id
),
access_provenance AS (
  SELECT a.user_id,
         min(a.at) AS first_recorded_access_event_at,
         max(a.at) AS last_recorded_access_event_at,
         COALESCE(jsonb_agg(DISTINCT a.kind) FILTER (
           WHERE a.at<=p.now AND a.expires_at>p.now
         ),'[]'::jsonb) AS covering_access_event_kinds
  FROM billing_access_events a CROSS JOIN p
  GROUP BY a.user_id
),
pilot AS (
  SELECT user_id, max(paid_until) AS legacy_until
  FROM stars_test_orders
  WHERE state IN ('paid','refund_sending','refund_uncertain')
  GROUP BY user_id
),
operational AS (
  SELECT c.*,
         COALESCE(b.approved_payments,0) AS approved_payments,
         b.last_approved_expiry,
         pt.first_owner_confirmation_at, pt.last_owner_confirmation_at,
         ap.first_recorded_access_event_at, ap.last_recorded_access_event_at,
         COALESCE(ap.covering_access_event_kinds,'[]'::jsonb) AS covering_access_event_kinds,
         GREATEST(COALESCE(e.expires_at,0), COALESCE(t.legacy_until,0)) AS access_until,
         COALESCE(
           (SELECT enforce FROM billing_control WHERE id='commercial-v1'),false
         ) AS enforce
  FROM cohort c
  LEFT JOIN buyers b ON b.user_id=c.id
  LEFT JOIN billing_entitlements e ON e.user_id=c.id
  LEFT JOIN pilot t ON t.user_id=c.id
  LEFT JOIN payment_times pt ON pt.user_id=c.id
  LEFT JOIN access_provenance ap ON ap.user_id=c.id
),
interests AS (
  SELECT s.user_id, count(*) AS saved_searches,
         count(*) FILTER (WHERE s.enabled) AS enabled_searches,
         count(DISTINCT m.feed_id) FILTER (
           WHERE s.enabled AND w.epoch=m.epoch
         ) AS current_epoch_groups,
         COALESCE(
           jsonb_agg(s.filters::jsonb ORDER BY s.id)
             FILTER (WHERE s.enabled),'[]'::jsonb
         ) AS enabled_filters
  FROM searches s
  LEFT JOIN monitor_watches w ON w.search_id=s.id
  LEFT JOIN monitor_memberships m ON m.search_id=s.id
  GROUP BY s.user_id
),
receipts AS (
  SELECT d.user_id,
         count(*) FILTER (WHERE d.state='pending') AS pending,
         count(*) FILTER (
           WHERE d.state='sent' AND t.accepted_at >= p.now-3600
             AND t.accepted_at < p.now
         ) AS accepted_last_hour,
         max(t.accepted_at) FILTER (WHERE d.state='sent') AS last_accepted_at,
         count(*) FILTER (
           WHERE d.state='sent' AND t.accepted_at IS NULL
         ) AS sent_missing_timing,
         count(*) FILTER (
           WHERE d.state='sent' AND t.accepted_at >= p.now-3600
             AND t.accepted_at < p.now
             AND NOT EXISTS (
               SELECT 1 FROM billing_access_events a
               WHERE a.user_id=d.user_id
                 AND a.at <= t.accepted_at
                 AND a.expires_at > t.accepted_at
             )
         ) AS last_hour_without_covering_access_event
  FROM deliveries d CROSS JOIN p
  JOIN listings l ON l.id=d.listing_id AND l.source='auto_ria'
  LEFT JOIN delivery_timings t ON t.delivery_id=d.id
  GROUP BY d.user_id
),
rows AS (
  SELECT o.*,
         NOT o.enforce OR o.access_until > p.now AS access_allowed,
         COALESCE(i.saved_searches,0) AS saved_searches,
         COALESCE(i.enabled_searches,0) AS enabled_searches,
         COALESCE(i.current_epoch_groups,0) AS current_epoch_groups,
         COALESCE(i.enabled_filters,'[]'::jsonb) AS enabled_filters,
         COALESCE(r.pending,0) AS pending,
         COALESCE(r.accepted_last_hour,0) AS accepted_last_hour,
         r.last_accepted_at,
         COALESCE(r.sent_missing_timing,0) AS sent_missing_timing,
         COALESCE(r.last_hour_without_covering_access_event,0)
           AS last_hour_without_covering_access_event
  FROM operational o CROSS JOIN p
  LEFT JOIN interests i ON i.user_id=o.id
  LEFT JOIN receipts r ON r.user_id=o.id
)
SELECT jsonb_build_object(
  'checked_at', (SELECT now FROM p),
  'total_clients', count(*),
  'lifetime_manual_buyers', count(*) FILTER (WHERE approved_payments>0),
  'never_bought_manual', count(*) FILTER (WHERE approved_payments=0),
  'current_access_allowed', count(*) FILTER (WHERE access_allowed),
  'ready_enabled_without_access', count(*) FILTER (
    WHERE ready AND enabled_searches>0 AND NOT access_allowed
  ),
  'eligible_client_provider_groups', (
    SELECT count(DISTINCT m.feed_id)
    FROM rows r
    JOIN searches s ON s.user_id=r.id AND s.enabled
    JOIN monitor_watches w ON w.search_id=s.id
    JOIN monitor_memberships m ON m.search_id=s.id AND m.epoch=w.epoch
    WHERE r.ready AND r.access_allowed
  ),
  'historical_control_enforcement_known',false,
  'clients',COALESCE(jsonb_agg(jsonb_build_object(
    'client','C'||lpad(anonymous_no::text,GREATEST(2,length(anonymous_no::text)),'0'),
    'ready',ready,
    'lifetime_manual_buyer',approved_payments>0,
    'approved_payments',approved_payments,
    'last_approved_expiry',last_approved_expiry,
    'first_owner_confirmation_at',first_owner_confirmation_at,
    'last_owner_confirmation_at',last_owner_confirmation_at,
    'first_recorded_access_event_at',first_recorded_access_event_at,
    'last_recorded_access_event_at',last_recorded_access_event_at,
    'covering_access_event_kinds',covering_access_event_kinds,
    'access_until',access_until,
    'access_enforced',enforce,
    'access_allowed',access_allowed,
    'saved_searches',saved_searches,
    'enabled_searches',enabled_searches,
    'current_epoch_groups',current_epoch_groups,
    'enabled_filters',enabled_filters,
    'pending_autoria_deliveries',pending,
    'accepted_last_hour',accepted_last_hour,
    'last_accepted_at',last_accepted_at,
    'sent_missing_timing',sent_missing_timing,
    'last_hour_without_covering_access_event',
      last_hour_without_covering_access_event
  ) ORDER BY anonymous_no),'[]'::jsonb)
)
FROM rows;
