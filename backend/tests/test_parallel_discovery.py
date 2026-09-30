"""Due feed overlap preserves deadlines, accounting and recipient epochs."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Delivery, MonitorFeed, MonitorJob, MonitorSeen, Range, Search, SourceBudget, User
from backend.monitor import Monitor, delivery_batch, reset_watch
from backend.ria_budget import BudgetLimits
from backend.tests.test_monitor import p, add_search, drain, wake, details, searches
from backend.tests.test_ria_ai_price import enable


def four_groups(p):
    p.clock[0] -= 2
    for n in range(2, 5):
        add_search(p, sid=n, uid=1000 + n, price=Range.model_validate({"from": n * 100}))
    p.clock[0] += 2


def test_four_due_searches_overlap_and_share_one_valuation(p, monkeypatch):
    four_groups(p)
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    entered, release, four = [], threading.Event(), threading.Event()
    lock = threading.Lock()
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def blocked(key, path, params):
            if path == "search":
                with lock:
                    entered.append(dict(params))
                    if len(entered) == 4:
                        four.set()
                assert release.wait(5)
            return fetch(key, path, params)
        source.fetch = blocked
        return source
    p.runner.search_factory = source_factory
    before = len(searches(p))
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert four.wait(2), "due feeds still run one at a time"
            assert len(entered) == 4
            other = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
            assert other.tick() is False
        finally:
            release.set()
            assert work.result(timeout=5)
    assert len(searches(p)) - before == 4
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorJob)))) == 1
        assert len(list(db.scalars(select(MonitorSeen)))) == 4
        checkpoints = {r.id: (r.cursor, r.next_poll) for r in db.scalars(select(MonitorFeed))}
        assert all(cursor <= p.clock[0] < deadline for cursor, deadline in checkpoints.values())
    # Finishing valuation and delivery does not repoll a feed before its deadline.
    drain(p)
    for _ in range(4):
        asyncio.run(delivery_batch(p.engine, p.settings, p.runner.sender))
    assert len(searches(p)) - before == 4
    assert quotes == ["124"] and len(details(p, "124")) == 1
    assert not details(p, "123")
    assert {(uid, car.source_id) for uid, car in p.sent} == {
        (111, "124"), (1002, "124"), (1003, "124"), (1004, "124")}
    assert len(p.sent) == 4


@pytest.mark.parametrize("cap", ["hourly", "daily", "total"])
def test_four_due_feeds_share_last_request_and_keep_unsearched_cursors(p, monkeypatch, cap):
    four_groups(p)
    enable(p, monkeypatch)
    drain(p)
    limits = BudgetLimits(10, 20, 100)
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        source.limits = limits
        return source
    p.runner.search_factory = source_factory
    wake(p)
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.total = 99 if cap == "total" else 30
        budget.calls = ([p.clock[0]] * 9 if cap == "hourly" else
                        [p.clock[0] - 4000] * 19 if cap == "daily" else [])
        initial = budget.total
        cursors = {r.id: r.cursor for r in db.scalars(select(MonitorFeed))}
        db.commit()
    before = len(searches(p))
    assert p.runner.tick()
    assert len(searches(p)) - before == 1 and not p.sent
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == initial + 1
        feeds = list(db.scalars(select(MonitorFeed)))
        advanced = [r for r in feeds if r.cursor > cursors[r.id]]
        deferred = [r for r in feeds if r.cursor == cursors[r.id]]
        assert len(advanced) == 1 and len(deferred) == 3
        assert all(r.status == "quota_exceeded" and r.context for r in deferred)


def test_stop_during_four_searches_cannot_restore_stopped_epoch(p, monkeypatch):
    four_groups(p)
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    entered, release = threading.Barrier(5), threading.Event()
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def blocked(key, path, params):
            if path == "search":
                entered.wait(timeout=5)
                assert release.wait(5)
            return fetch(key, path, params)
        source.fetch = blocked
        return source
    p.runner.search_factory = source_factory
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            entered.wait(timeout=5)
            with Session(p.engine) as db:
                db.get(User, 111).ready = False
                db.get(Search, 1).enabled = False
                reset_watch(db, 1, False)
                db.commit()
        finally:
            release.set()
            assert work.result(timeout=5)
    drain(p)
    for _ in range(4):
        asyncio.run(delivery_batch(p.engine, p.settings, p.runner.sender))
    assert quotes == ["124"]
    assert {(uid, car.source_id) for uid, car in p.sent} == {
        (1002, "124"), (1003, "124"), (1004, "124")}
    with Session(p.engine) as db:
        assert db.get(User, 111).ready is False
        assert db.get(Search, 1).enabled is False
        assert db.scalar(select(MonitorSeen).where(MonitorSeen.search_id == 1)) is None
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)) is None
