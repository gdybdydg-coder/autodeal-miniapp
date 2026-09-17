"""One-search pilot on the paid API instance. Durable state survives deploys.

Only explicitly enabled subscriptions are polled. The first window establishes a
baseline; manual searches never enter this pipeline. No catch-up blast on restart.
"""
import asyncio
import logging
import time
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Car, Delivery, Filters, Listing, MonitorControl, MonitorMatch,
                     MonitorSeen, MonitorWatch, Search, User)
from .ria_search import RiaSearch, estimate, matches, parse_ids, quota_status

INTERVAL = 60
LEASE = 120
MAX_PENDING_AGE = 300
PAUSED = {"window_gap", "unsupported_filter", "invalid_response"}
log = logging.getLogger(__name__)


def initialize(engine):
    with Session(engine) as db:
        if db.get(MonitorControl, "pilot") is None:
            db.add(MonitorControl(id="pilot"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()


def reset_watch(db, search_id, enabled):
    """Caller holds User lock. A new epoch invalidates any in-flight old poll."""
    db.execute(delete(MonitorSeen).where(MonitorSeen.search_id == search_id))
    db.execute(delete(MonitorMatch).where(MonitorMatch.search_id == search_id))
    watch = db.get(MonitorWatch, search_id)
    if not enabled:
        if watch:
            db.delete(watch)
        return
    if watch is None:
        watch = MonitorWatch(search_id=search_id)
        db.add(watch)
    watch.epoch = uuid.uuid4().hex
    watch.initialized, watch.window = False, []
    watch.status, watch.next_poll, watch.checked_at = "starting", 0, 0


def runtime_status(engine, enabled):
    with Session(engine) as db:
        row = db.get(MonitorControl, "pilot")
        healthy = bool(enabled and row and time.time() - row.heartbeat < LEASE + 60)
        return {"running": healthy, "status": row.status if healthy else "offline",
                "interval_seconds": INTERVAL, "max_active_searches": 1}


class Monitor:
    def __init__(self, engine, settings, search_factory=RiaSearch, sender=None):
        self.engine, self.settings, self.search_factory = engine, settings, search_factory
        self.owner = uuid.uuid4().hex
        self.sender = sender

    def claim(self):
        now = time.time()
        with Session(self.engine) as db:
            result = db.execute(update(MonitorControl).where(
                MonitorControl.id == "pilot", MonitorControl.lease_until < now).values(
                    owner=self.owner, lease_until=now + LEASE, heartbeat=now, status="running"))
            db.commit()
            return result.rowcount == 1

    def release(self, status):
        with Session(self.engine) as db:
            db.execute(update(MonitorControl).where(MonitorControl.id == "pilot",
                       MonitorControl.owner == self.owner).values(
                           lease_until=0, heartbeat=time.time(), status=status))
            db.commit()

    def current(self, db, sid, uid, epoch):
        # Same lock order as subscription edits and delivery: user before control.
        user = db.scalar(select(User).where(User.id == uid).with_for_update())
        control = db.get(MonitorControl, "pilot")
        row, watch = db.get(Search, sid), db.get(MonitorWatch, sid)
        if (not user or not user.ready or not row or not row.enabled or not watch
                or watch.epoch != epoch or not control or control.owner != self.owner
                or control.lease_until <= time.time()):
            return None
        return row, watch

    def window(self, sid, uid, epoch, ids, total):
        now = time.time()
        with Session(self.engine) as db:
            state = self.current(db, sid, uid, epoch)
            if not state:
                return False
            _, watch = state
            # A full page with no overlap means coverage is unknown. Require a
            # narrower filter/re-enable instead of claiming complete monitoring.
            if watch.initialized and watch.window and total >= 50 and not set(ids) & set(watch.window):
                watch.status, watch.checked_at = "window_gap", now
                db.commit()
                return False
            baseline = not watch.initialized
            for source_id in ids:
                if db.get(MonitorSeen, (sid, source_id)) is None:
                    db.add(MonitorSeen(search_id=sid, source_id=source_id, epoch=epoch,
                                       state="baseline" if baseline else "pending", first_seen=now))
            watch.window, watch.initialized = ids, True
            watch.checked_at, watch.next_poll = now, now + INTERVAL
            watch.status = "watching"
            db.commit()
            return not baseline

    def complete(self, sid, uid, epoch, source_id, car=None, outcome="checked"):
        with Session(self.engine) as db:
            state = self.current(db, sid, uid, epoch)
            if not state:
                return
            search, _ = state
            seen = db.get(MonitorSeen, (sid, source_id))
            if not seen or seen.epoch != epoch or seen.state != "pending":
                return
            seen.state = outcome
            if car is not None and time.time() - seen.first_seen <= MAX_PENDING_AGE:
                listing = db.scalar(select(Listing).where(Listing.source == "auto_ria", Listing.source_id == source_id))
                if listing is None:
                    listing = Listing(source="auto_ria", source_id=source_id)
                    db.add(listing)
                listing.car = car.model_dump(mode="json")
                db.flush()
                # Evidence of this exact subscription's new, post-filtered match.
                db.merge(MonitorMatch(search_id=sid, listing_id=listing.id,
                                      epoch=epoch, fingerprint=search.fingerprint))
            db.commit()

    def defer(self, sid, uid, epoch, reason):
        quota = quota_status(self.engine)
        wait = quota["retry_after_seconds"] if reason == "quota_exceeded" else INTERVAL
        with Session(self.engine) as db:
            state = self.current(db, sid, uid, epoch)
            if state:
                _, watch = state
                watch.status = reason
                watch.next_poll = time.time() + max(INTERVAL, wait or 3600)
                db.commit()

    def poll(self, sid, uid, epoch, filters):
        source = self.search_factory(self.engine, self.settings.auto_ria_api_key)
        acquired = False
        try:
            source.acquire()
            acquired = True
            params, ids = source.parameters(filters)
            params["countpage"] = 50
            results = source.request("search", params, parse_ids, ttl=INTERVAL, force=True)
            if not self.window(sid, uid, epoch, results["ids"], results["total"]):
                return
            with Session(self.engine) as db:
                pending = [(row.source_id, row.first_seen) for row in db.scalars(
                    select(MonitorSeen).where(MonitorSeen.search_id == sid,
                        MonitorSeen.epoch == epoch, MonitorSeen.state == "pending")
                    .order_by(MonitorSeen.first_seen, MonitorSeen.source_id))]
            checked = 0
            for source_id, first_seen in pending:
                if time.time() - first_seen > MAX_PENDING_AGE:
                    self.complete(sid, uid, epoch, source_id, outcome="expired")
                    continue
                if checked >= 3:
                    break
                checked += 1
                try:
                    candidate = source.car(source_id, force=True)
                    car = None
                    if matches(candidate, filters, ids):
                        rating = estimate(candidate, source.comparisons(candidate))
                        if (rating["valuation"] == "sample_median"
                                and candidate["price_usd"] <= rating["market"] * .85):
                            car = Car(source="auto_ria", source_id=source_id, url=candidate["url"],
                                      photo=candidate["image"], brand=candidate["brand"][:100], model=candidate["model"][:100],
                                      region=candidate["region"], body=candidate["body"][:100], fuel=candidate["fuel"][:100],
                                      transmission=candidate["transmission"][:100], year=candidate["year"],
                                      mileage=candidate["mileage"], price=candidate["price_usd"],
                                      market=rating["market"], comparables=rating["comparables"],
                                      observed_at=candidate["observed_at"])
                    self.complete(sid, uid, epoch, source_id, car)
                except RiaError as exc:
                    if str(exc) not in {"listing_unavailable", "invalid_response"}:
                        raise
                    self.complete(sid, uid, epoch, source_id, outcome="unavailable")
        except RiaError as exc:
            self.defer(sid, uid, epoch, str(exc))
        finally:
            if acquired:
                source.release()

    def tick(self):
        if not self.settings.live or not self.settings.monitor_enabled or not self.claim():
            return
        status = "idle"
        try:
            from .telegram_setup import webhook_status
            if webhook_status(self.engine)["status"] != "configured":
                status = "telegram_unavailable"
                return
            with Session(self.engine) as db:
                active = list(db.scalars(select(Search).join(User, User.id == Search.user_id)
                                        .where(Search.enabled.is_(True), User.ready.is_(True))))
                if len(active) > 1:
                    # Fail closed if a manual database edit bypasses the pilot cap.
                    status = "capacity_exceeded"
                    return
                work = None
                if active:
                    row = active[0]
                    watch = db.get(MonitorWatch, row.id)
                    if watch and watch.status not in PAUSED and watch.next_poll <= time.time():
                        work = (row.id, row.user_id, watch.epoch, Filters.model_validate(row.filters))
                    status = watch.status if watch else "missing_watch"
            if work:
                self.poll(*work)
            from .worker import TelegramSender, deliver_one, enqueue
            enqueue(self.engine)
            # One attempt per tick; uncertain outcomes are never retried blindly.
            deliver_one(self.engine, self.settings, self.sender or TelegramSender(self.settings.bot_token))
        except Exception as exc:
            status = "error"
            log.error("Monitor cycle failed (%s)", type(exc).__name__)
        finally:
            self.release(status)


async def run(engine, settings, stop):
    monitor = Monitor(engine, settings)
    while not stop.is_set():
        try:
            await asyncio.to_thread(monitor.tick)
        except Exception as exc:
            # Includes transient DB failure while claiming/releasing the lease.
            log.error("Monitor unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass
