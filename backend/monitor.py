"""Subscription-only discovery, durable valuation jobs and independent delivery.

Each provider request covers a frozen publication-time window. Checkpoints advance
only after its pages are saved. No full-market scan or in-memory work queue.
"""
import asyncio
import copy
import logging
import math
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import case, delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Car, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search, User)
from .ria_budget import BudgetLimits
from .ria_search import RiaSearch, estimate, matches, parse_ids, quota_status
from .valuation import (MAX_AGE, VERSION, PeerBatch, is_deal, price_only_evidence,
                        notification_condition_allowed, repair_notices)
from . import active_window, ria_market_range, poll_schedule
from .reference_valuation import VERSION as REFERENCE_VERSION, VALUED as PEER_VALUED, notification_estimate

VALUED = {*PEER_VALUED, "provider_lower_bound_adjusted"}

INTERVAL = 60
FAST_POLL_INTERVAL = 30
PARALLEL_TASKS = 4
LEASE = 120
PAGE_SIZE = 50
WINDOW_SECONDS = 3600
INDEX_OVERLAP = 600
EVIDENCE_SECONDS = 60
OPTIONAL_DETAIL_FIELDS = ("body_id", "fuel_id", "gear_id", "mileage")
NOTIFICATION_VERSION = "informational-v3"
log = logging.getLogger(__name__)
batch_log = logging.getLogger("autodeal.monitor_batches")
batch_log.setLevel(logging.INFO)
batch_log.propagate = False
if not batch_log.handlers:
    batch_log.addHandler(logging.StreamHandler())


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


