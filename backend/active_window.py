"""Supplement new publications with a deliberately limited active-listing window.

One first-page search and at most one candidate per group per five minutes.
No historical pagination. New publications/jobs always take scheduling priority.
"""
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import (Delivery, Filters, Listing, MonitorActiveWindow, MonitorFeed,
                     MonitorJob, MonitorSeen, SourceBudget)
from .ria_budget import BudgetLimits
from .ria_search import budget_state, parse_ids

INTERVAL = 300
WINDOW_SIZE = 50
RECHECK_SECONDS = 1800
KIND = "active_window"
CALL_RESERVE = 32


def budget_available(db, limits=None):
    limits, now = limits or BudgetLimits.env(), time.time()
    row = db.get(SourceBudget, "auto_ria")
    if not row or budget_state(row, now, limits)["reason"] != "available":
        return False
    # Reserve a full bounded work step, keeping half the rolling allowances for
    # primary work even if every permitted call in this step is consumed.
    return (sum(t > now - 3600 for t in row.calls) + CALL_RESERVE <= limits.hourly // 2
            and sum(t > now - 86400 for t in row.calls) + CALL_RESERVE <= limits.daily // 2
            and row.total + CALL_RESERVE <= limits.total)


def sync(db, feed_ids):
    for feed_id in sorted(feed_ids):
        if db.get(MonitorActiveWindow, feed_id) is None:
            db.add(MonitorActiveWindow(feed_id=feed_id))


def status(db, feed_ids, enabled):
    rows = list(db.scalars(select(MonitorActiveWindow).where(MonitorActiveWindow.feed_id.in_(feed_ids))))
    return {"enabled": enabled, "coverage": "latest_active_window_only",
            "window_size": WINDOW_SIZE, "interval_seconds": INTERVAL,
            "maximum_candidates_per_poll": 1, "historical_pagination": False,
            "state_counts": dict(Counter(row.status for row in rows)) if enabled else {},
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
        if not budget_available(db, source.limits):
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
        now = time.time()
        unseen, refresh = [], []
        for source_id in ids:
            job = db.get(MonitorJob, source_id)
            listing = db.scalar(select(Listing).where(Listing.source == "auto_ria",
                                                      Listing.source_id == source_id))
            new_members, old_members = [], []
            for sid, uid, epoch in recipients:
                if listing and db.scalar(select(Delivery.id).where(
                        Delivery.user_id == uid, Delivery.listing_id == listing.id)) is not None:
                    continue
                seen = db.get(MonitorSeen, (sid, source_id))
                if seen is None:
                    new_members.append((sid, epoch))
                # A shared informational outcome does not mean this recipient
                # received anything. Delivery records above remain authoritative.
                elif (seen.epoch == epoch and job and job.state in {"checked", "unvalued", "informational"}
                      and seen.state in {"checked", "unvalued", "informational"}
                      and job.last_attempt <= now - RECHECK_SECONDS):
                    old_members.append((sid, epoch))
            if new_members:
                unseen.append((source_id, new_members + old_members))
            elif old_members:
                refresh.append((source_id, old_members))
        # Existing non-deals are occasionally refreshed, so a price drop on the
        # same ID is not permanently suppressed. Oldest observation wins ties.
        refresh.sort(key=lambda item: (db.get(MonitorJob, item[0]).last_attempt, item[0]))
        selected = (unseen or refresh)[:1]
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
                job.state, job.next_run, job.result = "pending", 0, {"discovery_kind": KIND}
        row = db.get(MonitorActiveWindow, feed_id)
        row.window, row.source_total = ids, result["total"]
        row.status, row.checked_at, row.next_poll = "limited_window", now, now + INTERVAL
        db.commit()
