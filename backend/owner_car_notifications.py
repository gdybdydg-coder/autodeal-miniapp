"""Copies of confirmed paid-client cards; never a source/API access grant.

Reuse the durable delivery queue and Telegram transport. A saved client receipt
is required, and the snapshot is labelled historical instead of refreshing it.
No payment, search, consent, or readiness records are created or changed here.
"""
import json
import logging
import time

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from . import paid_source_access
from .models import (Car, Delivery, DeliveryTiming, Listing, MonitorMatch,
                     MonitorWatch, Search, SourceProbe, User)

log = logging.getLogger("uvicorn.error")
CONTROL = "owner-car-copies-v1"
PREFIX = "owner-car-copy-v1-"
REQUESTED_LISTING = "40514216"  # Explicitly requested recovery, once for this release.
RECOVERY = "owner-car-copy-request-40514216-v1"
CATCHUP_SECONDS = 300
MAX_BATCH = 50


def enabled(engine, settings):
    return bool(settings.owner_car_notifications_enabled and settings.admin_telegram_id
                and settings.live and settings.monitor_enabled
                and paid_source_access.strict(engine))


def marker(delivery_id):
    return PREFIX + str(delivery_id)


def _queue(db, settings, client, timing, listing, now):
    existing = db.scalar(select(Delivery).where(
        Delivery.user_id == settings.admin_telegram_id, Delivery.listing_id == listing.id))
    if existing is not None:
        return "existing"
    try:
        car = Car.model_validate(listing.car)
    except (TypeError, ValueError):
        return "invalid_snapshot"
    if car.source != "auto_ria" or car.source_id != listing.source_id:
        return "invalid_snapshot"
    pipeline = dict(car.pipeline or {})
    pipeline["owner_copy"] = {"client_accepted_at": timing.accepted_at,
                              "observed_at": car.observed_at}
    car = car.model_copy(update={"pipeline": pipeline})
    try:
        with db.begin_nested():
            copy = Delivery(user_id=settings.admin_telegram_id, listing_id=listing.id,
                            state="pending", retry_at=0)
            db.add(copy)
            db.flush()
            db.add(DeliveryTiming(delivery_id=copy.id, queued_at=now))
            db.add(SourceProbe(id=marker(copy.id), status="queued", checked_at=now,
                requests=0, result={"client_delivery_id": client.id,
                    "accepted_at": timing.accepted_at, "car": car.model_dump(mode="json")}))
    except IntegrityError:
        return "existing"
    log.info("Owner car copy queued source_id=%s basis=confirmed_client_receipt", listing.source_id)
    return "queued"


def _receipts(db, engine, owner, *, source_id=None, after=None):
    query = select(Delivery, DeliveryTiming, Listing).join(
        DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id).join(
        Listing, Listing.id == Delivery.listing_id).where(
            Delivery.state == "sent", Delivery.user_id != owner,
            Delivery.user_id.not_in(paid_source_access.exclusions(engine)),
            DeliveryTiming.accepted_at > 0, Listing.source == "auto_ria",
            paid_source_access.confirmed_clause(Delivery.user_id, DeliveryTiming.accepted_at,
                excluded=paid_source_access.exclusions(engine)))
    if source_id is not None:
        query = query.where(Listing.source_id == source_id)
    else:
        copy = aliased(Delivery)
        query = query.where(DeliveryTiming.accepted_at >= after,
            ~exists(select(copy.id).where(copy.user_id == owner,
                copy.listing_id == Listing.id).correlate(Listing)))
    return query.order_by(DeliveryTiming.accepted_at, Delivery.id).limit(MAX_BATCH)


def initialize(engine, settings):
    try:
        _initialize(engine, settings)
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        log.warning("Owner car copy initialization unavailable (%s)", type(exc).__name__)


def _initialize(engine, settings):
    """Start from this deployment; restore only the one owner-requested card."""
    if not enabled(engine, settings):
        return
    now = time.time()
    with Session(engine) as db:
        # /stop uses this same lock. It never becomes /start as a side effect.
        user = db.scalar(select(User).where(User.id == settings.admin_telegram_id).with_for_update())
        if db.get(SourceProbe, CONTROL) is None:
            db.add(SourceProbe(id=CONTROL, status="enabled", checked_at=now, requests=0,
                               result={"started_at": now}))
            db.flush()
        recovery = db.get(SourceProbe, RECOVERY)
        if recovery is None:
            rows = db.execute(_receipts(db, engine, settings.admin_telegram_id,
                                       source_id=REQUESTED_LISTING)).all()
            state = "owner_stopped" if not user or not user.ready else "no_client_receipt"
            if user and user.ready and rows:
                state = _queue(db, settings, *rows[0], now)
            db.add(SourceProbe(id=RECOVERY, status=state, checked_at=now, requests=0,
                               result={"source_id": REQUESTED_LISTING}))
        db.commit()
    log.info("Owner car copy policy enabled=true source_api_access=false old_history_replay=false")
    log_snapshot(engine, settings)


