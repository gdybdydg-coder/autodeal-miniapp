"""Read-only launch observations: no user impersonation, source calls or sends."""
import time
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (Delivery, DeliveryTiming, MonitorJob, MonitorSeen, Search,
                     SourceBudget, SourceProbe, TelegramTest, User)
from .valuation import DIMENSIONS, reason_category

PROBE_ID = "subscription-launch-v1"
WINDOW = 86400
UNKNOWN_CODES = {"provider_market_range_unavailable", "unverified_condition", "invalid_price", "invalid_year", "invalid_mileage",
                 "stale_details", "insufficient_comparables", "comparison_limit", "mixed_sample",
                 "missing_engine_cc", *("missing_" + name for name in DIMENSIONS)}
PEER_CODES = UNKNOWN_CODES | set(DIMENSIONS) | {"engine_cc", "year", "mileage", "duplicate_vehicle"}


def unknown_breakdown(db, conditions):
    """Only fixed reason codes and counts leave the stored valuation evidence."""
    reasons, peers, samples = Counter(), Counter(), Counter()
    query = select(MonitorJob.result["rating"]).where(*conditions,
        MonitorJob.state.in_(("unvalued", "informational")))
    for rating in db.scalars(query).yield_per(100):
        if not isinstance(rating, dict):
            continue
        reasons.update({code for code in rating.get("valuation_reasons", []) if code in UNKNOWN_CODES})
        size = rating.get("comparables")
        if type(size) is int and size >= 0:
            samples[str(min(size, 5))] += 1
        evidence = rating.get("valuation_evidence") or {}
        for rejected in evidence.get("rejected", []):
            peers.update({code for code in rejected.get("reasons", []) if code in PEER_CODES})
    return {"reasons": dict(sorted(reasons.items())), "peer_rejections": dict(sorted(peers.items())),
            "comparable_counts": dict(sorted(samples.items()))}


def source_added_at(value):
    # AUTO.RIA documents addDate, but not a timezone for naive values. Never
    # silently treat provider-local dates as UTC and invent publication latency.
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        result = stamp.timestamp()
        return result if 0 < result <= time.time() else None
    except (ValueError, OverflowError):
        return None


def initialize(engine, enabled):
    if not enabled:
        return
    with Session(engine) as db:
        if db.get(SourceProbe, PROBE_ID):
            return
        budget = db.get(SourceBudget, "auto_ria")
        db.add(SourceProbe(id=PROBE_ID, status="started", checked_at=time.time(), requests=0,
                           result={"provider_total_at_start": budget.total if budget else None}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def duration(start, end):
    return round(end - start, 3) if start is not None and end is not None and 0 < start <= end else None


def activity(db, uid=None):
    """A bounded 24h view. Private callers see only their subscription interests."""
    cutoff = time.time() - WINDOW
    subscriptions = select(Search.id)
    if uid is not None:
        subscriptions = subscriptions.where(Search.user_id == uid)
    owned_ids = select(MonitorSeen.source_id).where(MonitorSeen.search_id.in_(subscriptions))
    jobs = [MonitorJob.first_seen >= cutoff]
    if uid is not None:
        jobs.append(MonitorJob.source_id.in_(owned_ids))
    count = lambda model, *conditions: db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
    enabled = count(Search, Search.id.in_(subscriptions), Search.enabled.is_(True))
    rating = MonitorJob.result["rating"]["valuation"].as_string()
    latest_unknown = db.scalar(select(MonitorJob.result["rating"]).where(
        *jobs, MonitorJob.state.in_(("unvalued", "informational")))
        .order_by(MonitorJob.last_attempt.desc()).limit(1))
    unknown_reason = None
    if isinstance(latest_unknown, dict):
        codes = [code for code in latest_unknown.get("valuation_reasons", []) if code in UNKNOWN_CODES]
        unknown_reason = {"category": reason_category({"valuation_reasons": codes}), "codes": codes,
                          "comparables": latest_unknown.get("comparables", 0)}
    delivery_filters = [DeliveryTiming.accepted_at >= cutoff, Delivery.state == "sent"]
    if uid is not None:
        delivery_filters.append(Delivery.user_id == uid)
    sent = db.scalar(select(func.count()).select_from(Delivery).join(DeliveryTiming,
        Delivery.id == DeliveryTiming.delivery_id).where(*delivery_filters)) or 0
    recent = db.scalar(select(DeliveryTiming).join(Delivery, Delivery.id == DeliveryTiming.delivery_id)
        .where(*delivery_filters).order_by(DeliveryTiming.accepted_at.desc()).limit(1))
    last = None
    if recent:
        last = {"accepted_at": recent.accepted_at,
                "discovery_to_telegram_seconds": duration(recent.discovered_at, recent.accepted_at),
                "discovery_to_evaluation_seconds": duration(recent.discovered_at, recent.evaluated_at),
                "send_request_seconds": duration(recent.send_started_at, recent.accepted_at),
                "source_added_to_telegram_seconds": duration(recent.source_added_at, recent.accepted_at)}
    return {"window_seconds": WINDOW, "enabled_subscriptions": enabled,
            "new_listings": count(MonitorJob, *jobs),
            "pending": count(MonitorJob, *jobs, MonitorJob.state == "pending"),
            # Keep historical counters through the policy upgrade without
            # treating old median evidence as valid for a new delivery.
            "evaluated": count(MonitorJob, *jobs, rating.in_(("sample_median", "reference_median",
                "sample_lower_quartile", "reference_lower_quartile", "provider_lower_bound_adjusted"))),
            "provider_estimated": count(MonitorJob, *jobs, rating == "provider_lower_bound_adjusted"),
            "reference_estimated": count(MonitorJob, *jobs, rating.in_(("reference_median", "reference_lower_quartile"))),
            "unknown": count(MonitorJob, *jobs, MonitorJob.state.in_(("unvalued", "informational"))),
            "informational": count(MonitorJob, *jobs, MonitorJob.state == "informational"),
            "excluded_condition": count(MonitorJob, *jobs, MonitorJob.state == "excluded"),
            "latest_unknown_reason": unknown_reason,
            "unknown_breakdown": unknown_breakdown(db, jobs),
            "messages_accepted": sent, "last_delivery": last, "receipt_basis": "telegram_api_acceptance"}


def status(engine, enabled):
    with Session(engine) as db:
        row, budget = db.get(SourceProbe, PROBE_ID), db.get(SourceBudget, "auto_ria")
        baseline = row.result.get("provider_total_at_start") if row else None
        return {"enabled": enabled, "started_at": row.checked_at if row else None,
                "provider_requests_since_start": max(0, budget.total - baseline) if budget and baseline is not None else None,
                "requests_scope": "all_server_provider_calls", "activity": activity(db),
                "readiness": {
                    "telegram_ready_users": db.scalar(select(func.count()).select_from(User).where(User.ready.is_(True))),
                    "successful_tests": db.scalar(select(func.count()).select_from(TelegramTest).where(TelegramTest.state == "sent")),
                    "saved_subscriptions": db.scalar(select(func.count()).select_from(Search)),
                    "queued_deliveries": db.scalar(select(func.count()).select_from(Delivery).where(Delivery.state == "pending"))}}
