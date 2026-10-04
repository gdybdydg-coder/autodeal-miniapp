"""Read-only, aggregate source diagnostics. No outbound I/O or repair actions.

Private operator facts contain no search/recipient IDs, filters, keys or payment details.
Counters describe the current paid cohort and the stated observation window;
Telegram acceptance is never described as reading or whole-market coverage.
"""
from collections import Counter
import json
import logging
import os
import time

from sqlalchemy import String, cast, exists, func, select, text
from sqlalchemy.orm import Session

from . import api_attempt_audit, paid_source_access, poll_schedule
from .billing_models import AccessEvent, Entitlement
from .manual_payment_models import PaymentAudit, PaymentRequest
from .models import (BotReply, Delivery, DeliveryTiming, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMembership, MonitorSeen, MonitorWatch, Search, SourceBudget, SourceProbe, User)
from .purchase_stats import excluded_user_ids
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
    reasons, examples = Counter(), []
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
            reason = ("native_market_range_unverified"
                      if "native_market_range_unverified" in rating.get("valuation_reasons", [])
                      else "valuation_unconfirmed")
        elif not is_deal(candidate["price_usd"], rating["market"], filters.minDiscount):
            reason = "below_user_discount_threshold"
        else:
            reason = "eligible_without_delivery_record"
        reasons[reason] += 1
        if len(examples) < 6 and not any(item["reason"] == reason for item in examples):
            examples.append({"source_id": seen.source_id, "reason": reason,
                             "discovered_at": seen.first_seen})
    return {"distinct_cars": len(ids), "search_car_pairs": len(rows), "reasons": dict(reasons),
            "examples": examples, "truncated": truncated,
            "basis": "saved_decisions_current_filters_no_new_source_calls"}


def historical_copy_clause():
    """Keep accepted old copies distinct from ordinary paid-client deliveries."""
    return exists(select(SourceProbe.id).where(
        SourceProbe.id == "owner-car-copy-v1-" + cast(Delivery.id, String),
        SourceProbe.status != "ordinary_reclaimed"))


