"""Bounded owner restoration; offline SQLite, fixture RIA and Telegram only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import owner_car_notifications, paid_owner_restoration, paid_source_access
from backend.models import (Delivery, DeliveryTiming, Listing, MonitorFeed, MonitorJob,
    MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search, SourceProbe, User)
from backend.monitor import interest_query
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import approve, strict
from backend.tests.test_ria_ai_price import enable


def dormant(p):
    strict(p)
    settings = replace(p.settings, admin_telegram_id=111)
    paid_source_access.configure(p.engine, settings)
    approve(p)
    add_search(p, sid=2, uid=222); approve(p, 222, purchase_until=p.clock[0]+20000, access_until=p.clock[0]+20000)
    with Session(p.engine) as db:
        # Paid owner remains current while formerly excluded memberships age.
        from backend.billing_models import Entitlement
        from backend.manual_payment_models import PaymentRequest
        db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0] + 20000
        db.get(Entitlement, 111).expires_at = p.clock[0] + 20000
        member = db.get(MonitorMembership, 1)
        old_start = member.started_at
        db.add(MonitorFeed(id=member.feed_id, filters=p.filters.canonical(), started_at=old_start,
            cursor=old_start, context={}, next_poll=0, checked_at=old_start, status="watching"))
        listing = Listing(source="auto_ria", source_id="history", car={})
        db.add(listing); db.flush()
        db.add(MonitorMatch(search_id=1, listing_id=listing.id,
            epoch=db.get(MonitorWatch, 1).epoch, fingerprint=p.filters.fingerprint()))
        for sid in (1, 2):
            db.add(MonitorSeen(search_id=sid, source_id="124", epoch=db.get(MonitorWatch, sid).epoch,
                state="pending", first_seen=p.clock[0]))
        db.add(MonitorJob(source_id="124", first_seen=p.clock[0], state="pending", result={}))
        db.commit()
    p.clock[0] += 7200
    return settings


def history(db):
    return ([(r.search_id, r.source_id, r.epoch, r.state, r.first_seen) for r in db.scalars(
                select(MonitorSeen).order_by(MonitorSeen.search_id, MonitorSeen.source_id))],
            [(r.search_id, r.listing_id, r.epoch, r.fingerprint) for r in db.scalars(
                select(MonitorMatch).order_by(MonitorMatch.search_id, MonitorMatch.listing_id))])


def test_dormant_owner_rebases_once_preserving_other_client_feed_and_all_history(p):
    settings = dormant(p)
    with Session(p.engine) as db:
        before = history(db)
        feed = db.scalar(select(MonitorFeed))
        original_feed = (feed.id, feed.cursor, feed.started_at, feed.context, feed.checked_at, feed.next_poll)
        client = (db.get(MonitorWatch, 2).epoch, db.get(MonitorMembership, 2).started_at)
        owner_epoch = db.get(MonitorWatch, 1).epoch
        assert [uid for _, uid, _, _ in db.execute(interest_query("124", p.clock[0],
            **paid_source_access.query_options(p.engine)))] == [111, 222]
    result = paid_owner_restoration.initialize(p.engine, settings, p.clock[0])
    assert result["status"] == "applied" and result["rebased_searches"] == 1
    with Session(p.engine) as db:
        assert history(db) == before
        feed = db.scalar(select(MonitorFeed))
        assert (feed.id, feed.cursor, feed.started_at, feed.context, feed.checked_at, feed.next_poll) == original_feed
        assert (db.get(MonitorWatch, 2).epoch, db.get(MonitorMembership, 2).started_at) == client
        restored_epoch = db.get(MonitorWatch, 1).epoch
        assert restored_epoch != owner_epoch
        assert db.get(MonitorMembership, 1).epoch == restored_epoch
        assert db.get(MonitorMembership, 1).started_at == int(p.clock[0])
        assert [uid for _, uid, _, _ in db.execute(interest_query("124", p.clock[0],
            **paid_source_access.query_options(p.engine)))] == [222]
    assert paid_owner_restoration.initialize(p.engine, settings, p.clock[0]+300)["status"] == "already_initialized"
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == restored_epoch
    assert not p.calls and not p.sent


def test_restored_owner_only_receives_new_publication_and_shared_client_still_progresses(p, monkeypatch):
    settings = dormant(p)
    quotes = enable(p, monkeypatch)
    paid_owner_restoration.initialize(p.engine, settings, p.clock[0])
    p.ads["124"] = p.clock[0] - 3000
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(222, "124")]
    p.ads["125"] = p.clock[0] + 1
    wake(p); drain(p)
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "125"), (222, "124"), (222, "125")]
    assert quotes == ["124", "125"]
    assert sum(path == "info" and params["auto_id"] == "125" for path, params in p.calls) == 1


@pytest.mark.parametrize("state,started,accepted,message,source,copy_status,expected", [
    ("pending", None, None, None, "auto_ria", None, "cancelled"),
    ("pending", 1, None, None, "auto_ria", None, "pending"),
    ("pending", None, 1, None, "auto_ria", None, "pending"),
    ("pending", None, None, 71, "auto_ria", None, "pending"),
    ("sent", 1, 1, 71, "auto_ria", None, "sent"),
    ("uncertain", 1, None, None, "auto_ria", None, "uncertain"),
    ("sending", None, None, None, "auto_ria", None, "sending"),
    ("pending", None, None, None, "olx", None, "pending"),
    ("pending", None, None, None, "auto_ria", "retired_unattempted", "pending"),
    ("pending", None, None, None, "auto_ria", "ordinary_reclaimed", "cancelled"),
])
def test_only_proven_unattempted_ordinary_ria_claims_are_cancelled(p, state, started, accepted, message,
                                                               source, copy_status, expected):
    settings = dormant(p)
    with Session(p.engine) as db:
        listing = Listing(source=source, source_id="queued-proof", car={})
        db.add(listing); db.flush()
        delivery = Delivery(user_id=111, listing_id=listing.id, state=state, message_id=message)
        db.add(delivery); db.flush()
        did = delivery.id
        db.add(DeliveryTiming(delivery_id=did, queued_at=p.clock[0]-3000,
            send_started_at=started, accepted_at=accepted))
        if copy_status:
            db.add(SourceProbe(id=owner_car_notifications.marker(did), status=copy_status,
                checked_at=p.clock[0], requests=0, result={"historical": True}))
        db.commit()
    result = paid_owner_restoration.initialize(p.engine, settings, p.clock[0])
    with Session(p.engine) as db:
        assert db.get(Delivery, did).state == expected
        timing = db.get(DeliveryTiming, did)
        assert (timing.send_started_at, timing.accepted_at) == (started, accepted)
        if copy_status:
            assert db.get(SourceProbe, owner_car_notifications.marker(did)).status == copy_status
    assert result["cancelled_unattempted_deliveries"] == int(expected == "cancelled")
    assert not p.calls and not p.sent


@pytest.mark.parametrize("reason", ["stopped", "disabled", "expired", "unconfirmed"])
def test_owner_without_current_permitted_paid_search_is_untouched(p, reason):
    settings = dormant(p)
    with Session(p.engine) as db:
        from backend.manual_payment_models import PaymentRequest
        if reason == "stopped": db.get(User, 111).ready = False
        elif reason == "disabled": db.get(Search, 1).enabled = False
        elif reason == "expired": db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0]
        elif reason == "unconfirmed": db.get(PaymentRequest, "fixture-111").state = "review"
        db.commit()
        before = (db.get(User, 111).ready, db.get(Search, 1).enabled,
            db.get(MonitorWatch, 1).epoch, db.get(MonitorMembership, 1).started_at, history(db))
    assert paid_owner_restoration.initialize(p.engine, settings, p.clock[0])["status"] == "not_applicable"
    with Session(p.engine) as db:
        assert (db.get(User, 111).ready, db.get(Search, 1).enabled,
            db.get(MonitorWatch, 1).epoch, db.get(MonitorMembership, 1).started_at, history(db)) == before


def test_access_read_failure_aborts_without_marker_or_state_changes(p, monkeypatch):
    settings = dormant(p)
    with Session(p.engine) as db:
        before = (db.get(MonitorWatch, 1).epoch, db.get(MonitorMembership, 1).started_at, history(db))
    def unavailable(*_):
        raise OperationalError("isolated access read failure", {}, None)
    monkeypatch.setattr(paid_source_access, "allowed", unavailable)
    with pytest.raises(OperationalError):
        paid_owner_restoration.initialize(p.engine, settings, p.clock[0])
    with Session(p.engine) as db:
        assert db.get(SourceProbe, paid_owner_restoration.CONTROL) is None
        assert (db.get(MonitorWatch, 1).epoch, db.get(MonitorMembership, 1).started_at, history(db)) == before
    assert not p.calls and not p.sent


def test_parallel_initializers_create_one_marker_and_one_activation_epoch(p):
    settings = dormant(p)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: paid_owner_restoration.initialize(p.engine, settings, p.clock[0]), range(3)))
    assert [row["status"] for row in results].count("applied") == 1
    assert [row["status"] for row in results].count("already_initialized") == 2
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(SourceProbe).where(SourceProbe.id == paid_owner_restoration.CONTROL)))) == 1
        assert db.get(MonitorWatch, 1).epoch == db.get(MonitorMembership, 1).epoch
    assert not p.calls and not p.sent
