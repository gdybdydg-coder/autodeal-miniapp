"""Explicit operator read of one listing's path for the configured admin only.

No provider requests, delivery operations, user impersonation, database writes,
public endpoint, or arbitrary recipient selector. Failures never block startup.
"""
import json
import logging
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .launch import activity, listing_trace
from .models import (Delivery, DeliveryTiming, Filters, Listing, MonitorJob,
                     MonitorFeed, MonitorMembership, MonitorSeen, MonitorWatch,
                     Search, SourceProbe, User)
from .notification_diagnostic import validate_id
from .delivery_diagnostic import receipt

log = logging.getLogger(__name__)


def snapshot(db, uid, source_id):
    validate_id(source_id)
    if not source_id or not uid:
        return None
    user = db.get(User, uid)
    if user is None:
        return {"source_id": source_id, "scope": "configured_admin", "account_found": False}
    result = {"source_id": source_id, "scope": "configured_admin", "account_found": True,
              "telegram_ready": user.ready, "trace": listing_trace(db, uid, source_id)}
    from .ria_market_range import policy
    result["valuation_policy"] = policy()
    # snapshot has no runtime settings; this field depends on confirmed-only.
    result["valuation_policy"].pop("missing_range", None)
    result["activity"] = activity(db, uid)
    result["recent_accepted"] = [
        {"source_id": listing.source_id, "message_id": delivery.message_id,
         "accepted_at": timing.accepted_at}
        for delivery, timing, listing in db.execute(
            select(Delivery, DeliveryTiming, Listing)
            .join(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
            .join(Listing, Listing.id == Delivery.listing_id)
            .where(Delivery.user_id == uid, Delivery.state == "sent",
                   DeliveryTiming.accepted_at.is_not(None))
            .order_by(DeliveryTiming.accepted_at.desc(), Delivery.id.desc()).limit(5))]
    repair = db.get(SourceProbe, 'owner-photo-repair-v1-' + str(uid) + '-' + source_id)
    result['photo_repair'] = {'status': repair.status, 'result': repair.result} if repair else None
    result["searches"] = []
    for search in db.scalars(select(Search).where(Search.user_id == uid).order_by(Search.id).limit(100)):
        filters = Filters.model_validate(search.filters)
        seen = db.get(MonitorSeen, (search.id, source_id))
        watch = db.get(MonitorWatch, search.id)
        member = db.get(MonitorMembership, search.id)
        feed = db.get(MonitorFeed, member.feed_id) if member else None
        result["searches"].append({"search_id": search.id, "enabled": search.enabled,
            "filters": filters.model_dump(by_alias=True),
            "monitor": {"status": watch.status, "checked_at": watch.checked_at,
                "next_poll_at": watch.next_poll, "cursor_at": feed.cursor if feed else None}
                if watch else None,
            "first_seen_at": seen.first_seen if seen else None})
    job = db.get(MonitorJob, source_id)
    if job:
        evidence = job.result if isinstance(job.result, dict) else {}
        candidate, rating = evidence.get("candidate") or {}, evidence.get("rating") or {}
        result["job"] = {"state": job.state, "first_seen_at": job.first_seen,
            "evaluated_at": evidence.get("evaluated_at"),
            "car": {key: candidate.get(key) for key in
                    ("brand", "model", "year", "price_usd", "region", "fuel", "transmission")},
            "market": rating.get("market"), "valuation": rating.get("valuation")}
        result["job"]["valuation_reasons"] = rating.get("valuation_reasons", [])
        result["job"]["vehicle_dimensions"] = {key: candidate.get(key) for key in (
            "category_id", "brand_id", "model_id", "generation_id", "modification_id",
            "body_id", "fuel_id", "gear_id", "engine_cc", "year", "mileage")}
        # Private, read-only retained price proof; no provider refresh or raw
        # responses/similarCars/contact/VIN data. Preserve exact inputs so an
        # owner screenshot can be compared with the actual send-time decision.
        proof = rating.get('valuation_evidence')
        quote = proof.get('source_range') if isinstance(proof, dict) else None
        if isinstance(quote, dict):
            provider = quote.get('provider')
            result['job']['pricing_evidence'] = {
                'evaluated_at': evidence.get('evaluated_at'),
                'discount_percent': rating.get('discount'),
                'version': proof.get('version'),
                'source_range': {key: quote.get(key) for key in (
                    'source_id', 'basis', 'currency', 'lower_usd', 'upper_usd', 'observed_at')},
                'provider': {key: provider.get(key) for key in (
                    'average_usd', 'range_fraction', 'quantity', 'period_hours')}
                    if isinstance(provider, dict) else None,
            }
    listing = db.scalar(select(Listing).where(Listing.source == "auto_ria", Listing.source_id == source_id))
    if listing:
        result['stored_card'] = {key: listing.car.get(key) for key in (
            'brand', 'model', 'year', 'mileage', 'price', 'market', 'observed_at')}
        photo = listing.car.get('photo')
        parts = urlsplit(photo) if isinstance(photo, str) else urlsplit('')
        approved_host = (parts.hostname or '').endswith('.riastatic.com')
        result['photo'] = {'present': bool(photo), 'scheme': parts.scheme,
            'host': parts.hostname if approved_host else None,
            'path': parts.path[:1000] if approved_host else None}
    delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
        Delivery.listing_id == listing.id)) if listing else None
    result["delivery"] = None
    if delivery:
        timing = db.get(DeliveryTiming, delivery.id)
        result["delivery"] = {"state": delivery.state, "message_id": delivery.message_id,
            "attempts": receipt(db, delivery.id),
            "retry_at": delivery.retry_at,
            "timing": {key: getattr(timing, key) for key in
                ("queued_at", "discovered_at", "evaluated_at", "send_started_at", "accepted_at", "telegram_date")}
                if timing else None}
    return result


def log_once(engine, settings):
    """One read per explicit deployment; never logs credentials or account IDs."""
    if not settings.ria_owner_trace_listing_id:
        return
    if not settings.admin_telegram_id:
        log.warning("Owner listing trace unavailable: admin_not_configured")
        return
    try:
        with Session(engine) as db:
            result = snapshot(db, settings.admin_telegram_id, settings.ria_owner_trace_listing_id)
        if result:
            result['photo_repair_selected'] = settings.ria_owner_trace_listing_id in settings.ria_photo_repair_ids.split(',')
            log.warning("Owner listing trace %s", json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        log.warning("Owner listing trace unavailable (%s)", type(exc).__name__)
