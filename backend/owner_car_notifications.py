"""Retire obsolete administrator car copies without deleting their history.

There is no copy producer, recovery sender, saved-card payload or access bypass.
The owner uses the ordinary paid search and delivery queue. Never-attempted copy
reservations may be reclaimed only by a current ordinary matching candidate.
"""
import logging
import time
from threading import Lock
from weakref import WeakKeyDictionary

from sqlalchemy import String, cast, exists, literal, select, update
from sqlalchemy.orm import Session

from .models import Delivery, DeliveryTiming, SourceProbe

log = logging.getLogger("uvicorn.error")
CONTROL = "owner-car-copies-v1"
PREFIX = "owner-car-copy-v1-"
QUEUE_STATE = "owner_pending"  # Historical state; no worker claims it.
_cleanup_lock = Lock()
_cleanup_checks = WeakKeyDictionary()


def marker(delivery_id):
    return PREFIX + str(delivery_id)


def ordinary_delivery_clause():
    """Legacy copy claims are never transport work, even in an old pending state."""
    return ~exists(select(SourceProbe.id).where(
        SourceProbe.id == literal(PREFIX) + cast(Delivery.id, String),
        SourceProbe.status != "ordinary_reclaimed"))


def reusable_clause():
    """A retired reservation cannot become a retry of an attempted transport."""
    unused = exists(select(DeliveryTiming.delivery_id).where(
        DeliveryTiming.delivery_id == Delivery.id,
        DeliveryTiming.send_started_at.is_(None), DeliveryTiming.accepted_at.is_(None)))
    retired = exists(select(SourceProbe.id).where(
        SourceProbe.id == literal(PREFIX) + cast(Delivery.id, String),
        SourceProbe.status == "retired_unattempted"))
    return Delivery.state == "cancelled", Delivery.message_id.is_(None), unused, retired


def reclaim(db, delivery, now):
    """Called only after the ordinary paid, fresh-car and matching-search checks."""
    changed = db.execute(update(Delivery).where(
        Delivery.id == delivery.id, *reusable_clause()).values(state="pending", retry_at=0))
    if changed.rowcount != 1:
        return False
    db.execute(update(SourceProbe).where(SourceProbe.id == marker(delivery.id),
        SourceProbe.status == "retired_unattempted").values(
            status="ordinary_reclaimed", checked_at=now))
    return True


def retire_if_due(engine, settings):
    """Catch predecessor queue writes during a deploy; never produce a copy."""
    checked_at = time.monotonic()
    with _cleanup_lock:
        if checked_at - _cleanup_checks.get(engine, float("-inf")) < 60:
            return
        _cleanup_checks[engine] = checked_at
    try:
        initialize(engine, settings, report=False)
    except Exception as exc:
        # The ordinary dispatcher still applies its paid/search checks. Copy
        # states are unclaimable even when this read-only-history cleanup waits.
        log.warning("Administrator car copy retirement deferred (%s)", type(exc).__name__)


def initialize(engine, settings, *, report=True):
    """One deployment cleanup; settings can never re-enable automobile copies."""
    now = time.time()
    retired = 0
    with Session(engine) as db:
        control = db.get(SourceProbe, CONTROL)
        if control is None:
            control = SourceProbe(id=CONTROL, requests=0, result={})
            db.add(control)
        control.status = "disabled"
        control.checked_at = now
        rows = db.execute(select(SourceProbe, Delivery, DeliveryTiming)
            .join(Delivery, SourceProbe.id == literal(PREFIX) + cast(Delivery.id, String))
            .outerjoin(DeliveryTiming, DeliveryTiming.delivery_id == Delivery.id)
            .where(SourceProbe.id.like(PREFIX + "%"),
                   SourceProbe.status.not_in(("ordinary_reclaimed", "retired_unattempted", "retired_history")))
            .with_for_update(of=(Delivery, SourceProbe))).all()
        for saved, delivery, timing in rows:
            never_attempted = (delivery.state in {"pending", QUEUE_STATE, "cancelled"}
                and delivery.message_id is None and timing is not None
                and timing.send_started_at is None and timing.accepted_at is None)
            if never_attempted:
                delivery.state = "cancelled"
                retired += 1
            # Preserve the complete snapshot while making predecessor payload
            # parsing fail closed during a deployment overlap.
            saved.result = {"retired_at": now, "legacy_copy": saved.result}
            saved.status = "retired_unattempted" if never_attempted else "retired_history"
            saved.checked_at = now
        db.commit()
    if report or retired:
        log.info("Administrator car copies disabled retired_unattempted=%s history_preserved=true", retired)
