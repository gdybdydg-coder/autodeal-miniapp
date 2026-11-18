"""Dry-run first, transactional reset for ONE recipient; all writers must stop.

Run with --help. No schema changes, settings edits, paid API calls or Telegram I/O.
The baseline is explicit so retries of one reset never advance the cutoff.
"""
import argparse
from datetime import datetime
import json
import math
import os
import re
import time

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from .models import (Delivery, Listing, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch,
                     Search, SourceProbe, User)
from .notification_baseline import boundary, key


def validate(baseline, reset_id):
    if type(baseline) not in (int, float) or not math.isfinite(baseline) or not 0 < baseline <= time.time():
        raise ValueError("baseline_must_be_a_past_or_current_UTC_instant")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reset_id):
        raise ValueError("invalid_reset_id")


def configured_owner():
    owner = os.getenv("ADMIN_TELEGRAM_ID", "")
    expected = os.getenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "")
    if not owner.isdecimal() or int(owner) <= 0 or owner != expected:
        raise ValueError("configured_owner_identity_must_be_verified")
    return int(owner)


def plan(db, user_id, baseline, reset_id):
    validate(baseline, reset_id)
    if db.get(User, user_id) is None:
        raise ValueError("unknown_user")
    prior = db.get(SourceProbe, key(user_id))
    if prior and prior.result.get("reset_id") == reset_id:
        if prior.result["baseline_at"] != baseline:
            raise ValueError("reset_id_already_used_with_different_baseline")
        return {"already_applied": True, "user_id": user_id, "baseline_at": baseline}
    old = boundary(db, user_id)
    if old is not None and baseline <= old:
        raise ValueError("baseline_cannot_move_backwards")
    searches = select(Search.id).where(Search.user_id == user_id)
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where))
    return {"already_applied": False, "user_id": user_id, "reset_id": reset_id,
            "baseline_at": baseline, "previous_baseline": old,
            "searches": count(Search, Search.user_id == user_id),
            "seen_interests": count(MonitorSeen, MonitorSeen.search_id.in_(searches)),
            "matches": count(MonitorMatch, MonitorMatch.search_id.in_(searches)),
            "unsent_deliveries": count(Delivery, Delivery.user_id == user_id,
                                        Delivery.state.in_(("pending", "sending", "owner_pending")))}


def apply(engine, user_id, baseline, reset_id, *, writers_stopped=False):
    if not writers_stopped:
        raise ValueError("stop_all_API_monitor_delivery_OLX_and_owner_workers_first")
    from .monitor import reset_watch
    with Session(engine) as db, db.begin():
        db.scalar(select(User).where(User.id == user_id).with_for_update())
        result = plan(db, user_id, baseline, reset_id)
        if result["already_applied"]:
            return result
        control = db.scalar(select(MonitorControl).where(MonitorControl.id == "pilot").with_for_update())
        if control and control.lease_until > time.time():
            raise ValueError("monitor_lease_still_active_wait_for_expiry")
        searches = list(db.scalars(select(Search).where(Search.user_id == user_id).order_by(Search.id)))
        ids = [s.id for s in searches]
        old_sources = list(db.scalars(select(MonitorSeen.source_id).where(MonitorSeen.search_id.in_(ids)).distinct()))
        feed_ids = set(db.scalars(select(MonitorMembership.feed_id).where(MonitorMembership.search_id.in_(ids))))
        for search in searches:
            # Reuse normal activation semantics, without changing Search fields.
            reset_watch(db, search.id, search.enabled, started_at=baseline)
        db.flush()
        feed_ids.update(db.scalars(select(MonitorMembership.feed_id).where(MonitorMembership.search_id.in_(ids))))
        for feed_id in sorted(feed_ids):
            others = db.scalar(select(MonitorMembership.search_id).where(
                MonitorMembership.feed_id == feed_id, MonitorMembership.search_id.not_in(ids)).limit(1))
            feed = db.get(MonitorFeed, feed_id)
            if feed and others is None:
                feed.started_at = feed.cursor = baseline
                feed.context, feed.next_poll, feed.checked_at, feed.status = {}, 0, 0, "starting"
                from .models import MonitorActiveWindow
                window = db.get(MonitorActiveWindow, feed_id)
                if window:
                    window.window, window.next_poll, window.checked_at, window.status = [], 0, 0, "starting"
        for source_id in old_sources:
            current = db.scalar(select(MonitorSeen.search_id).join(MonitorWatch,
                MonitorWatch.search_id == MonitorSeen.search_id).where(
                MonitorSeen.source_id == source_id, MonitorSeen.state == "pending",
                MonitorSeen.epoch == MonitorWatch.epoch).limit(1))
            job = db.get(MonitorJob, source_id)
            if job and job.state == "pending" and current is None:
                job.state, job.reason, job.next_run = "cancelled", "notification_baseline_reset", 0
        db.execute(update(Delivery).where(Delivery.user_id == user_id,
            Delivery.state.in_(("pending", "sending", "owner_pending"))).values(state="cancelled", retry_at=0))
        row = db.get(SourceProbe, key(user_id))
        if row is None:
            row = SourceProbe(id=key(user_id), requests=0)
            db.add(row)
        row.status, row.checked_at = "active", time.time()
        row.result = {"baseline_at": baseline, "reset_id": reset_id, "plan": result}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    recipient = parser.add_mutually_exclusive_group(required=True)
    recipient.add_argument("--user-id", type=int)
    recipient.add_argument("--owner", action="store_true", help="verified ADMIN_TELEGRAM_ID only")
    parser.add_argument("--baseline", required=True, help="ISO 8601 instant with timezone; use the SAME value on retry")
    parser.add_argument("--reset-id", required=True)
    parser.add_argument("--apply", action="store_true", help="default is read-only dry run")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    user_id = configured_owner() if args.owner else args.user_id
    instant = datetime.fromisoformat(args.baseline.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        parser.error("--baseline requires a timezone")
    baseline = instant.timestamp()
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        if args.apply:
            result = apply(engine, user_id, baseline, args.reset_id, writers_stopped=args.writers_stopped)
        else:
            with engine.connect() as conn:
                with conn.begin():
                    if conn.dialect.name == "postgresql":
                        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    with Session(bind=conn) as db:
                        result = plan(db, user_id, baseline, args.reset_id)
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