def enqueue_confirmed(engine, settings):
    try:
        _enqueue_confirmed(engine, settings)
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        # Owner observation must never interrupt the paid-client dispatcher.
        log.warning("Owner car copy queue unavailable (%s)", type(exc).__name__)


def _enqueue_confirmed(engine, settings):
    """Bounded catch-up heals a crash after client acceptance, without API calls."""
    if not enabled(engine, settings):
        return
    now = time.time()
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.id == settings.admin_telegram_id).with_for_update())
        control = db.get(SourceProbe, CONTROL)
        if not user or not user.ready or not control or control.status != "enabled":
            return
        after = max(control.result["started_at"], now - CATCHUP_SECONDS)
        for row in db.execute(_receipts(db, engine, settings.admin_telegram_id, after=after)):
            _queue(db, settings, *row, now)
        db.commit()


def payload(db, settings, delivery):
    """Only the configured owner may bypass paid-client delivery checks."""
    if not enabled(db.get_bind(), settings) or delivery.user_id != settings.admin_telegram_id:
        return None
    saved = db.get(SourceProbe, marker(delivery.id))
    if not saved or not isinstance(saved.result, dict):
        return None
    parent = db.get(Delivery, saved.result.get("client_delivery_id"))
    timing = db.get(DeliveryTiming, parent.id) if parent else None
    if (not parent or parent.state != "sent" or parent.listing_id != delivery.listing_id
            or parent.user_id == settings.admin_telegram_id
            or parent.user_id in paid_source_access.exclusions(db.get_bind())
            or not timing or not timing.accepted_at
            or timing.accepted_at != saved.result.get("accepted_at")):
        return None
    if not db.scalar(select(paid_source_access.confirmed_clause(parent.user_id, timing.accepted_at,
            excluded=paid_source_access.exclusions(db.get_bind())))):
        return None
    try:
        car = Car.model_validate(saved.result.get("car"))
    except (TypeError, ValueError):
        return None
    listing = db.get(Listing, delivery.listing_id)
    if (not listing or car.source != "auto_ria" or car.source_id != listing.source_id
            or not isinstance((car.pipeline or {}).get("owner_copy"), dict)):
        return None
    return car


def log_snapshot(engine, settings):
    """Aggregate real DB records, never recipient IDs or payment details."""
    try:
        with Session(engine) as db:
            now = time.time()
            paid = paid_source_access.confirmed_clause(User.id, now,
                excluded=paid_source_access.exclusions(engine))
            total = db.scalar(select(func.count(User.id)).where(paid))
            listing = db.scalar(select(Listing).where(Listing.source == "auto_ria",
                                                     Listing.source_id == REQUESTED_LISTING))
            matched = 0
            states = {}
            if listing:
                matched = db.scalar(select(func.count(func.distinct(User.id)))
                    .select_from(MonitorMatch).join(Search, Search.id == MonitorMatch.search_id)
                    .join(MonitorWatch, MonitorWatch.search_id == Search.id)
                    .join(User, User.id == Search.user_id).where(paid, User.ready.is_(True),
                        Search.enabled.is_(True), MonitorMatch.listing_id == listing.id,
                        MonitorMatch.epoch == MonitorWatch.epoch,
                        MonitorMatch.fingerprint == Search.fingerprint))
                states = dict(db.execute(select(Delivery.state, func.count(Delivery.id))
                    .where(Delivery.listing_id == listing.id,
                        Delivery.user_id.not_in(paid_source_access.exclusions(engine)))
                    .group_by(Delivery.state)).all())
            recovery = db.get(SourceProbe, RECOVERY)
            result = {"checked_at": now, "source_id": REQUESTED_LISTING,
                      "current_paid_clients": total, "current_matched_paid_clients": matched,
                      "saved_client_delivery_states": states,
                      "owner_recovery": recovery.status if recovery else "not_initialized",
                      "extra_source_requests": 0}
        log.info("Owner car copy verification %s", json.dumps(result, sort_keys=True))
    except Exception as exc:
        log.warning("Owner car copy verification unavailable (%s)", type(exc).__name__)