def paid_cohort_details(db, settings, now, members):
    """Bounded private per-account evidence, without identities or filter values.

    Labels apply only to this snapshot. Historical acceptance is compared with
    saved approval/access events at that time, never today's entitlement alone.
    Reading a durable acknowledgement does not prove a phone notification.
    """
    users = list(db.scalars(select(User).where(
        paid_source_access.confirmed_clause(User.id, now)).order_by(User.id).limit(51)))
    result, client_number = [], 0
    for user in users[:50]:
        admin = user.id == settings.admin_telegram_id
        if not admin:
            client_number += 1
        purchase = db.scalar(select(PaymentRequest).where(
            PaymentRequest.user_id == user.id, PaymentRequest.state == "approved",
            PaymentRequest.amount_minor > 0, PaymentRequest.currency == "UAH", PaymentRequest.days > 0,
            PaymentRequest.updated_at <= now, PaymentRequest.expires_at > now)
            .order_by(PaymentRequest.updated_at.desc()).limit(1))
        approval = db.scalar(select(PaymentAudit).where(
            PaymentAudit.request_id == purchase.id, PaymentAudit.action == "approved",
            PaymentAudit.at <= now, PaymentAudit.after_expiry > now)
            .order_by(PaymentAudit.at.desc()).limit(1))
        entitlement = db.get(Entitlement, user.id)
        searches = list(db.scalars(select(Search).where(Search.user_id == user.id)))
        command = db.scalar(select(BotReply.command).where(BotReply.user_id == user.id)
            .order_by(BotReply.command_at.desc(), BotReply.update_id.desc()).limit(1))
        normal = ~historical_copy_clause()
        states = dict(db.execute(select(Delivery.state, func.count()).where(
            Delivery.user_id == user.id, normal).group_by(Delivery.state)).all())
        traces = []
        rows = db.execute(select(Delivery, DeliveryTiming, Listing).join(
            DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id).join(
            Listing, Listing.id == Delivery.listing_id).where(Delivery.user_id == user.id, normal)
            .order_by(DeliveryTiming.queued_at.desc(), Delivery.id.desc()).limit(3))
        for delivery, timing, listing in rows:
            accepted = timing.accepted_at
            event = db.scalar(select(AccessEvent).where(AccessEvent.user_id == user.id,
                AccessEvent.at <= accepted).order_by(AccessEvent.at.desc(), AccessEvent.id.desc())
                .limit(1)) if accepted else None
            approved_at_acceptance = bool(accepted and db.scalar(select(exists(
                select(PaymentAudit.id).join(PaymentRequest, PaymentRequest.id == PaymentAudit.request_id)
                .where(PaymentRequest.user_id == user.id, PaymentAudit.action == "approved",
                    PaymentAudit.at <= accepted, PaymentAudit.after_expiry > accepted,
                    PaymentRequest.amount_minor > 0, PaymentRequest.currency == "UAH", PaymentRequest.days > 0)))))
            traces.append({"source_id": listing.source_id, "state": delivery.state,
                "discovered_at": timing.discovered_at, "evaluated_at": timing.evaluated_at,
                "queued_at": timing.queued_at, "send_started_at": timing.send_started_at,
                "accepted_at": accepted, "owner_approval_covers_acceptance": approved_at_acceptance,
                "saved_access_event_covers_acceptance": (event.expires_at > accepted if event else None)})
        result.append({"account": "administrator" if admin else f"client_{client_number}",
            "access_basis": "approved_manual_purchase_and_current_entitlement",
            "purchase_confirmed_at": purchase.updated_at, "purchase_expires_at": purchase.expires_at,
            "operational_expires_at": entitlement.expires_at,
            "owner_approval_recorded": approval is not None,
            "approval_actor_is_configured_owner": bool(approval and approval.actor == settings.admin_telegram_id),
            "source_access_allowed": paid_source_access.allowed(db, user.id, now),
            "ready": user.ready, "latest_saved_command": command if command in {"/start", "/stop"} else None,
            "searches_total": len(searches), "searches_enabled": sum(search.enabled for search in searches),
            "planned_searches": sum(search.user_id == user.id for search, _, _ in members),
            "earliest_active_search_start": min((member.started_at for search, _, member in members
                if search.user_id == user.id), default=None),
            "ordinary_delivery_states": states, "recent_ordinary_deliveries": traces})
    return {"accounts": result, "truncated": len(users) > 50,
            "basis": "saved_payment_search_and_delivery_records_no_outbound_io"}


def access_audit(db, settings, now):
    """Compare source eligibility with durable owner approval and search state.

    Reads only: an inconsistency is evidence to investigate, never a grant or
    an automatic repair. Expired approvals and operational revocations stay
    distinct from a current approved purchase.
    """
    excluded = paid_source_access.exclusions(db.get_bind())
    paid = paid_source_access.confirmed_clause(User.id, now, excluded=excluded)
    raw_paid = paid_source_access.confirmed_clause(User.id, now)
    entitled = exists(select(Entitlement.user_id).where(
        Entitlement.user_id == User.id, Entitlement.expires_at > now))
    approval = exists(select(PaymentAudit.id).join(PaymentRequest,
        PaymentRequest.id == PaymentAudit.request_id).where(
        PaymentRequest.user_id == User.id, PaymentAudit.action == "approved",
        PaymentAudit.at <= now, PaymentAudit.after_expiry > now,
        PaymentRequest.amount_minor > 0, PaymentRequest.currency == "UAH",
        PaymentRequest.days > 0))
    proof_with_access = approval & entitled & User.id.not_in(excluded)
    configured_only = tuple(set(excluded) - set(excluded_user_ids(admin_uid=settings.admin_telegram_id)))
    checks = {
        "current_owner_approval_with_access_clients": proof_with_access,
        "current_owner_approval_without_access_clients": approval & ~entitled & User.id.not_in(excluded),
        "owner_approval_with_access_blocked_by_guard_clients": proof_with_access & ~paid,
        "current_purchase_without_owner_audit_clients": paid & ~approval,
        "current_paid_excluded_admin_clients": raw_paid & User.id.in_(excluded) & (User.id == settings.admin_telegram_id),
        "current_paid_excluded_config_only_clients": raw_paid & User.id.in_(configured_only),
        "current_paid_not_ready_clients": paid & User.ready.is_(False),
        "current_paid_without_enabled_search_clients": paid & ~exists(select(Search.id).where(
            Search.user_id == User.id, Search.enabled.is_(True))),
        "active_entitlement_without_current_purchase_clients": entitled & ~raw_paid & User.id.not_in(excluded),
    }
    result = dict(zip(checks, db.execute(select(*(func.count().filter(condition)
        for condition in checks.values())).select_from(User)).one()))
    membership = exists(select(MonitorWatch.search_id).join(MonitorMembership,
        MonitorMembership.search_id == MonitorWatch.search_id).where(
        MonitorWatch.search_id == Search.id, MonitorWatch.epoch == MonitorMembership.epoch))
    result["paid_ready_searches_without_current_membership"] = db.scalar(select(func.count(Search.id))
        .join(User, User.id == Search.user_id).where(paid, User.ready.is_(True),
            Search.enabled.is_(True), ~membership))
    result["basis"] = "saved_owner_approval_audit_access_and_membership_no_source_calls"
    return result


