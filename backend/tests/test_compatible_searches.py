from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.compatible_searches import discovery_filters
from backend.models import (Filters, MonitorActiveWindow, MonitorFeed, MonitorMembership,
                            MonitorSeen, MonitorWatch, Range, Search)
from backend.monitor import Monitor, active_members, reset_watch, source_filters
from backend.tests.test_monitor import p, add_search, drain, searches, wake


def groups(p):
    with Session(p.engine) as db:
        return {member.feed_id for _, _, member in active_members(db)}


def sync(p):
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")


def test_one_discovery_preserves_distinct_optional_filters_and_restart(p):
    # The car has 100,000 km: only the unrestricted subscription may receive it.
    add_search(p, mileage=Range.model_validate({"to": 50}), minDiscount=20)
    with Session(p.engine) as db:
        db.get(MonitorMembership, 2).started_at = db.get(MonitorMembership, 1).started_at
        db.commit()
    drain(p)
    assert len(groups(p)) == 1
    p.calls.clear()
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert len(searches(p)) == 1
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (2, "124")).state == "checked"
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    wake(p)
    drain(p)
    assert len(p.sent) == 1


@pytest.mark.parametrize("change", [dict(brand="Audi"), dict(model="Passat"),
    dict(region="Вінницька область"), dict(price={"to": 3000}), dict(year={"from": 2010})])
def test_hard_discovery_constraints_never_merge(p, change):
    data = p.filters.model_dump(by_alias=True) | change
    other = Filters.model_validate(data)
    assert discovery_filters(other).fingerprint() != discovery_filters(p.filters).fingerprint()
    add_search(p, **{key: getattr(other, key) for key in change})
    sync(p)
    assert len(groups(p)) == 2


def test_optional_discovery_key_does_not_replace_valuation_key(p):
    other = p.filters.model_copy(update={"fuel": [], "transmission": ["Автомат"],
        "body": ["Хетчбек"], "mileage": Range.model_validate({"to": 50})})
    assert discovery_filters(other).fingerprint() == discovery_filters(p.filters).fingerprint()
    assert source_filters(other).fingerprint() != source_filters(p.filters).fingerprint()


def legacy_feeds(p):
    add_search(p, fuel=[])
    # Create the exact old feed layout without running the new merger.
    with Session(p.engine) as db:
        for search, _, member in active_members(db):
            db.add(MonitorFeed(id=member.feed_id, filters=source_filters(search.filters).canonical(),
                started_at=member.started_at, cursor=member.started_at + search.id * 30,
                checked_at=p.clock[0], next_poll=p.clock[0] + search.id * 60, status="watching"))
        db.commit()


@pytest.mark.parametrize("blocked", ["context", "quota_exceeded"])
def test_merge_waits_for_inflight_page_and_retry_gate(p, blocked):
    legacy_feeds(p)
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        if blocked == "context":
            feed.context = {"page": 2, "slices": [{"after": 1, "before": 2}]}
        else:
            feed.status = blocked
        db.commit()
    sync(p)
    assert len(groups(p)) == 2


def test_migration_keeps_earliest_cursor_epochs_interests_and_activation(p):
    legacy_feeds(p)
    with Session(p.engine) as db:
        before = {s.id: (w.epoch, m.started_at) for s, w, m in active_members(db)}
        cursor = min(f.cursor for f in db.scalars(select(MonitorFeed)))
        db.add(MonitorSeen(search_id=1, source_id="999", epoch=before[1][0],
                           state="sent", first_seen=p.clock[0]))
        db.commit()
    sync(p)
    sync(p)
    with Session(p.engine) as db:
        assert {s.id: (w.epoch, m.started_at) for s, w, m in active_members(db)} == before
        assert len(groups(p)) == 1
        assert db.get(MonitorFeed, next(iter(groups(p)))).cursor == cursor
        assert db.get(MonitorSeen, (1, "999")).state == "sent"
        search = db.get(Search, 2)
        search.enabled = False
        reset_watch(db, 2, False)
        db.commit()
    sync(p)
    with Session(p.engine) as db:
        assert db.get(MonitorMembership, 2) is None
        assert db.get(MonitorWatch, 2) is None


def test_supplemental_snapshot_must_converge_before_merging(p):
    legacy_feeds(p)
    p.runner.settings = replace(p.settings, ria_active_window_enabled=True)
    # Install the existing baseline marker first, then saved snapshots.
    from backend import active_window
    with Session(p.engine) as db:
        active_window.sync(db, groups(p))
        db.flush()
        for index, row in enumerate(db.scalars(select(MonitorActiveWindow))):
            row.window = ["123"] if index else ["124"]
            row.checked_at, row.status = p.clock[0], "watching"
        db.commit()
    sync(p)
    assert len(groups(p)) == 2
    with Session(p.engine) as db:
        for row in db.scalars(select(MonitorActiveWindow)):
            row.window = ["123"]
        db.commit()
    sync(p)
    assert len(groups(p)) == 1
    with Session(p.engine) as db:
        assert db.get(MonitorActiveWindow, next(iter(groups(p)))).window == ["123"]


def test_paid_shared_discovery_pagination_and_per_user_discount(p, monkeypatch):
    from backend.tests.test_ria_ai_price import enable
    quotes = enable(p, monkeypatch)
    add_search(p, fuel=[], minDiscount=40)
    drain(p)
    assert len(groups(p)) == 1
    p.ads.update({str(n): p.clock[0] + 1 for n in range(1000, 1057)})
    wake(p)
    assert p.runner.tick()
    # Restart with a saved page, preserving the already merged recipients.
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    for _ in range(60):
        p.runner.deliver_tick()
    assert len(quotes) == 57 and len(set(quotes)) == 57
    assert {(uid, car.source_id) for uid, car in p.sent} == {
        (111, str(n)) for n in range(1000, 1057)}
    assert len(p.sent) == 57
    assert {params["page"] for params in searches(p)} == {0, 1}
