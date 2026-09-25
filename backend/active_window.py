"""Supplement new publications with a deliberately limited active-listing window.

One newest-page search per group. Every new ID entering that page is queued.
An explicit operator option also includes unseen IDs on the initial page.
No historical pagination. New publications/jobs always take scheduling priority.
"""
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Delivery, Filters, Listing, MonitorActiveWindow, MonitorFeed,
                     MonitorJob, MonitorSeen, SourceBudget, SourceProbe)
from .ria_budget import BudgetLimits
from .ria_search import budget_state, parse_ids

# Seven shared groups at one request per minute can exceed the 12,000/day
# allowance even before publication searches and listing details. A five-minute
# first-page poll uses at most 2,016 searches/day for seven groups.
INTERVAL = 300
WINDOW_SIZE = 50
KIND = "active_window"
RETIRED_STATE = "window_cancelled"
CALL_RESERVE = 32
FRESH_ARRIVAL_SECONDS = 600
BASELINE_ID = "active-window-fresh-only-v1"


def budget_available(db, limits=None, *, reserve=CALL_RESERVE, backlog=False):
    limits, now = limits or BudgetLimits.env(), time.time()
    row = db.get(SourceBudget, "auto_ria")
    if not row or budget_state(row, now, limits)["reason"] != "available":
        return False
    # Primary searches have scheduling priority and keep 20% hourly headroom,
    # with at least one full bounded evaluation step reserved. Counting primary
    # calls against a 50% ceiling previously stalled fresh arrivals around
    # 420/900 calls even while almost half the real quota was unused.
    # The daily reserve and per-request hard caps remain unchanged.
    hourly_ceiling = (limits.hourly // 2 if backlog else
                      limits.hourly - max(limits.hourly // 5, CALL_RESERVE))
    return (sum(t > now - 3600 for t in row.calls) + reserve <= hourly_ceiling
            and sum(t > now - 86400 for t in row.calls) + reserve
                <= limits.daily - max(limits.daily // 5, limits.hourly * 2)
            and row.total + reserve <= limits.total)


def sync(db, feed_ids):
    # Old rows can predate this fresh-only policy. A one-time marker creates
    # a clean baseline even if the flag was previously on, then turned off
    # while old code left its first-page snapshot in place.
    if db.get(SourceProbe, BASELINE_ID) is None:
        reset(db)
        db.add(SourceProbe(id=BASELINE_ID, status="baselined", checked_at=time.time(),
                           requests=0, result={}))
    for feed_id in sorted(feed_ids):
        if db.get(MonitorActiveWindow, feed_id) is None:
            db.add(MonitorActiveWindow(feed_id=feed_id))


def reset(db):
    """Forget page snapshots when disabled, preserving jobs and delivery claims."""
    for row in db.scalars(select(MonitorActiveWindow)):
        row.window, row.source_total = [], 0
        row.checked_at, row.next_poll, row.status = 0, 0, "disabled"


def status(db, feed_ids, enabled, include_initial=False):
    rows = list(db.scalars(select(MonitorActiveWindow).where(MonitorActiveWindow.feed_id.in_(feed_ids))))
    return {"enabled": enabled, "coverage": "latest_active_window_diff",
            "include_initial": include_initial,
            "window_size": WINDOW_SIZE, "interval_seconds": INTERVAL,
            "maximum_candidates_per_poll": WINDOW_SIZE, "historical_pagination": False,
            "state_counts": dict(Counter(row.status for row in rows)) if enabled else {},
            "discovery_budget_available": bool(enabled and budget_available(db, reserve=1)),
            "valuation_budget_available": bool(enabled and budget_available(db)),
            "backlog_budget_available": bool(enabled and budget_available(db, backlog=True)),
            "last_checked_at": max((row.checked_at for row in rows), default=0) if enabled else None}


def due(db, feed_ids):
    return db.scalar(select(MonitorActiveWindow).where(MonitorActiveWindow.feed_id.in_(feed_ids),
        MonitorActiveWindow.next_poll <= time.time()).order_by(MonitorActiveWindow.next_poll,
                                                              MonitorActiveWindow.feed_id).limit(1))


def defer(db, feed_id, reason):
    row = db.get(MonitorActiveWindow, feed_id)
    row.status, row.next_poll = reason, time.time() + INTERVAL


def discover(monitor, feed_id, source):
    from .monitor import active_members

    with Session(monitor.engine) as db:
        if not monitor.owned(db):
            return
        # The newest-page search uses exactly one request. A pending detail/AI
        # evaluation may need up to CALL_RESERVE and is gated separately.
        if not budget_available(db, source.limits, reserve=1):
            defer(db, feed_id, "reserved_for_new_publications")
            db.commit()
            return
        members = [(search.id, search.user_id, watch.epoch)
                   for search, watch, member in active_members(db) if member.feed_id == feed_id]
        if not members:
            return
        filters = Filters.model_validate(db.get(MonitorFeed, feed_id).filters)
    params, _ = source.discovery_parameters(filters)
    params.update(countpage=WINDOW_SIZE, page=0, order_by=7)
    result = source.request("search", params, parse_ids, force=True)
    ids = result["ids"]
    if len(ids) < min(WINDOW_SIZE, result["total"]):
        raise RiaError("invalid_response")
    with Session(monitor.engine) as db:
        if not monitor.owned(db):
            return
        recipients = [(sid, uid, epoch) for sid, uid, epoch in members
                      if monitor.current(db, sid, uid, epoch)]
        row = db.get(MonitorActiveWindow, feed_id)
        now = time.time()
        baseline = row.checked_at <= 0 and not row.window
        previous = set(row.window or [])
        include_initial = monitor.settings.ria_active_window_include_initial
        arrivals = ([] if baseline and not include_initial else
                    [source_id for source_id in ids if source_id not in previous])
        selected = []

        # Including the initial page is an explicit active-listing opt-in.
        # Every path still preserves existing seen IDs and delivery claims.
        for source_id in arrivals:
            listing = db.scalar(select(Listing).where(Listing.source == "auto_ria",
                                                      Listing.source_id == source_id))
            interested = []
            for sid, uid, epoch in recipients:
                if listing and db.scalar(select(Delivery.id).where(
                        Delivery.user_id == uid, Delivery.listing_id == listing.id)) is not None:
                    continue
                seen = db.get(MonitorSeen, (sid, source_id))
                # Re-entry into the top page (or a changed price) is not a
                # new interest for a subscription that saw this ID already.
                if seen is None:
                    interested.append((sid, epoch))
            if interested:
                selected.append((source_id, interested))

        for source_id, interested in selected:
            for sid, epoch in interested:
                seen = db.get(MonitorSeen, (sid, source_id))
                if seen is None:
                    db.add(MonitorSeen(search_id=sid, source_id=source_id, epoch=epoch,
                                       state="pending", first_seen=now))
                else:
                    seen.state = "pending"
            job = db.get(MonitorJob, source_id)
            if job is None:
                db.add(MonitorJob(source_id=source_id, first_seen=now, result={"discovery_kind": KIND}))
            elif job.state != "pending":
                # Reuse fresh details/quote when another group finds this car.
                # This new interest is supplemental; normal age checks still run.
                job.state, job.next_run = "pending", 0
                job.result = {**job.result, "discovery_kind": KIND}

        row.window, row.source_total = ids, result["total"]
        row.status = "baseline" if baseline and not include_initial else "watching"
        row.checked_at, row.next_poll = now, now + INTERVAL
        db.commit()