def snapshot(db, settings, now):
    from .monitor import active_members, interest_query, poll_interval
    from . import html_shadow, recent_publications
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
        schedule_enabled=settings.ria_poll_schedule_enabled, now=now)
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
        .where(access, Delivery.state == "sent", ~historical_copy_clause()))
    last_owner = db.scalar(select(func.max(DeliveryTiming.accepted_at)).select_from(Delivery)
        .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
        .where(Delivery.user_id == settings.admin_telegram_id, Delivery.state == "sent",
               ~historical_copy_clause())) if settings.admin_telegram_id else None
    attempts = api_attempt_audit.summary(db, now - 3600, now)
    errors = dict(db.execute(select(api_attempt_audit.RiaApiAttempt.error_code, func.count()).where(
        api_attempt_audit.RiaApiAttempt.reserved_at >= now - 3600,
        api_attempt_audit.RiaApiAttempt.reserved_at < now,
        api_attempt_audit.RiaApiAttempt.error_code.is_not(None)).group_by(
            api_attempt_audit.RiaApiAttempt.error_code)).all())
    recent = db.get(SourceProbe, recent_publications.PROBE_ID)
    shadow_key = html_shadow.key(settings)
    shadow = db.get(SourceProbe, shadow_key) if shadow_key else None
    pending_copies = db.scalar(select(func.count(Delivery.id)).join(
        DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id).where(historical_copy_clause(),
            Delivery.state.in_(("owner_pending", "pending")), Delivery.message_id.is_(None),
            DeliveryTiming.send_started_at.is_(None), DeliveryTiming.accepted_at.is_(None)))
    return {
        "observed_at": now, "window_seconds": 3600,
        "cohort": "current_confirmed_paid_clients" if paid_source_access.strict(db.get_bind()) else "legacy_test_access",
        "current_paid_clients": db.scalar(select(func.count(User.id)).where(paid)) if paid_source_access.strict(db.get_bind()) else None,
        "ready_enabled_searches": len(members), "active_filter_groups": len(groups),
        "source_access_audit": access_audit(db, settings, now) if paid_source_access.strict(db.get_bind()) else None,
        "paid_cohort_details": paid_cohort_details(db, settings, now, members) if paid_source_access.strict(db.get_bind()) else None,
        "process_configuration": {
            "release": os.getenv("RENDER_GIT_COMMIT"), "monitor_enabled": settings.monitor_enabled,
            "delivery_enabled": settings.live, "strict_paid_sources": paid_source_access.strict(db.get_bind()),
            "shared_distribution_enabled": settings.ria_shared_distribution_enabled,
            "recent_publications_enabled": settings.ria_recent_publications_enabled,
            "html_shadow_configured": bool(settings.ria_html_shadow_run_id),
            "full_scan_enabled": settings.full_scan_enabled,
            "active_window_enabled": settings.ria_active_window_enabled,
            "provider_valuation_enabled": settings.ria_ai_price_enabled,
            "valuation_selection_status": "withheld_native_range_unverified" if settings.ria_ai_price_enabled else "legacy_comparisons",
            "manual_payment_notices_enabled": settings.manual_payment_notices_enabled,
            "automobile_admin_copies_enabled": False,
        },
        "supplemental_processors": {
            "recent_publications": {"enabled": recent_publications.enabled(settings),
                "status": recent.status if recent else "not_initialized",
                "checked_at": recent.checked_at if recent else None,
                "last_success_at": recent.result.get("last_success_at") if recent else None},
            "html_shadow": {"configured": bool(shadow_key),
                "status": shadow.status if shadow else "not_initialized",
                "checked_at": shadow.checked_at if shadow else None},
        },
        "primary": {
            "running": bool(settings.monitor_enabled and control and now - control.heartbeat < 180),
            "status": control.status if control else "unavailable",
            "interval_seconds": interval,
            "schedule": poll_schedule.policy(len(groups), limits, now) if (
                settings.ria_poll_schedule_enabled and settings.ria_ai_price_enabled) else {"enabled": False},
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
                     "unattempted_admin_copies_remaining": pending_copies,
                     "receipt_basis": "telegram_api_acceptance"},
    }


