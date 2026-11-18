"""Reset through real discovery, valuation and transport, with no live I/O."""
import copy
import math

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import notification_baseline as baseline
from backend.models import (Delivery, Listing, MonitorControl, MonitorFeed, MonitorJob,
    MonitorMatch, MonitorMembership, Search, SourceProbe, User)
from backend.billing_models import BillingOrder, Entitlement
from backend.reset_notification_history import apply, plan, configured_owner
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.worker import deliver_one, enqueue


def reset(p, instant=None, reset_id="reset-1"):
    # Operators must stop all writers and wait for the existing lease to expire.
    with Session(p.engine) as db:
        db.get(MonitorControl, "pilot").lease_until = 0
        db.commit()
    return apply(p.engine, 111, p.clock[0] if instant is None else instant,
                 reset_id, writers_stopped=True)


@pytest.mark.parametrize("published,lower,expected", [
    (99, 101, False), (100, 101, False), (101, None, True),
    (None, 99, False), (None, 100, True), (None, None, False),
    (None, 201, False), (math.nan, 101, False), (True, 101, False)])
def test_only_publication_time_or_dated_search_lower_bound_is_proof(published, lower, expected):
    assert baseline.after(100, published_at=published, publication_after=lower, now=200) is expected


def test_owner_mode_requires_existing_verified_identity(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "111")
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "111")
    assert configured_owner() == 111
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "222")
    with pytest.raises(ValueError, match="identity_must_be_verified"):
        configured_owner()


def test_backlog_reset_restart_new_arrivals_and_dedupe(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()  # discover
    p.runner.tick()  # value, no dispatch
    enqueue(p.engine, now=p.clock[0])
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == "pending"
        old_listing = db.scalar(select(Listing)).id
    at = p.clock[0]
    reset(p, at)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == "cancelled"
        assert db.get(Listing, old_listing) is not None
        assert db.get(MonitorMembership, 1).started_at == at
        assert db.scalar(select(MonitorFeed)).cursor == at
        assert db.scalar(select(MonitorFeed)).context == {}
    # Both downtime cars and a never-seen old result remain in the source.
    p.ads["125"] = at - 100
    p.ads["126"] = at + 1
    wake(p)
    from backend.monitor import Monitor
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "126")]
    wake(p)
    drain(p)
    assert len(p.sent) == 1


