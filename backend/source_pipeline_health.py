"""Read-only, aggregate source diagnostics. No outbound I/O or repair actions.

Private operator facts contain no search/recipient IDs, filters, keys or payment details.
Counters describe the current paid cohort and the stated observation window;
Telegram acceptance is never described as reading or whole-market coverage.
"""
from collections import Counter
import json
import logging
import time

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from . import api_attempt_audit, paid_source_access
from .models import (Delivery, DeliveryTiming, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorSeen, MonitorWatch, Search, SourceBudget, User)
from .ria_budget import BudgetLimits, total_cap
from .ria_search import budget_state


def completed_decisions(db, now, current_seen):
    """Bounded local audit of recorded decisions, without repricing old cars.

    Reasons partition search/car pairs; distinct-car totals are reported apart.
    Applying current filters to saved data does not prove current market value.
    """
    from .monitor import VALUED, source_filters
    from .ria_search import matches
    from .valuation import is_deal, notification_condition_allowed
    rows = list(db.execute(select(MonitorSeen, Search).join(Search, Search.id == MonitorSeen.search_id)
        .join(User, User.id == Search.user_id).join(MonitorWatch, MonitorWatch.search_id == Search.id)
        .where(*current_seen, MonitorSeen.first_seen >= now - 3600)
        .order_by(MonitorSeen.first_seen.desc(), MonitorSeen.search_id).limit(501)))
    truncated, rows = len(rows) > 500, rows[:500]
    ids = {seen.source_id for seen, _ in rows}
    jobs = {job.source_id: job for job in db.scalars(select(MonitorJob).where(MonitorJob.source_id.in_(ids)))}
    claims = {(uid, sid): state for uid, sid, state in db.execute(select(Delivery.user_id, Listing.source_id, Delivery.state)
        .join(Listing, Listing.id == Delivery.listing_id).where(Listing.source == "auto_ria", Listing.source_id.in_(ids),
            Delivery.user_id.in_({search.user_id for _, search in rows})))}
    reasons = Counter()
    for seen, search in rows:
        job = jobs.get(seen.source_id)
        evidence = job.result if job and isinstance(job.result, dict) else {}
        candidate, rating = evidence.get("candidate"), evidence.get("rating") or {}
        filters = Filters.model_validate(search.filters)
        resolved = evidence.get("filters", {}).get(source_filters(filters).fingerprint())
        claim = claims.get((search.user_id, seen.source_id))
        if claim:
            reason = "delivery_" + claim
        elif seen.state == "html_cancelled":
            reason = job.reason if job and job.reason else "supplemental_publication_retired"
        elif seen.state == "cancelled" and evidence.get("html_verified") is True:
            reason = "legacy_supplemental_publication_retired"
        elif seen.state == "pending":
            reason = "pending_evaluation"
        elif not candidate:
            reason = "saved_details_unavailable"
        elif not notification_condition_allowed(candidate):
            reason = "foreign_or_custom_exclusion"
        elif resolved is None:
            reason = "saved_filter_resolution_unavailable"
        elif not matches(candidate, filters, resolved):
            reason = "filter_mismatch"
        elif rating.get("valuation") not in VALUED or not rating.get("market"):
            reason = "valuation_unconfirmed"
        elif not is_deal(candidate["price_usd"], rating["market"], filters.minDiscount):
            reason = "below_user_discount_threshold"
        else:
            reason = "eligible_without_delivery_record"
        reasons[reason] += 1
    return {"distinct_cars": len(ids), "search_car_pairs": len(rows), "reasons": dict(reasons),
            "truncated": truncated, "basis": "saved_decisions_current_filters_no_new_source_calls"}