def closed_windows_snapshot(db, now):
    """Closed event-clock windows; never sum overlapping rolling snapshots.

    These are global retained records, not a reconstruction of historical
    filters/access or a cohort conversion funnel. Job results are replaceable;
    their last stored evaluation is not an immutable evaluation event ledger.
    """
    end = int(now // 3600) * 3600

    def window(after):
        def inside(column):
            return (column >= after) & (column < end)
        evaluation_time = MonitorJob.result["evaluated_at"].as_float()
        valuation = MonitorJob.result["rating"]["valuation"].as_string()
        # Filter cheap timestamps before extracting historical JSON payloads.
        # Count evaluations of this discovery cohort, not unrelated old jobs.
        evaluated = dict(db.execute(select(valuation, func.count()).where(
            inside(MonitorJob.first_seen), inside(evaluation_time)).group_by(valuation)).all())
        columns = {"queued": DeliveryTiming.queued_at,
                   "send_started": DeliveryTiming.send_started_at,
                   "accepted": DeliveryTiming.accepted_at}
        event_counts = db.execute(select(*(
            func.count().filter(inside(column)) for column in columns.values()),
            func.count(func.distinct(Delivery.user_id)).filter(inside(DeliveryTiming.accepted_at)))
            .select_from(Delivery).join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
            .join(Listing, Listing.id == Delivery.listing_id)
            .where(Listing.source == "auto_ria",
                inside(DeliveryTiming.queued_at) | inside(DeliveryTiming.send_started_at)
                | inside(DeliveryTiming.accepted_at), ~historical_copy_clause())).one()
        return {
            "window": {"after_inclusive": after, "before_exclusive": end},
            "source_attempts": api_attempt_audit.summary(db, after, end),
            "candidate_ids_first_seen": db.scalar(select(func.count()).select_from(MonitorJob)
                .where(inside(MonitorJob.first_seen))),
            "evaluated_ids_by_saved_result": {key or "unrecorded": value for key, value in evaluated.items()},
            "ordinary_delivery_events": dict(zip((*columns, "distinct_accepted_recipients"), event_counts)),
            "matching_recipients_at_evaluation": None,
            "scope": "all_retained_source_jobs_and_ordinary_autoria_delivery_records",
            "historical_filter_and_access_reconstruction": False,
            "evaluation_basis": "latest_retained_job_result_not_immutable_event_history",
            "evaluation_cohort": "candidate_ids_first_seen_in_same_window",
            "receipt_basis": "telegram_api_acceptance_not_read_receipt",
        }
    return {"observed_at": now, "last_complete_hour": window(end-3600),
            "preceding_24_hours": window(end-86400)}


def log_closed_windows(engine, settings):
    """Startup-only SELECT diagnostic; no provider, Telegram or database writes."""
    if not paid_source_access.strict(engine) or not settings.admin_telegram_id:
        return
    logger = logging.getLogger("uvicorn.error")
    try:
        with Session(engine) as db:
            if db.get_bind().dialect.name == "postgresql":
                # Transaction-local timeout; restored when the session closes.
                # Diagnostic failure must never block application startup.
                db.execute(text("SELECT set_config('statement_timeout', '3000', true)"))
            result = closed_windows_snapshot(db, time.time())
        logger.info("Source completed windows %s", json.dumps(result, sort_keys=True))
    except Exception as exc:
        logger.error("Source completed windows unavailable (%s)", type(exc).__name__)


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
