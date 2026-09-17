"""Subscription-only discovery, durable valuation jobs and independent delivery.

Each provider request covers a frozen creation-time window. Checkpoints advance
only after its pages are saved. No full-market scan or in-memory work queue.
"""
import asyncio
import copy
import logging
import math
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Car, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search, User)
from .ria_budget import BudgetLimits
from .ria_search import RiaSearch, estimate, matches, parse_ids, quota_status
from .valuation import MAX_AGE, VERSION, is_deal

INTERVAL = 60
LEASE = 120
PAGE_SIZE = 50
WINDOW_SECONDS = 3600
INDEX_OVERLAP = 120
EVIDENCE_SECONDS = 60
log = logging.getLogger(__name__)


def initialize(engine):
    with Session(engine) as db:
        if db.get(MonitorControl, "pilot") is None:
            db.add(MonitorControl(id="pilot"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()


def source_filters(filters):
    # Alerts always require the deal threshold; this UI flag changes no source IDs.
    return Filters.model_validate(filters).model_copy(update={"onlyDeals": True})


def reset_watch(db, search_id, enabled):
    """Caller holds User lock. Changing an epoch invalidates in-flight recipients."""
    db.execute(delete(MonitorSeen).where(MonitorSeen.search_id == search_id))
    db.execute(delete(MonitorMatch).where(MonitorMatch.search_id == search_id))
    db.execute(delete(MonitorMembership).where(MonitorMembership.search_id == search_id))
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
    search = db.get(Search, search_id)
    db.add(MonitorMembership(search_id=search_id, epoch=watch.epoch,
        feed_id=source_filters(search.filters).fingerprint(), started_at=math.ceil(time.time())))


def poll_interval(groups, limits=None):
    """Scheduling target; reserve half the call rate for details/comparisons."""
    limits = limits or BudgetLimits.env()
    return max(INTERVAL, math.ceil(groups * 86400 / max(1, limits.daily // 2)),
               math.ceil(groups * 3600 / max(1, limits.hourly // 2)))


def active_members(db):
    return list(db.execute(select(Search, MonitorWatch, MonitorMembership)
        .join(User, User.id == Search.user_id)
        .join(MonitorWatch, MonitorWatch.search_id == Search.id)
        .join(MonitorMembership, MonitorMembership.search_id == Search.id)
        .where(Search.enabled.is_(True), User.ready.is_(True),
               MonitorWatch.epoch == MonitorMembership.epoch).order_by(Search.user_id, Search.id)))


def runtime_status(engine, enabled):
    with Session(engine) as db:
        row = db.get(MonitorControl, "pilot")
        healthy = bool(enabled and row and time.time() - row.heartbeat < LEASE + 60)
        groups = len({member.feed_id for _, _, member in active_members(db)})
        return {"running": healthy, "status": row.status if healthy else "offline",
                "interval_seconds": poll_interval(groups), "minimum_interval_seconds": INTERVAL,
                "active_filter_groups": groups, "shared_polling": True,
                "pending_jobs": db.scalar(select(func.count()).select_from(MonitorJob)
                    .where(MonitorJob.state == "pending")), "strategy": "new_listings_v2"}


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
                    owner=self.owner, lease_until=now + LEASE, heartbeat=now))
            db.commit()
            return result.rowcount == 1

    def owned(self, db):
        control = db.get(MonitorControl, "pilot")
        return bool(control and control.owner == self.owner and control.lease_until > time.time())

    def release(self, status):
        with Session(self.engine) as db:
            db.execute(update(MonitorControl).where(MonitorControl.id == "pilot",
                       MonitorControl.owner == self.owner).values(
                           lease_until=0, heartbeat=time.time(), status=status))
            db.commit()

    def current(self, db, sid, uid, epoch):
        # Same lock order as subscription edits and delivery: user before control.
        user = db.scalar(select(User).where(User.id == uid).with_for_update())
        row, watch = db.get(Search, sid), db.get(MonitorWatch, sid)
        if (not user or not user.ready or not row or not row.enabled or not watch
                or watch.epoch != epoch or not self.owned(db)):
            return None
        return row, watch

    def sync(self):
        with Session(self.engine) as db:
            # Legacy watches have no verified creation-time checkpoint. Upgrade
            # to a new baseline, never reinterpret old windows as new arrivals.
            legacy = db.execute(select(Search.id, Search.user_id).join(User, User.id == Search.user_id)
                .outerjoin(MonitorMembership, MonitorMembership.search_id == Search.id)
                .where(Search.enabled.is_(True), User.ready.is_(True), MonitorMembership.search_id.is_(None))
                .order_by(Search.user_id, Search.id)).all()
            for sid, uid in legacy:
                db.scalar(select(User).where(User.id == uid).with_for_update())
                search = db.get(Search, sid)
                if search and search.enabled and db.get(User, uid).ready:
                    reset_watch(db, sid, True)
            db.flush()
            for search, _, member in sorted(active_members(db), key=lambda item: item[2].started_at):
                if db.get(MonitorFeed, member.feed_id) is None:
                    db.add(MonitorFeed(id=member.feed_id, filters=source_filters(search.filters).canonical(),
                        started_at=member.started_at, cursor=member.started_at))
                    db.flush()
            db.commit()

    def prepare_window(self, feed_id):
        with Session(self.engine) as db:
            if not self.owned(db):
                return None
            feed = db.get(MonitorFeed, feed_id)
            members = [(s.id, s.user_id, w.epoch, m.started_at) for s, w, m in active_members(db)
                       if m.feed_id == feed_id]
            if not members:
                return None
            if feed.context:
                valid = {(sid, uid, epoch) for sid, uid, epoch, _ in members}
                if not any(tuple(member) in valid for part in feed.context["slices"] for member in part["members"]):
                    feed.context = {}
            if not feed.context:
                # Never poll an inactive gap; reactivation starts afresh.
                start = max(feed.started_at, feed.cursor - INDEX_OVERLAP, min(m[3] for m in members))
                target = math.floor(time.time())
                end = min(target, max(feed.cursor, start) + WINDOW_SECONDS)
                if end <= start:
                    feed.next_poll = time.time() + 1
                    db.commit()
                    return None
                # Split at activation boundaries so late joiners receive no old
                # backlog, including the overlapping lookback slice.
                boundaries = sorted({start, end, *(m[3] for m in members if start < m[3] < end)})
                slices = [{"after": a, "before": b,
                           "members": [list(m[:3]) for m in members if m[3] <= a]}
                          for a, b in zip(boundaries, boundaries[1:])]
                feed.context = {"slices": slices, "finish_at": end, "page": 0,
                                "last_ids": [], "multi": False, "added": False, "passes": 0,
                                "catchup": end < target}
                db.commit()
            return copy.deepcopy(feed.context), Filters.model_validate(feed.filters)

    def discover(self, feed_id, source, interval):
        prepared = self.prepare_window(feed_id)
        if not prepared:
            return
        context, filters = prepared
        current_slice = context["slices"][0]
        params, _ = source.parameters(filters)
        params.update(countpage=PAGE_SIZE, page=context["page"], order_by=7,
                      created_after=stamp(current_slice["after"]),
                      created_before=stamp(current_slice["before"] + 1))
        result = source.request("search", params, parse_ids, ttl=INTERVAL, force=True)
        ids, total = result["ids"], result["total"]
        if ((context["page"] and ids and ids == context["last_ids"])
                or (len(ids) < PAGE_SIZE and context["page"] * PAGE_SIZE + len(ids) < total)):
            raise RiaError("invalid_response")
        now = time.time()
        with Session(self.engine) as db:
            if not self.owned(db):
                return
            feed = db.get(MonitorFeed, feed_id)
            for sid, uid, epoch in current_slice["members"]:
                state = self.current(db, sid, uid, epoch)
                if not state:
                    continue
                _, watch = state
                for source_id in ids:
                    if db.get(MonitorSeen, (sid, source_id)) is not None:
                        continue
                    db.add(MonitorSeen(search_id=sid, source_id=source_id, epoch=epoch,
                                       state="pending", first_seen=now))
                    job = db.get(MonitorJob, source_id)
                    if job is None:
                        db.add(MonitorJob(source_id=source_id, first_seen=now))
                    elif job.state != "pending":
                        job.state, job.next_run = "pending", 0
                    context["added"] = True
                watch.initialized, watch.window, watch.checked_at = True, ids, now
            more = bool(ids) and (context["page"] + 1) * PAGE_SIZE < total
            if more:
                context.update(page=context["page"] + 1, last_ids=ids, multi=True)
            elif context["multi"] and context["added"]:
                # Re-read multi-page slices until a pass adds no IDs. Fixed time
                # bounds plus this pass recover page shifts from deleted ads.
                context.update(page=0, last_ids=[], added=False, passes=context["passes"] + 1)
            else:
                context["slices"].pop(0)
                context.update(page=0, last_ids=[], multi=False, added=False, passes=0)
            feed.checked_at = now
            if context["slices"]:
                feed.context, feed.status = context, "catching_up"
                feed.next_poll = now + (INTERVAL if context["passes"] >= 3 else 0)
                if context["passes"] >= 3:
                    feed.status = "coverage_changed"
                    context["passes"] = 0
                    feed.context = copy.deepcopy(context)
            else:
                feed.cursor, feed.context, feed.status = context["finish_at"], {}, "watching"
                feed.next_poll = now + (0 if context["catchup"] else interval)
            for _, watch, member in active_members(db):
                if member.feed_id == feed_id:
                    watch.status = feed.status if watch.initialized else "starting"
                    watch.next_poll = feed.next_poll
            db.commit()

    def interests(self, db, source_id):
        return list(db.execute(select(Search.id, Search.user_id, MonitorWatch.epoch, Search.filters)
            .join(User, User.id == Search.user_id).join(MonitorWatch, MonitorWatch.search_id == Search.id)
            .join(MonitorSeen, MonitorSeen.search_id == Search.id)
            .where(User.ready.is_(True), Search.enabled.is_(True), MonitorSeen.source_id == source_id,
                   MonitorSeen.state == "pending", MonitorSeen.epoch == MonitorWatch.epoch)
            .order_by(Search.user_id, Search.id)))

    def complete(self, source_id, evidence, outcome):
        with Session(self.engine) as db:
            if not self.owned(db):
                return
            job = db.get(MonitorJob, source_id)
            for sid, uid, epoch, _ in self.interests(db, source_id):
                state = self.current(db, sid, uid, epoch)
                if not state:
                    continue
                search, _ = state
                seen = db.get(MonitorSeen, (sid, source_id))
                fingerprint = source_filters(search.filters).fingerprint()
                # Dispatch may request a refresh for another subscription while
                # this evaluation is in flight. Resolve its filters on the next
                # work step, rather than consuming unexamined evidence.
                if evidence.get("candidate") and fingerprint not in evidence.get("filters", {}):
                    continue
                seen.state = outcome
                listing = db.scalar(select(Listing).where(Listing.source == "auto_ria", Listing.source_id == source_id))
                if listing:
                    db.execute(delete(MonitorMatch).where(MonitorMatch.search_id == sid,
                                                         MonitorMatch.listing_id == listing.id))
                candidate, rating = evidence.get("candidate"), evidence.get("rating")
                resolved = evidence.get("filters", {}).get(fingerprint)
                if (not candidate or not rating or resolved is None
                        or not matches(candidate, Filters.model_validate(search.filters), resolved)
                        or rating["valuation"] != "sample_median"
                        or not is_deal(candidate["price_usd"], rating["market"])):
                    continue
                car = Car(source="auto_ria", source_id=source_id, url=candidate["url"],
                    photo=candidate["image"], brand=candidate["brand"][:100], model=candidate["model"][:100],
                    region=candidate["region"], body=candidate["body"][:100], fuel=candidate["fuel"][:100],
                    transmission=candidate["transmission"][:100], year=candidate["year"],
                    mileage=candidate["mileage"], price=candidate["price_usd"], market=rating["market"],
                    comparables=rating["comparables"], observed_at=candidate["observed_at"],
                    valuation_evidence=rating["valuation_evidence"])
                if listing is None:
                    listing = Listing(source="auto_ria", source_id=source_id)
                    db.add(listing)
                listing.car = car.model_dump(mode="json")
                db.flush()
                db.merge(MonitorMatch(search_id=sid, listing_id=listing.id,
                                      epoch=epoch, fingerprint=search.fingerprint))
            db.flush()
            job.state = "pending" if self.interests(db, source_id) else outcome
            job.reason, job.result = "", evidence
            if job.state == "pending":
                job.next_run = 0
            db.commit()

    def evaluate(self, source_id, source):
        with Session(self.engine) as db:
            interests = self.interests(db, source_id)
            job = db.get(MonitorJob, source_id)
            evidence = copy.deepcopy(job.result)
            job.last_attempt, job.attempts = time.time(), job.attempts + 1
            db.commit()
        if not interests:
            self.complete(source_id, {}, "cancelled")
            return
        candidate = evidence.get("candidate")
        reusable = bool(candidate and 0 <= time.time() - candidate["observed_at"] <= EVIDENCE_SECONDS)
        if not reusable:
            candidate = source.car(source_id, force=True)
            evidence = {"candidate": candidate, "filters": {}}
        any_match = False
        for _, _, _, raw_filters in interests:
            filters = source_filters(raw_filters)
            fingerprint = filters.fingerprint()
            if fingerprint not in evidence["filters"]:
                _, resolved = source.parameters(filters)
                evidence["filters"][fingerprint] = resolved
            any_match |= matches(candidate, filters, evidence["filters"][fingerprint])
        rating = evidence.get("rating", {})
        proof_peers = rating.get("valuation_evidence", {}).get("peers", [])
        if (rating.get("valuation_version") != VERSION or
                any(not -30 <= time.time() - peer["observed_at"] <= MAX_AGE for peer in proof_peers)):
            evidence.pop("rating", None)
        if any_match and "rating" not in evidence:
            evidence["rating"] = estimate(candidate, source.comparisons(candidate))
        rating = evidence.get("rating", {})
        outcome = "checked" if not any_match or rating.get("valuation") == "sample_median" else "unvalued"
        self.complete(source_id, evidence, outcome)

    def defer(self, kind, key, reason, limits):
        quota = quota_status(self.engine, limits)
        wait = (quota["retry_after_seconds"] or 3600) if reason == "quota_exceeded" else (
            1 if reason in {"busy", "search_limit"} else INTERVAL)
        with Session(self.engine) as db:
            if not self.owned(db):
                return
            if kind == "discover":
                feed = db.get(MonitorFeed, key)
                feed.status, feed.next_poll = reason, time.time() + wait
                for _, watch, member in active_members(db):
                    if member.feed_id == key:
                        watch.status, watch.next_poll = reason, feed.next_poll
            else:
                job = db.get(MonitorJob, key)
                job.reason, job.next_run = reason, time.time() + wait
            db.commit()

    def tick(self):
        if not self.settings.live or not self.settings.monitor_enabled or not self.claim():
            return False
        status, worked = "idle", False
        try:
            from .telegram_setup import webhook_status
            if webhook_status(self.engine)["status"] != "configured":
                status = "telegram_unavailable"
                return False
            self.sync()
            with Session(self.engine) as db:
                groups = {member.feed_id for _, _, member in active_members(db)}
                feed = db.scalar(select(MonitorFeed).where(MonitorFeed.id.in_(groups),
                    MonitorFeed.next_poll <= time.time()).order_by(MonitorFeed.next_poll, MonitorFeed.id).limit(1))
                job = db.scalar(select(MonitorJob).where(MonitorJob.state == "pending",
                    MonitorJob.next_run <= time.time()).order_by(MonitorJob.last_attempt, MonitorJob.first_seen,
                                                               MonitorJob.source_id).limit(1))
                last = db.get(MonitorControl, "pilot").status
                kind = "evaluate" if job and (not feed or last == "discover") else "discover" if feed else None
                key = job.source_id if kind == "evaluate" else feed.id if kind else None
            if kind:
                status, worked = kind, True
                source = self.search_factory(self.engine, self.settings.auto_ria_api_key)
                acquired = False
                try:
                    source.acquire()
                    acquired = True
                    if kind == "discover":
                        self.discover(key, source, poll_interval(len(groups), source.limits))
                    else:
                        self.evaluate(key, source)
                except RiaError as exc:
                    if kind == "evaluate" and str(exc) == "listing_unavailable":
                        self.complete(key, {}, "unavailable")
                    else:
                        self.defer(kind, key, str(exc), source.limits)
                finally:
                    if acquired:
                        source.release()
        except Exception as exc:
            status = "error"
            log.error("Monitor cycle failed (%s)", type(exc).__name__)
        finally:
            self.release(status)
        return worked

    def deliver_tick(self):
        if not self.settings.live or not self.settings.monitor_enabled:
            return
        from .telegram_setup import webhook_status
        if webhook_status(self.engine)["status"] != "configured":
            return
        from .worker import TelegramSender, deliver_one, enqueue
        enqueue(self.engine)
        deliver_one(self.engine, self.settings, self.sender or TelegramSender(self.settings.bot_token))


async def run(engine, settings, stop):
    monitor = Monitor(engine, settings)

    async def loop(action):
        while not stop.is_set():
            worked = False
            try:
                worked = await asyncio.to_thread(action)
            except Exception as exc:
                log.error("Monitor unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=.25 if worked else 1)
            except TimeoutError:
                pass

    # Dispatch never waits for a slow AUTO.RIA HTTP request/valuation.
    await asyncio.gather(loop(monitor.tick), loop(monitor.deliver_tick))
