"""Subscription-only discovery, durable valuation jobs and independent delivery.

Each provider request covers a frozen publication-time window. Checkpoints advance
only after its pages are saved. No full-market scan or in-memory work queue.
"""
import asyncio
import copy
import logging
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Car, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search, User)
from .ria_budget import BudgetLimits
from .ria_search import RiaSearch, estimate, matches, parse_ids, quota_status
from .valuation import MAX_AGE, VERSION, PeerBatch, is_deal, price_only_evidence
from . import active_window
from .reference_valuation import VERSION as REFERENCE_VERSION, VALUED, notification_estimate

INTERVAL = 60
LEASE = 120
PAGE_SIZE = 50
WINDOW_SECONDS = 3600
INDEX_OVERLAP = 600
EVIDENCE_SECONDS = 60
OPTIONAL_DETAIL_FIELDS = ("body_id", "fuel_id", "gear_id", "mileage")
NOTIFICATION_VERSION = "informational-v2"
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
    # Percentages filter shared valuations, never provider discovery or pricing.
    return Filters.model_validate(filters).model_copy(update={"onlyDeals": True, "minDiscount": 15})


def incomplete_optional_details(candidate):
    return any(candidate.get(field) is None for field in OPTIONAL_DETAIL_FIELDS)


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


def runtime_status(engine, enabled, uid=None, *, active_window_enabled=False):
    with Session(engine) as db:
        row = db.get(MonitorControl, "pilot")
        healthy = bool(enabled and row and time.time() - row.heartbeat < LEASE + 60)
        members = active_members(db)
        groups = len({member.feed_id for _, _, member in members})
        own_groups = {member.feed_id for search, _, member in members
                      if uid is None or search.user_id == uid}
        feeds = list(db.scalars(select(MonitorFeed).where(MonitorFeed.id.in_(own_groups))))
        states = Counter(feed.status for feed in feeds)
        states["starting"] += len(own_groups) - len(feeds)
        states = {state: count for state, count in states.items() if count}
        successful = [feed.checked_at for feed in feeds if feed.checked_at > 0]
        oldest_cursor = min((feed.cursor for feed in feeds), default=None)
        errors = set(states) - {"starting", "watching", "catching_up", "busy", "search_limit"}
        lag = max(0, round(time.time() - oldest_cursor)) if oldest_cursor is not None else None
        discovery = {"state_counts": states, "successful_groups": len(successful),
                     "last_success_at": max(successful, default=None),
                     "oldest_cursor_at": oldest_cursor, "lag_seconds": lag,
                     "needs_attention": bool(errors or (lag is not None and lag > max(300, poll_interval(groups) * 3)))}
        return {"running": healthy, "status": row.status if healthy else "offline",
                "interval_seconds": poll_interval(groups), "minimum_interval_seconds": INTERVAL,
                "active_filter_groups": groups, "shared_polling": True,
                "pending_jobs": db.scalar(select(func.count()).select_from(MonitorJob)
                    .where(MonitorJob.state == "pending")),
                "strategy": "publications_with_bounded_active_window" if active_window_enabled else "new_publications_v3",
                "index_overlap_seconds": INDEX_OVERLAP,
                "active_window": active_window.status(db, own_groups, active_window_enabled),
                "discovery": discovery}


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
            if not self.settings.ria_active_window_enabled:
                # Retire only queued supplemental work. Keep publication cursors,
                # subscription epochs and sent/uncertain delivery records intact.
                retired_ids = select(MonitorJob.source_id).where(MonitorJob.state == "pending",
                    MonitorJob.result["discovery_kind"].as_string() == active_window.KIND)
                db.execute(update(MonitorSeen).where(MonitorSeen.source_id.in_(retired_ids),
                    MonitorSeen.state == "pending").values(state="cancelled"))
                db.execute(update(MonitorJob).where(MonitorJob.source_id.in_(retired_ids)).values(
                    state="cancelled", reason="active_window_disabled"))
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
            # Reconsider only previously discovered, still-active interests when
            # the valuation policy changes. Never replay a catalog or /stop epoch.
            outdated = db.execute(select(MonitorJob, MonitorSeen)
                .join(MonitorSeen, MonitorSeen.source_id == MonitorJob.source_id)
                .join(Search, Search.id == MonitorSeen.search_id)
                .join(User, User.id == Search.user_id)
                .join(MonitorWatch, MonitorWatch.search_id == Search.id)
                .where(MonitorJob.state == "unvalued", MonitorSeen.state == "unvalued",
                       Search.enabled.is_(True), User.ready.is_(True),
                       MonitorSeen.epoch == MonitorWatch.epoch,
                       MonitorJob.first_seen >= time.time() - 86400)).all()
            for job, seen in outdated:
                if (job.result.get("rating", {}).get("valuation_version") != VERSION
                        or job.result.get("notification_version") != NOTIFICATION_VERSION):
                    origin = job.result.get("discovery_kind", "new_publication")
                    job.state, job.next_run, job.result = "pending", 0, {"discovery_kind": origin}
                    seen.state = "pending"
            if self.settings.ria_active_window_enabled:
                active_window.sync(db, {member.feed_id for _, _, member in active_members(db)})
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
            if feed.context and feed.context.get("clock") != "published":
                # Keep the checkpoint and recipient epochs. Only discard an
                # unfinished old-clock page sequence; never restart the catalog.
                feed.context = {}
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
                                "clock": "published",
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
        params, _ = source.discovery_parameters(filters)
        params.update(countpage=PAGE_SIZE, page=context["page"], order_by=7,
                      published_after=stamp(current_slice["after"]),
                      published_before=stamp(current_slice["before"] + 1))
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
            evidence = {**evidence, "discovered_at": job.first_seen, "evaluated_at": time.time(),
                        "notification_version": NOTIFICATION_VERSION}
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
                filters = Filters.model_validate(search.filters)
                partial = evidence.get("informational_notification") is True
                priced_deal = bool(rating and rating.get("valuation") in VALUED
                    and is_deal(candidate["price_usd"], rating["market"], filters.minDiscount))
                if (not candidate or candidate.get("condition_exclusions") or resolved is None
                        or not matches(candidate, filters, resolved)
                        or not (partial or priced_deal)):
                    continue
                proof = (price_only_evidence(candidate, evidence["evaluated_at"], evidence["uncertainty_reasons"])
                         if partial else rating["valuation_evidence"])
                car = Car(source="auto_ria", source_id=source_id, url=candidate["url"],
                    photo=candidate["image"], brand=candidate["brand"][:100], model=candidate["model"][:100],
                    region=candidate["region"], body=candidate["body"][:100], fuel=candidate["fuel"][:100],
                    transmission=candidate["transmission"][:100], year=candidate["year"],
                    mileage=candidate["mileage"], price=candidate["price_usd"],
                    market=None if partial else rating["market"],
                    comparables=0 if partial else rating["comparables"], observed_at=candidate["observed_at"],
                    valuation_evidence=proof,
                    pipeline={"discovered_at": job.first_seen, "evaluated_at": evidence["evaluated_at"],
                              "discovery_kind": evidence.get("discovery_kind", "new_publication"),
                              "source_added_at": candidate.get("source_added_at")})
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
        reusable = bool(candidate and "condition_exclusions" in candidate
                        and 0 <= time.time() - candidate["observed_at"] <= EVIDENCE_SECONDS)
        if not reusable:
            candidate = source.car(source_id, force=True)
            evidence = {"candidate": candidate, "filters": {},
                        "discovery_kind": evidence.get("discovery_kind", "new_publication")}
        any_match = False
        for _, _, _, raw_filters in interests:
            filters = source_filters(raw_filters)
            fingerprint = filters.fingerprint()
            if fingerprint not in evidence["filters"]:
                _, resolved = source.parameters(filters)
                evidence["filters"][fingerprint] = resolved
            any_match |= matches(candidate, filters, evidence["filters"][fingerprint])
        partial = incomplete_optional_details(candidate)
        excluded = bool(candidate.get("condition_exclusions"))
        rating = evidence.get("rating", {})
        proof_peers = rating.get("valuation_evidence", {}).get("peers", [])
        if (rating.get("valuation_version") not in {VERSION, REFERENCE_VERSION} or
                any(not -30 <= time.time() - peer["observed_at"] <= MAX_AGE for peer in proof_peers)):
            evidence.pop("rating", None)
        if any_match and not excluded and "rating" not in evidence:
            try:
                peers = source.notification_comparisons(candidate)
            except RiaError as exc:
                if str(exc) not in {"search_limit", "quota_exceeded", "connection_error", "upstream_error"}:
                    raise
                # The listing price was already freshly retrieved. A bounded
                # peer-search failure must not discard that verified candidate.
                peers = PeerBatch(limited=True, unavailable=str(exc))
            evidence["rating"] = notification_estimate(candidate, peers)
        rating = evidence.get("rating", {})
        uncertainty = []
        if any_match and not excluded and rating.get("valuation") not in VALUED:
            if partial:
                uncertainty.append("incomplete_details")
            elif rating.get("valuation") in {"insufficient_data", "mixed_sample"}:
                for reason in ("insufficient_comparables", "mixed_sample", "unverified_condition"):
                    if reason in rating.get("valuation_reasons", []):
                        uncertainty.append(reason)
                if not uncertainty:
                    uncertainty.append("missing_valuation_details")
            if uncertainty and not candidate.get("comparable_condition"):
                uncertainty.append("unverified_condition")
        evidence["uncertainty_reasons"] = sorted(set(uncertainty))
        evidence["informational_notification"] = bool(uncertainty)
        outcome = ("excluded" if excluded else "informational" if uncertainty else "checked"
                   if not any_match or rating.get("valuation") in VALUED else "unvalued")
        self.complete(source_id, evidence, outcome)

    def defer(self, kind, key, reason, limits):
        quota = quota_status(self.engine, limits)
        wait = (quota["retry_after_seconds"] or 3600) if reason == "quota_exceeded" else (
            1 if reason in {"busy", "search_limit"} else INTERVAL)
        with Session(self.engine) as db:
            if not self.owned(db):
                return
            if kind == active_window.KIND:
                active_window.defer(db, key, reason)
            elif kind == "discover":
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
            if self.settings.ria_recovery_listing_id:
                from .notification_recovery import recover_once, report
                recover_once(self, self.settings.ria_recovery_listing_id)
                report(self.engine, self.settings.ria_recovery_listing_id)
            with Session(self.engine) as db:
                groups = {member.feed_id for _, _, member in active_members(db)}
                feed = db.scalar(select(MonitorFeed).where(MonitorFeed.id.in_(groups),
                    MonitorFeed.next_poll <= time.time()).order_by(MonitorFeed.next_poll, MonitorFeed.id).limit(1))
                supplemental = func.coalesce(MonitorJob.result["discovery_kind"].as_string(), "") == active_window.KIND
                job_query = select(MonitorJob).where(MonitorJob.state == "pending", MonitorJob.next_run <= time.time())
                if not self.settings.ria_active_window_enabled or not active_window.budget_available(db):
                    job_query = job_query.where(~supplemental)
                job = db.scalar(job_query.order_by(case((supplemental, 1), else_=0),
                    MonitorJob.last_attempt, MonitorJob.first_seen, MonitorJob.source_id).limit(1))
                last = db.get(MonitorControl, "pilot").status
                kind = "evaluate" if job and (not feed or last == "discover") else "discover" if feed else None
                key = job.source_id if kind == "evaluate" else feed.id if kind else None
                if kind is None and self.settings.ria_active_window_enabled:
                    extra = active_window.due(db, groups)
                    if extra:
                        if active_window.budget_available(db):
                            kind, key = active_window.KIND, extra.feed_id
                        else:
                            active_window.defer(db, extra.feed_id, "reserved_for_new_publications")
                            db.commit()
            if kind:
                status, worked = kind, True
                source = self.search_factory(self.engine, self.settings.auto_ria_api_key)
                supplemental_work = kind == active_window.KIND or (kind == "evaluate"
                        and job.result.get("discovery_kind") == active_window.KIND)
                if supplemental_work:
                    source.request_limit = active_window.CALL_RESERVE
                acquired = False
                try:
                    source.acquire()
                    acquired = True
                    if supplemental_work:
                        with Session(self.engine) as db:
                            if not active_window.budget_available(db, source.limits):
                                raise RiaError("reserved_for_new_publications")
                    if kind == active_window.KIND:
                        active_window.discover(self, key, source)
                    elif kind == "discover":
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
        enqueue(self.engine, allow_active_window=self.settings.ria_active_window_enabled)
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
