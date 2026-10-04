"""One bounded activation of a genuinely paid owner after role exclusion.

Retain every search and historical claim. A new subscription epoch prevents
the formerly excluded owner's dormant cursors and matches from replaying cars.
Other customers' shared source checkpoints are never changed.
"""
import json
import logging
import math
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import owner_car_notifications, paid_source_access
from .models import (Delivery, DeliveryTiming, Listing, MonitorMembership,
                     MonitorWatch, Search, SourceProbe, User)

CONTROL = "paid-owner-restoration-20261004-v1"
log = logging.getLogger("uvicorn.error")


def _initialize(engine, settings, now):
    with Session(engine) as db, db.begin():
        # Same order as /stop, search edits and delivery. Concurrent initializers
        # serialize here on PostgreSQL; the unique marker also protects SQLite.
        user = db.scalar(select(User).where(User.id == settings.admin_telegram_id)
                         .with_for_update())
        previous = db.get(SourceProbe, CONTROL)
        if previous is not None:
            return {**previous.result, "status": "already_initialized"}
        boundary = math.floor(now)
        result = {"status": "not_applicable", "activation_at": boundary,
                  "rebased_searches": 0, "cancelled_unattempted_deliveries": 0,
                  "source_requests": 0, "history_preserved": True}
        paid = bool(user and paid_source_access.allowed(db, user.id, now))
        if paid and user.ready:
            searches = list(db.scalars(select(Search).where(
                Search.user_id == user.id, Search.enabled.is_(True)).order_by(Search.id)))
            for search in searches:
                watch = db.get(MonitorWatch, search.id)
                member = db.get(MonitorMembership, search.id)
                if watch is None:
                    watch = MonitorWatch(search_id=search.id)
                    db.add(watch)
                watch.epoch = uuid.uuid4().hex
                watch.initialized, watch.window = False, []
                watch.status, watch.next_poll, watch.checked_at = "starting", 0, 0
                if member is None:
                    from .monitor import source_filters
                    member = MonitorMembership(search_id=search.id,
                        feed_id=source_filters(search.filters).fingerprint())
                    db.add(member)
                member.epoch, member.started_at = watch.epoch, boundary
                result["rebased_searches"] += 1
            if searches:
                # Only a proven unattempted ordinary queue claim may be retired.
                # Never reinterpret a sent, uncertain or attempted outcome.
                deliveries = db.execute(select(Delivery, DeliveryTiming).join(
                    DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
                    .join(Listing, Listing.id == Delivery.listing_id).where(
                    Delivery.user_id == user.id, Delivery.state == "pending",
                    Listing.source == "auto_ria",
                    Delivery.message_id.is_(None), DeliveryTiming.send_started_at.is_(None),
                    DeliveryTiming.accepted_at.is_(None))).all()
                for delivery, _ in deliveries:
                    copy = db.get(SourceProbe, owner_car_notifications.marker(delivery.id))
                    if copy is not None and copy.status != "ordinary_reclaimed":
                        continue
                    delivery.state = "cancelled"
                    result["cancelled_unattempted_deliveries"] += 1
                result["status"] = "applied"
        db.add(SourceProbe(id=CONTROL, status=result["status"], checked_at=now,
                           requests=0, result=result))
        return result


def initialize(engine, settings, now=None):
    """Run before source/delivery tasks; technical failures abort startup.

    This is activation, never a payment grant. Inactive, stopped and disabled
    owner searches remain inactive; later explicit approval/start uses its
    existing activation path. The durable marker makes restarts a read only.
    """
    if not paid_source_access.strict(engine) or not settings.admin_telegram_id:
        return {"status": "disabled"}
    now = time.time() if now is None else now
    if not math.isfinite(now):
        raise ValueError("invalid_owner_restoration_clock")
    for attempt in range(5):
        try:
            result = _initialize(engine, settings, now)
            log.info("Paid owner restoration %s", json.dumps(result, sort_keys=True))
            return result
        except IntegrityError:
            if attempt == 4:
                raise
        except OperationalError as exc:
            # SQLite fixture writers can race at the unique marker. Other
            # database/access failures remain technical startup failures.
            code = getattr(exc.orig, "sqlite_errorcode", None)
            if engine.dialect.name != "sqlite" or code is None or code & 255 not in {5, 6} or attempt == 4:
                raise
            time.sleep(.01)