def poll_interval(groups, limits=None, *, provider_pricing_enabled=False, active_window_enabled=False,
                  schedule_enabled=False, now=None):
    """Pace distinct searches while reserving capacity for fresh valuations.

    Paid listing-specific pricing needs a detail read and a quote, rather than
    a peer search. In publication-only mode discovery can use up to 3/4 of the
    planned rate. Legacy comparison/supplemental modes retain their 1/2 share.
    Every actual request still passes the shared hard quota gate.
    """
    limits = limits or BudgetLimits.env()
    fast = provider_pricing_enabled and not active_window_enabled
    if provider_pricing_enabled and schedule_enabled:
        schedule = poll_schedule.policy(groups, limits, now)
        if schedule["enabled"]:
            return schedule["interval_seconds"]
    share = 3 if fast else 2
    return max(FAST_POLL_INTERVAL if fast else INTERVAL,
               math.ceil(groups * 86400 / max(1, limits.daily * share // 4)),
               math.ceil(groups * 3600 / max(1, limits.hourly * share // 4)))


def active_members(db):
    return list(db.execute(select(Search, MonitorWatch, MonitorMembership)
        .join(User, User.id == Search.user_id)
        .join(MonitorWatch, MonitorWatch.search_id == Search.id)
        .join(MonitorMembership, MonitorMembership.search_id == Search.id)
        .where(Search.enabled.is_(True), User.ready.is_(True),
               MonitorWatch.epoch == MonitorMembership.epoch).order_by(Search.user_id, Search.id)))


def runtime_status(engine, enabled, uid=None, *, active_window_enabled=False, provider_pricing_enabled=False,
                   schedule_enabled=False, active_window_include_initial=False):
    with Session(engine) as db:
        row = db.get(MonitorControl, "pilot")
        healthy = bool(enabled and row and time.time() - row.heartbeat < LEASE + 60)
        members = active_members(db)
        groups = len({member.feed_id for _, _, member in members})
        interval = poll_interval(groups, provider_pricing_enabled=provider_pricing_enabled,
                                 active_window_enabled=active_window_enabled, schedule_enabled=schedule_enabled)
        schedule = (poll_schedule.policy(groups, BudgetLimits.env())
                    if schedule_enabled and provider_pricing_enabled
                    else {"enabled": False})
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
                     "needs_attention": bool(errors or (lag is not None and lag > max(300, interval * 3)))}
        return {"running": healthy, "status": row.status if healthy else "offline",
                "interval_seconds": interval,
                "schedule": schedule,
                "minimum_interval_seconds": (FAST_POLL_INTERVAL
                    if provider_pricing_enabled and not active_window_enabled else INTERVAL),
                "active_filter_groups": groups, "shared_polling": True,
                "pending_jobs": db.scalar(select(func.count()).select_from(MonitorJob)
                    .where(MonitorJob.state == "pending")),
                "source_parallelism": PARALLEL_TASKS if provider_pricing_enabled else 1,
                "strategy": "publications_with_bounded_active_window" if active_window_enabled else "new_publications_v3",
                "index_overlap_seconds": INDEX_OVERLAP,
                "active_window": active_window.status(db, own_groups, active_window_enabled,
                                                     active_window_include_initial),
                "discovery": discovery}


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Monitor:
    def __init__(self, engine, settings, search_factory=RiaSearch, sender=None):
        self.engine, self.settings, self.search_factory = engine, settings, search_factory
        self.owner = uuid.uuid4().hex
        self.sender = sender
        # Keep state transitions ordered while independent HTTP calls overlap.
        # Sessions are never shared between threads or held across network waits.
        self._state_lock = threading.RLock()
        self._schedule_key = None

    def reschedule(self, db, groups):
        """Change only ordinary healthy waits; retain cursors, retries and claims."""
        if not self.owned(db):
            return
        if not (self.settings.ria_poll_schedule_enabled and self.settings.ria_ai_price_enabled):
            return
        schedule = poll_schedule.policy(len(groups), BudgetLimits.env())
        if not schedule["enabled"]:
            return
        interval = schedule["interval_seconds"]
        key = (frozenset(groups), schedule["active_period"], interval)
        if key == self._schedule_key:
            return
        changed = {}
        for feed in db.scalars(select(MonitorFeed).where(MonitorFeed.id.in_(groups),
                                                        MonitorFeed.status == "watching")):
            # Immediate catch-up pages and error/quota backoffs are not timers
            # created by the ordinary schedule and must never be rewritten.
            if not feed.context and feed.checked_at > 0 and feed.next_poll > feed.checked_at:
                feed.next_poll = feed.checked_at + interval
                changed[feed.id] = feed.next_poll
        for _, watch, member in active_members(db):
            if member.feed_id in changed:
                watch.next_poll = changed[member.feed_id]
        db.commit()
        self._schedule_key = key

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
                origin = func.coalesce(MonitorJob.result["discovery_kind"].as_string(), "")
                # Older delivery refreshes discarded job provenance. Recover it
                # from the saved card only when no primary confirmation exists.
                legacy_refresh = (origin == "") & exists(select(Listing.id).where(
                    Listing.source == "auto_ria", Listing.source_id == MonitorJob.source_id,
                    Listing.car["pipeline"]["discovery_kind"].as_string() == active_window.KIND))
                retired_ids = select(MonitorJob.source_id).where(MonitorJob.state == "pending",
                    (origin == active_window.KIND) | legacy_refresh)
                db.execute(update(MonitorSeen).where(MonitorSeen.source_id.in_(retired_ids),
                    MonitorSeen.state == "pending").values(state=active_window.RETIRED_STATE))
                db.execute(update(MonitorJob).where(MonitorJob.source_id.in_(retired_ids)).values(
                    state="cancelled", reason="active_window_disabled"))
                active_window.reset(db)
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
            if self.settings.ria_active_window_enabled:
                active_window.sync(db, {member.feed_id for _, _, member in active_members(db)})
            db.commit()

    def prepare_window(self, feed_id):
        with self._state_lock, Session(self.engine) as db:
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
        with self._state_lock, Session(self.engine) as db:
            if not self.owned(db):
                return
            feed = db.get(MonitorFeed, feed_id)
            for sid, uid, epoch in current_slice["members"]:
                state = self.current(db, sid, uid, epoch)
                if not state:
                    continue
                _, watch = state
                for source_id in ids:
                    seen = db.get(MonitorSeen, (sid, source_id))
                    if seen is not None and not (seen.epoch == epoch
                            and seen.state == active_window.RETIRED_STATE):
                        # A current publication-window result supersedes the
                        # unverified newest-page origin of an unevaluated ID.
                        # Turning that extra search off must not cancel primary
                        # work, including an ID already observed by this search.
                        if seen.epoch == epoch and seen.state == "pending":
                            job = db.get(MonitorJob, source_id)
                            if job:
                                job.result = {**job.result, "discovery_kind": "new_publication"}
                        continue
                    if seen is None:
                        db.add(MonitorSeen(search_id=sid, source_id=source_id, epoch=epoch,
                                           state="pending", first_seen=now))
                    else:
                        # Only cancellation of unverified supplemental work is
                        # reversible here, and only after fresh primary search
                        # evidence for the same active subscription epoch.
                        # Sent/uncertain delivery claims are never modified.
                        seen.state = "pending"
                    job = db.get(MonitorJob, source_id)
                    if job is None:
                        db.add(MonitorJob(source_id=source_id, first_seen=now))
                    else:
                        if job.state != "pending":
                            job.state, job.next_run, job.reason = "pending", 0, ""
                        job.result = {**job.result, "discovery_kind": "new_publication"}
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
        with self._state_lock, Session(self.engine) as db:
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
                if (not candidate or not notification_condition_allowed(candidate) or resolved is None
                        or not matches(candidate, filters, resolved)
                        or not (partial or priced_deal)):
                    continue
                proof = (price_only_evidence(candidate, evidence["evaluated_at"], evidence["uncertainty_reasons"],
                         pricing_policy=ria_market_range.VERSION if self.settings.ria_ai_price_enabled else None)
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
        with self._state_lock, Session(self.engine) as db:
            if not self.owned(db):
                return
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
        excluded = not notification_condition_allowed(candidate)
        rating = evidence.get("rating", {})
        proof = rating.get("valuation_evidence") or {}
        reusable_rating = (rating.get("valuation_version") in {VERSION, REFERENCE_VERSION}
            and all(-30 <= time.time() - peer["observed_at"] <= MAX_AGE for peer in proof.get("peers", [])))
        if self.settings.ria_ai_price_enabled:
            reusable_rating = (rating.get("valuation_version") == ria_market_range.VERSION
                and ria_market_range.range_valid(proof.get("source_range"), source_id, time.time())
                and bool((proof.get("source_range") or {}).get("provider")))
        if not reusable_rating:
            evidence.pop("rating", None)
        if any_match and not excluded and "rating" not in evidence:
            if self.settings.ria_ai_price_enabled:
                quote = None
                try:
                    quote = source.market_range(source_id, self.settings.auto_ria_user_id)
                except RiaError as exc:
                    # A method outage/permission/quota failure cannot hide the
                    # matching candidate whose positive price is already fresh.
                    log.warning("AUTO.RIA AI valuation unavailable source_id=%s reason=%s", source_id, str(exc))
                evidence["rating"] = ria_market_range.estimate(candidate, quote)
            else:
                try:
                    peers = source.notification_comparisons(candidate)
                except RiaError as exc:
                    if str(exc) not in {"search_limit", "quota_exceeded", "connection_error", "upstream_error"}:
                        raise
                    peers = PeerBatch(limited=True, unavailable=str(exc))
                evidence["rating"] = notification_estimate(candidate, peers)
        rating = evidence.get("rating", {})
        uncertainty = []
        if any_match and not excluded and rating.get("valuation") not in VALUED:
            if self.settings.ria_ai_price_enabled:
                uncertainty.append("provider_market_range_unavailable")
            if repair_notices(candidate):
                uncertainty.append("repair_condition")
            if partial:
                uncertainty.append("incomplete_details")
            elif rating.get("valuation") in {"insufficient_data", "mixed_sample"}:
                for reason in ("insufficient_comparables", "mixed_sample", "unverified_condition"):
                    if reason in rating.get("valuation_reasons", []):
                        uncertainty.append(reason)
                if not uncertainty:
                    uncertainty.append("missing_valuation_details")
            if uncertainty and not candidate.get("comparable_condition") and not repair_notices(candidate):
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
        with self._state_lock, Session(self.engine) as db:
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

    def parallel_step(self, tasks, groups):
        """At most four operations under the original global monitor/source leases.

        A due publication page owns one slot; the rest evaluate distinct jobs.
        Completion is per car, so delivery need not wait for the slowest quote.
        Joining all tasks before release also fences overlapping deployments.
        """
        source = self.search_factory(self.engine, self.settings.auto_ria_api_key)
        status = "evaluate" if any(kind == "evaluate" for kind, _ in tasks) else "discover"
        try:
            source.acquire()
        except RiaError as exc:
            for kind, key in tasks:
                self.defer(kind, key, str(exc), source.limits)
            return status, True
        started = time.monotonic()
        try:
            interval = poll_interval(groups, source.limits, provider_pricing_enabled=True,
                                     active_window_enabled=self.settings.ria_active_window_enabled,
                                     schedule_enabled=self.settings.ria_poll_schedule_enabled)
            with Session(self.engine) as db:
                db.execute(update(MonitorControl).where(MonitorControl.id == "pilot",
                    MonitorControl.owner == self.owner).values(status=status, heartbeat=time.time()))
                db.commit()
            def process(task):
                kind, key = task
                child = source.fork()
                try:
                    if kind == "discover":
                        self.discover(key, child, interval)
                    else:
                        # Supplemental valuations keep their own reserve and
                        # bounded request count even in a shared parallel batch.
                        with self._state_lock, Session(self.engine) as db:
                            job = db.get(MonitorJob, key)
                            if job.result.get("discovery_kind") == active_window.KIND:
                                child.request_limit = active_window.CALL_RESERVE
                                if not active_window.budget_available(db, child.limits,
                                        backlog=job.first_seen < time.time() - active_window.FRESH_ARRIVAL_SECONDS):
                                    raise RiaError("reserved_for_new_publications")
                        self.evaluate(key, child)
                except RiaError as exc:
                    if kind == "evaluate" and str(exc) == "listing_unavailable":
                        self.complete(key, {}, "unavailable")
                    else:
                        self.defer(kind, key, str(exc), child.limits)
                except Exception as exc:
                    log.error("Monitor operation failed kind=%s (%s)", kind, type(exc).__name__)
                    self.defer(kind, key, "processing_error", child.limits)
                    return False
                return True
            with ThreadPoolExecutor(max_workers=PARALLEL_TASKS) as pool:
                results = list(pool.map(process, tasks))
            if not all(results):
                status = "error"
            if len(tasks) > 1:
                batch_log.info("Monitor parallel batch tasks=%s evaluations=%s peak_requests=%s elapsed_seconds=%.3f",
                    len(tasks), sum(kind == "evaluate" for kind, _ in tasks),
                    source._lease_state["peak"], time.monotonic() - started)
        finally:
            source.release()
        return status, True

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
                self.reschedule(db, groups)
                feed = db.scalar(select(MonitorFeed).where(MonitorFeed.id.in_(groups),
                    MonitorFeed.next_poll <= time.time()).order_by(MonitorFeed.next_poll, MonitorFeed.id).limit(1))
                supplemental = func.coalesce(MonitorJob.result["discovery_kind"].as_string(), "") == active_window.KIND
                job_query = select(MonitorJob).where(MonitorJob.state == "pending", MonitorJob.next_run <= time.time())
                if not self.settings.ria_active_window_enabled or not active_window.budget_available(db):
                    job_query = job_query.where(~supplemental)
                elif not active_window.budget_available(db, backlog=True):
                    job_query = job_query.where((~supplemental) | (MonitorJob.first_seen >=
                        time.time() - active_window.FRESH_ARRIVAL_SECONDS))
                # Newly surfaced active-page IDs must not sit behind hours of
                # older supplemental backlog. Keep primary publication jobs
                # first, their retry ordering, and all existing budget gates.
                jobs = list(db.scalars(job_query.order_by(case((supplemental, 1), else_=0),
                    case((supplemental, -MonitorJob.first_seen), else_=MonitorJob.last_attempt),
                    MonitorJob.last_attempt, MonitorJob.first_seen, MonitorJob.source_id).limit(PARALLEL_TASKS)))
                job = jobs[0] if jobs else None
                last = db.get(MonitorControl, "pilot").status
                kind = "evaluate" if job and (not feed or last == "discover") else "discover" if feed else None
                key = job.source_id if kind == "evaluate" else feed.id if kind else None
                # A nonempty supplemental valuation queue must not starve its
                # own discovery. A due, bounded newest-page poll comes before
                # supplemental evaluation, but never before primary work.
                supplemental_selected = (kind == "evaluate" and
                    job.result.get("discovery_kind") == active_window.KIND)
                if self.settings.ria_active_window_enabled and (kind is None or
                        (not feed and supplemental_selected)):
                    extra = active_window.due(db, groups)
                    if extra:
                        if active_window.budget_available(db, reserve=1):
                            kind, key = active_window.KIND, extra.feed_id
                        else:
                            active_window.defer(db, extra.feed_id, "reserved_for_new_publications")
                            db.commit()
            # Active-page discovery remains a single bounded step. Both kinds
            # of valuation may overlap; due publication search keeps its slot.
            if kind and self.settings.ria_ai_price_enabled and kind != active_window.KIND:
                tasks = [("discover", feed.id)] if feed else []
                tasks.extend(("evaluate", row.source_id) for row in jobs[:PARALLEL_TASKS - len(tasks)])
                status, worked = self.parallel_step(tasks, len(groups))
            elif kind:
                status, worked = kind, True
                source = self.search_factory(self.engine, self.settings.auto_ria_api_key)
                supplemental_work = kind == active_window.KIND or (kind == "evaluate"
                        and job.result.get("discovery_kind") == active_window.KIND)
                if supplemental_work:
                    source.request_limit = (1 if kind == active_window.KIND else active_window.CALL_RESERVE)
                acquired = False
                try:
                    source.acquire()
                    acquired = True
                    if supplemental_work:
                        with Session(self.engine) as db:
                            if not active_window.budget_available(db, source.limits,
                                    reserve=source.request_limit,
                                    backlog=(kind == "evaluate" and job.first_seen <
                                        time.time() - active_window.FRESH_ARRIVAL_SECONDS)):
                                raise RiaError("reserved_for_new_publications")
                    if kind == active_window.KIND:
                        active_window.discover(self, key, source)
                    elif kind == "discover":
                        self.discover(key, source, poll_interval(len(groups), source.limits,
                            provider_pricing_enabled=self.settings.ria_ai_price_enabled,
                            active_window_enabled=self.settings.ria_active_window_enabled,
                            schedule_enabled=self.settings.ria_poll_schedule_enabled))
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
        enqueue(self.engine, allow_active_window=self.settings.ria_active_window_enabled,
                require_provider_range=self.settings.ria_ai_price_enabled)
        # Legacy single-step test/operator helper; the running dispatcher below
        # enforces per-chat spacing when sending batches concurrently.
        deliver_one(self.engine, self.settings, self.sender or TelegramSender(self.settings.bot_token),
                    enforce_chat_interval=False)


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

    async def dispatch():
        from .worker import TelegramSender, enqueue
        sender = TelegramSender(settings.bot_token)
        next_enqueue = 0
        while not stop.is_set():
            worked = False
            try:
                if settings.live and settings.monitor_enabled and webhook_status(engine)["status"] == "configured":
                    if time.monotonic() >= next_enqueue:
                        await asyncio.to_thread(enqueue, engine,
                            allow_active_window=settings.ria_active_window_enabled,
                            require_provider_range=settings.ria_ai_price_enabled)
                        next_enqueue = time.monotonic() + 1
                    # Four simultaneous requests, with at least .25s between
                    # batches: at most 16/s, below Telegram's free broadcast cap.
                    states = await delivery_batch(engine, settings, sender)
                    worked = any(state not in {"empty", "busy"} for state in states)
            except Exception as exc:
                log.error("Delivery unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=.25 if worked else 1)
            except TimeoutError:
                pass

    from .telegram_setup import webhook_status
    # A slow source HTTP request never blocks dispatch or its bounded workers.
    await asyncio.gather(loop(monitor.tick), dispatch())


async def delivery_batch(engine, settings, sender):
    from .worker import deliver_one
    return await asyncio.gather(*(asyncio.to_thread(
        deliver_one, engine, settings, sender) for _ in range(4)))
