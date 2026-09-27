"""Opt-in, read-only comparison of anonymous recipients of the same listing.

No source/Telegram calls, state changes, public endpoint, or recipient selector.
Current subscription first-seen records are supporting evidence, not a historical
snapshot of filters. DeliveryTiming.discovered_at is shared and cannot establish
when a particular recipient's search first found a car.
"""
import json
import logging
import math
import os
import time
from collections import Counter, defaultdict

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import Delivery, DeliveryTiming, Listing, MonitorSeen, Search

log = logging.getLogger(__name__)
WINDOW = 48 * 3600
MAX_COHORTS = 200
MAX_RECORDS = 10000


def difference(start, end):
    if start is None or end is None:
        return None
    return round(end - start, 3)


def summarize(rows, seen):
    """Compare first and last acceptance, keeping the decomposition signed."""
    rows = sorted(rows, key=lambda r: (r["accepted_at"], r["delivery_id"]))
    first, last = rows[0], rows[-1]
    gap = difference(first["accepted_at"], last["accepted_at"])
    queued = difference(first["queued_at"], last["queued_at"])
    waits = [difference(r["queued_at"], r["send_started_at"]) for r in (first, last)]
    requests = [difference(r["send_started_at"], r["accepted_at"]) for r in (first, last)]
    wait_delta = difference(*waits)
    request_delta = difference(*requests)
    observed = []
    for row in (first, last):
        stamp = seen.get((row["recipient"], row["source_id"]))
        observed.append(stamp if stamp is not None and stamp <= row["queued_at"] else None)
    seen_delta = difference(*observed)
    valid_timing = all(r["send_started_at"] is not None and
                       0 < r["queued_at"] <= r["send_started_at"] <= r["accepted_at"]
                       for r in (first, last))
    if not valid_timing:
        stage = "insufficient_timing"
    elif gap < 30:
        stage = "under_30_seconds"
    elif queued >= gap * .8:
        stage = ("later_search_observation" if seen_delta is not None and
                 seen_delta >= gap * .8 else "before_enqueue")
    elif wait_delta >= gap * .8:
        stage = "after_enqueue"
    elif request_delta >= gap * .8:
        stage = "telegram_request"
    else:
        stage = "mixed"
    return {"source_id": first["source_id"], "recipients": len(rows),
            "first_accepted_at": first["accepted_at"], "last_accepted_at": last["accepted_at"],
            "acceptance_gap_seconds": gap, "queue_entry_delta_seconds": queued,
            "send_wait_delta_seconds": wait_delta, "request_delta_seconds": request_delta,
            "search_observation_delta_seconds": seen_delta,
            "first_recipient": {"seen_at": observed[0], **{k: first[k] for k in
                ("queued_at", "evaluated_at", "send_started_at", "accepted_at")}},
            "last_recipient": {"seen_at": observed[1], **{k: last[k] for k in
                ("queued_at", "evaluated_at", "send_started_at", "accepted_at")}},
            "dominant_observed_stage": stage}


def distribution(cohorts):
    values = sorted(c["acceptance_gap_seconds"] for c in cohorts)
    percentile = lambda p: values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return {"cohorts": len(values), "over_60_seconds": sum(v > 60 for v in values),
            "over_180_seconds": sum(v > 180 for v in values),
            "p50_seconds": percentile(.5), "p90_seconds": percentile(.9),
            "maximum_seconds": max(values) if values else None,
            "stages": dict(Counter(c["dominant_observed_stage"] for c in cohorts))}


def snapshot(db, now=None):
    now = time.time() if now is None else now
    # Most recently accepted shared listings, not a claim about all traffic.
    cohort_ids = list(db.scalars(select(Delivery.listing_id)
        .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
        .join(Listing, Listing.id == Delivery.listing_id)
        .where(Delivery.state == "sent", Listing.source == "auto_ria",
               DeliveryTiming.accepted_at >= now - WINDOW, DeliveryTiming.accepted_at <= now)
        .group_by(Delivery.listing_id).having(func.count(Delivery.id) > 1)
        .order_by(func.max(DeliveryTiming.accepted_at).desc(), Delivery.listing_id.desc())
        .limit(MAX_COHORTS)))
    records = list(db.execute(select(Delivery.id, Delivery.user_id, Delivery.listing_id,
        Listing.source_id, DeliveryTiming.queued_at, DeliveryTiming.evaluated_at,
        DeliveryTiming.send_started_at, DeliveryTiming.accepted_at)
        .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
        .join(Listing, Listing.id == Delivery.listing_id)
        .where(Delivery.listing_id.in_(cohort_ids), Delivery.state == "sent",
               DeliveryTiming.accepted_at >= now - WINDOW, DeliveryTiming.accepted_at <= now)
        .order_by(Delivery.listing_id, DeliveryTiming.accepted_at, Delivery.id).limit(MAX_RECORDS + 1)))
    truncated = len(records) > MAX_RECORDS
    # Omit the entire boundary cohort rather than reporting an incomplete span.
    boundary = records[MAX_RECORDS].listing_id if truncated else None
    records = [r for r in records[:MAX_RECORDS] if r.listing_id != boundary]
    sources = {r.source_id for r in records}
    seen = {(uid, sid): stamp for uid, sid, stamp in db.execute(
        select(Search.user_id, MonitorSeen.source_id, func.min(MonitorSeen.first_seen))
        .join(Search, Search.id == MonitorSeen.search_id)
        .where(MonitorSeen.source_id.in_(sources))
        .group_by(Search.user_id, MonitorSeen.source_id))}
    grouped = defaultdict(list)
    for r in records:
        grouped[r.listing_id].append({"delivery_id": r.id, "recipient": r.user_id,
            "source_id": r.source_id, "queued_at": r.queued_at, "evaluated_at": r.evaluated_at,
            "send_started_at": r.send_started_at, "accepted_at": r.accepted_at})
    cohorts = [summarize(rows, seen) for rows in grouped.values() if len(rows) > 1]
    recent = [c for c in cohorts if c["first_accepted_at"] >= now - 10800]
    worst = lambda rows: sorted(rows, key=lambda c: c["acceptance_gap_seconds"], reverse=True)[:8]
    return {"checked_at": now, "window_seconds": WINDOW, "max_cohorts": MAX_COHORTS,
            "records_truncated": truncated, "receipt_basis": "telegram_api_acceptance",
            "discovery_basis": "existing_subscription_first_seen_records",
            "sample": distribution(cohorts), "recent_three_hours": distribution(recent),
            "largest_gaps": worst(cohorts), "recent_largest_gaps": worst(recent)}


def log_once(engine):
    if os.getenv("RIA_DELIVERY_COHORT_AUDIT") != "true":
        return
    try:
        with Session(engine) as db:
            if engine.dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            result = snapshot(db)
        log.warning("Delivery cohort audit %s", json.dumps(result, sort_keys=True))
    except Exception as exc:
        # No credentials/SQL parameters/error payloads in logs. Fail open.
        log.warning("Delivery cohort audit unavailable (%s)", type(exc).__name__)