def test_requeued_old_delivery_cannot_send_or_start_refresh(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    enqueue(p.engine, now=p.clock[0])
    at = p.clock[0]
    reset(p, at)
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        delivery.state = "pending"  # Simulate an old recovery/admin queue.
        listing = db.get(Listing, delivery.listing_id)
        car = copy.deepcopy(listing.car)
        car["observed_at"] = at - 1000  # Must not create a refresh job.
        listing.car = car
        db.commit()
    before = len(p.calls)
    assert deliver_one(p.engine, p.settings, p.runner.sender, now=at) == "cancelled"
    assert not p.sent and len(p.calls) == before


def test_dry_run_idempotency_and_all_protected_tables_unchanged(p):
    drain(p)
    with Session(p.engine) as db:
        db.add(Entitlement(user_id=111, expires_at=p.clock[0]+9999, updated_at=p.clock[0]))
        db.add(BillingOrder(id="paid", user_id=111, update_id=1, amount=100,
            terms_version="v1", created_at=p.clock[0], state="paid", charge_id="charge"))
        db.commit()
    from backend.models import Base
    changed = {"source_probes", "monitor_watches", "monitor_memberships", "monitor_feeds",
               "monitor_active_windows", "monitor_matches", "monitor_seen", "monitor_jobs", "deliveries"}
    def snapshot():
        with p.engine.connect() as conn:
            return {t.name: conn.execute(select(t)).all() for t in Base.metadata.sorted_tables
                    if t.name not in changed}
    original = snapshot()
    with Session(p.engine) as db:
        report = plan(db, 111, p.clock[0], "once")
        assert not report["already_applied"]
    with Session(p.engine) as db:
        assert db.get(SourceProbe, baseline.key(111)) is None
    reset(p, reset_id="once")
    with Session(p.engine) as db:
        epoch = db.get(MonitorMembership, 1).epoch
    assert reset(p, reset_id="once")["already_applied"]
    with Session(p.engine) as db:
        assert db.get(MonitorMembership, 1).epoch == epoch
    assert snapshot() == original
    with pytest.raises(ValueError, match="different_baseline"):
        reset(p, p.clock[0]-1, "once")
    with pytest.raises(ValueError, match="backwards"):
        reset(p, p.clock[0]-1, "other")


def test_shared_feed_and_other_recipient_are_preserved(p):
    add_search(p)  # Same shared feed, second recipient.
    drain(p)
    wake(p, 3600)
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        before = feed.cursor, copy.deepcopy(feed.context)
        other = db.get(MonitorMembership, 2).epoch
    reset(p)
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert (feed.cursor, feed.context) == before
        assert db.get(MonitorMembership, 2).epoch == other
    p.ads["124"] = p.clock[0] - 100
    p.ads["125"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert (111, "124") not in [(uid, car.source_id) for uid, car in p.sent]
    assert {(uid, car.source_id) for uid, car in p.sent} == {(222, "124"), (111, "125"), (222, "125")}


def test_requires_quiescence_and_valid_user(p):
    with pytest.raises(ValueError, match="stop_all"):
        apply(p.engine, 111, p.clock[0], "once")
    drain(p)
    with Session(p.engine) as db:
        db.get(MonitorControl, "pilot").lease_until = p.clock[0] + 120
        db.commit()
    with pytest.raises(ValueError, match="lease_still_active"):
        apply(p.engine, 111, p.clock[0], "once", writers_stopped=True)


def test_known_old_source_date_overrides_even_an_incorrect_new_search_result(p):
    from datetime import datetime
    from backend.recent_publications import KYIV
    reset(p)
    source_factory = p.runner.search_factory
    old_date = datetime.fromtimestamp(p.clock[0]-3600, KYIV).strftime("%Y-%m-%d %H:%M:%S")
    def factory(engine, key):
        source = source_factory(engine, key)
        fetch = source.fetch
        def altered(key, path, params):
            result = fetch(key, path, params)
            if path == "info" and params["auto_id"] == "124":
                result["addDate"] = old_date
            return result
        source.fetch = altered
        return source
    p.runner.search_factory = factory
    p.ads["124"] = p.clock[0]+1  # Simulate a stale ID returned in a fresh window.
    p.ads["125"] = p.clock[0]+2
    wake(p)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["125"]


def test_reset_rolls_back_all_changes_on_failure(p, monkeypatch):
    import backend.monitor
    drain(p)
    with Session(p.engine) as db:
        db.get(MonitorControl, "pilot").lease_until = 0
        db.commit()
    from backend.models import Base
    def snapshot():
        with p.engine.connect() as conn:
            return {t.name:conn.execute(select(t)).all() for t in Base.metadata.sorted_tables}
    before = snapshot()
    original = backend.monitor.reset_watch
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted")
    monkeypatch.setattr(backend.monitor, "reset_watch", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        apply(p.engine,111,p.clock[0],"once",writers_stopped=True)
    assert snapshot() == before


@pytest.mark.parametrize("created,expected", [(None, False), (-1, False), (0, False), (1, True)])
def test_olx_requires_identity_verified_creation_after_cutoff(p, created, expected):
    reset(p)
    car = {"checked_at": p.clock[0], "first_seen": p.clock[0]}
    if created is not None:
        car["source_date_observations"] = {"identity_matches": True, "issues": [],
            "values": {"createdTime": {"epoch": p.clock[0]+created}}}
    p.clock[0] += 2
    assert baseline.olx_allowed(p.engine, 111, car) is expected