def snapshot(db, settings, now):
    from .monitor import active_members, interest_query, poll_interval
    members = active_members(db)
    groups = {member.feed_id for _, _, member in members}
    feeds = list(db.scalars(select(MonitorFeed).where(MonitorFeed.id.in_(groups))))
    control = db.get(MonitorControl, "pilot")
    limits = BudgetLimits.env()
    budget = db.get(SourceBudget, "auto_ria")
    oldest = min((feed.cursor for feed in feeds), default=None)
    interval = poll_interval(len(groups), limits,
        provider_pricing_enabled=settings.ria_ai_price_enabled,
        active_window_enabled=settings.ria_active_window_enabled,
        schedule_enabled=settings.ria_poll_schedule_enabled, now=now,
        continuous_paid=paid_source_access.strict(db.get_bind()))
    paid = paid_source_access.confirmed_clause(User.id, now, excluded=paid_source_access.exclusions(db.get_bind()))
    access = paid if paid_source_access.strict(db.get_bind()) else True
    current_seen = (Search.enabled.is_(True), User.ready.is_(True),
        MonitorSeen.epoch == MonitorWatch.epoch, access)
    seen = select(MonitorSeen.state, func.count()).join(Search, Search.id == MonitorSeen.search_id).join(
        User, User.id == Search.user_id).join(MonitorWatch, MonitorWatch.search_id == Search.id).where(
            *current_seen, MonitorSeen.first_seen >= now - 3600).group_by(MonitorSeen.state)
    pending = select(MonitorJob.reason, func.count()).where(MonitorJob.state == "pending", exists(
        interest_query(MonitorJob.source_id, now, **paid_source_access.query_options(db.get_bind())))).group_by(
            MonitorJob.reason)
    deliveries = (select(Delivery.state, func.count()).join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
        .join(User, User.id == Delivery.user_id).where(access, DeliveryTiming.queued_at >= now - 3600)
        .group_by(Delivery.state))
    last_client = db.scalar(select(func.max(DeliveryTiming.accepted_at)).select_from(Delivery)
        .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id).join(User, User.id == Delivery.user_id)
        .where(access, Delivery.state == "sent"))
    last_owner = db.scalar(select(func.max(DeliveryTiming.accepted_at)).select_from(Delivery)
        .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
        .where(Delivery.user_id == settings.admin_telegram_id, Delivery.state == "sent")) if settings.admin_telegram_id else None
    attempts = api_attempt_audit.summary(db, now - 3600, now)
    errors = dict(db.execute(select(api_attempt_audit.RiaApiAttempt.error_code, func.count()).where(
        api_attempt_audit.RiaApiAttempt.reserved_at >= now - 3600,
        api_attempt_audit.RiaApiAttempt.reserved_at < now,
        api_attempt_audit.RiaApiAttempt.error_code.is_not(None)).group_by(
            api_attempt_audit.RiaApiAttempt.error_code)).all())
    return {
        "observed_at": now, "window_seconds": 3600,
        "cohort": "current_confirmed_paid_clients" if paid_source_access.strict(db.get_bind()) else "legacy_test_access",
        "current_paid_clients": db.scalar(select(func.count(User.id)).where(paid)) if paid_source_access.strict(db.get_bind()) else None,
        "ready_enabled_searches": len(members), "active_filter_groups": len(groups),
        "primary": {
            "running": bool(settings.monitor_enabled and control and now - control.heartbeat < 180),
            "status": control.status if control else "unavailable",
            "interval_seconds": interval,
            "feed_states": dict(Counter(feed.status for feed in feeds)),
            "last_success_at": max((feed.checked_at for feed in feeds if feed.checked_at > 0), default=None),
            "oldest_cursor_at": oldest, "cursor_lag_seconds": max(0, round(now - oldest)) if oldest else None,
            "candidate_search_states_last_hour": dict(db.execute(seen).all()),
            "recorded_decisions_last_hour": completed_decisions(db, now, current_seen),
            "pending_jobs_by_reason": dict(db.execute(pending).all()),
        },
        "global_budget": {
            "basis": "local_reservations_not_provider_balance", "limits": {
                **limits.public(), "total": total_cap(db, limits)},
            "used": {"hourly": sum(at > now - 3600 for at in budget.calls),
                     "daily": sum(at > now - 86400 for at in budget.calls), "total": budget.total},
            "gate": budget_state(budget, now, limits, db=db),
        } if budget else {"gate": {"reason": "accounting_unavailable"}},
        "api_last_hour": {
            "transport_by_category": attempts["transport_by_category"],
            "states": attempts["states"], "errors": errors, "http_statuses": attempts["http_statuses"],
            "incomplete": attempts["incomplete"], "provider_charged_units": None,
            "transport_authorization": attempts["transport_authorization"],
            "transport_without_authorization_context": attempts["transport_without_authorization_context"],
        },
        "delivery": {"states_queued_last_hour": dict(db.execute(deliveries).all()),
                     "last_client_accepted_at": last_client, "last_owner_accepted_at": last_owner,
                     "receipt_basis": "telegram_api_acceptance"},
    }


def log_snapshot(engine, settings):
    """Private startup diagnostic only; never expose the paid cohort publicly."""
    if not paid_source_access.strict(engine) or not settings.admin_telegram_id:
        return
    logger = logging.getLogger("uvicorn.error")
    try:
        with Session(engine) as db:
            result = snapshot(db, settings, time.time())
        logger.info("Source pipeline health %s", json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        logger.error("Source pipeline health unavailable (%s)", type(exc).__name__)
