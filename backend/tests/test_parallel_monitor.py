"""Real overlapping source work with bounded capacity and shared durable caps."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import ria_ai_price as ai
from backend.auto_ria import RiaError
from backend.models import Delivery, MonitorControl, MonitorJob, MonitorMatch, Search, SourceBudget, User
from backend.monitor import Monitor, reset_watch
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch
from backend.tests.test_monitor import p, add_search, drain, wake, details
from backend.tests.test_ria_ai_price import enable, wire
from backend.tests.test_active_window import enable_window
from backend.worker import enqueue


def queue(p, count):
    p.ads.update({str(n): p.clock[0] + 1 for n in range(200, 200 + count)})
    wake(p)
    # A late subscriber can split the first frozen discovery window.
    for _ in range(4):
        assert p.runner.tick()
        with Session(p.engine) as db:
            if len(list(db.scalars(select(MonitorJob).where(MonitorJob.state == "pending")))) == count:
                return
    pytest.fail("new publications were not queued")


def dispatch(p, count=20):
    # The live dispatcher runs independently; the legacy drain helper sends
    # only one message per valuation tick and can stop with deliveries pending.
    for _ in range(count):
        p.runner.deliver_tick()


@pytest.mark.parametrize("active_supplement", [False, True])
def test_slow_quote_does_not_hold_other_cars_or_due_discovery(p, monkeypatch, active_supplement):
    if active_supplement:
        enable_window(p, monkeypatch)
    enable(p, monkeypatch)
    drain(p)
    queue(p, 3)
    started, release, fast, discovered = (threading.Event() for _ in range(4))
    def quote(key, uid, sid):
        if sid == "200":
            started.set()
            assert release.wait(5)
        else:
            fast.set()
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, "fetch_quote", quote)
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(key, path, params):
            result = original(key, path, params)
            if path == "search":
                discovered.set()
            return result
        source.fetch = fetch
        return source
    p.runner.search_factory = source_factory
    wake(p, 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert started.wait(2)
            assert fast.wait(2), "one slow quote blocks every other fresh car"
            assert discovered.wait(2), "valuation blocks due publication discovery"
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                p.runner.deliver_tick()
                if p.sent:
                    break
                time.sleep(.01)
            assert p.sent and all(car.source_id != "200" for _, car in p.sent)
        finally:
            release.set()
            assert work.result(timeout=5)
    drain(p)
    dispatch(p)
    assert sorted(car.source_id for _, car in p.sent) == ["200", "201", "202"]
    assert all(len(details(p, sid)) == 1 for sid in ("200", "201", "202"))


@pytest.mark.parametrize("active_supplement", [False, True])
def test_at_most_four_cars_and_no_second_monitor_can_duplicate_work(p, monkeypatch, active_supplement):
    if active_supplement:
        enable_window(p, monkeypatch)
    enable(p, monkeypatch)
    drain(p)
    queue(p, 8)
    entered, release, four = [], threading.Event(), threading.Event()
    lock = threading.Lock()
    def quote(key, uid, sid):
        with lock:
            entered.append(sid)
            if len(entered) == 4:
                four.set()
        assert release.wait(5)
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, "fetch_quote", quote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert four.wait(2), "fresh cars still run serially"
            assert len(entered) == 4
            other = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
            assert other.tick() is False
        finally:
            release.set()
            work.result(timeout=5)
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorJob).where(MonitorJob.state == "pending")))) == 4
    drain(p)
    dispatch(p)
    assert len(entered) == len(set(entered)) == 8
    assert len(p.sent) == 8


@pytest.mark.parametrize("active_supplement", [False, True])
def test_stop_during_parallel_quote_preserves_other_subscriber(p, monkeypatch, active_supplement):
    if active_supplement:
        enable_window(p, monkeypatch)
    add_search(p)
    enable(p, monkeypatch)
    drain(p)
    queue(p, 2)
    started, release = threading.Event(), threading.Event()
    def quote(key, uid, sid):
        started.set()
        assert release.wait(5)
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, "fetch_quote", quote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert started.wait(2)
            with Session(p.engine) as db:
                db.get(User, 111).ready = False
                db.get(Search, 1).enabled = False
                reset_watch(db, 1, False)
                db.commit()
        finally:
            release.set()
            work.result(timeout=5)
    drain(p)
    dispatch(p)
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(222, "200"), (222, "201")]
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorMatch).where(MonitorMatch.search_id == 1)) is None


@pytest.mark.parametrize("cap", ["hourly", "daily", "total"])
def test_parallel_clients_cannot_overspend_last_request(p, cap):
    limits = BudgetLimits(10, 20, 100)
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.total = 99 if cap == "total" else 30
        budget.calls = ([p.clock[0]] * 9 if cap == "hourly" else
                        [p.clock[0] - 4000] * 19 if cap == "daily" else [])
        initial = budget.total
        db.commit()
    calls = []
    parent = RiaSearch(p.engine, "test-only", lambda *args: calls.append(args[1]) or {}, limits)
    parent.acquire()
    try:
        children = [parent.fork() for _ in range(4)]
        barrier = threading.Barrier(4)
        def request(i):
            barrier.wait(timeout=2)
            try:
                children[i].request("info", {"auto_id": str(800 + i)}, lambda value: value, force=True)
                return "ok"
            except RiaError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(request, range(4)))
        assert results.count("ok") == 1 and results.count("quota_exceeded") == 3
        assert len(calls) == 1
        with Session(p.engine) as db:
            assert db.get(SourceBudget, "auto_ria").total == initial + 1
    finally:
        parent.release()


def test_parallel_cached_request_is_fetched_once_and_child_cannot_release_lease(p):
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(*args):
        calls.append(args[1])
        started.set()
        assert release.wait(5)
        return {"items": []}
    parent = RiaSearch(p.engine, "test-only", fetch, BudgetLimits(900, 12000, 90000))
    parent.acquire()
    try:
        children = [parent.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(child.request, "states", {}, lambda value: value) for child in children]
            try:
                assert started.wait(2)
                children[0].release()
                with pytest.raises(RiaError, match="busy"):
                    RiaSearch(p.engine, "test-only").acquire()
            finally:
                release.set()
            assert all(f.result(timeout=5) == {"items": []} for f in futures)
        assert calls == ["states"]
    finally:
        parent.release()


def test_children_share_provider_cooldown_and_cannot_use_released_lease(p):
    calls = []
    def fail(*args):
        calls.append(args[1])
        raise RiaError("quota_exceeded")
    parent = RiaSearch(p.engine, "test-only", fail, BudgetLimits(900, 12000, 90000))
    parent.acquire()
    child, other = parent.fork(), parent.fork()
    try:
        with pytest.raises(RiaError, match="quota_exceeded"):
            child.request("info", {"auto_id": "800"}, lambda value: value, force=True)
        with pytest.raises(RiaError, match="quota_exceeded"):
            other.request("info", {"auto_id": "801"}, lambda value: value, force=True)
        assert calls == ["info"]
    finally:
        parent.release()
    with pytest.raises(RiaError, match="search_limit"):
        child.request("info", {"auto_id": "802"}, lambda value: value, force=True)
    assert calls == ["info"]


def test_failed_operation_does_not_drop_or_block_other_jobs(p, monkeypatch):
    enable(p, monkeypatch)
    drain(p)
    queue(p, 3)
    def quote(key, uid, sid):
        if sid == "200":
            raise RuntimeError("private detail must never be logged")
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, "fetch_quote", quote)
    p.runner.tick()
    dispatch(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "200")
        assert job.state == "pending" and job.reason == "processing_error"
        assert job.next_run > p.clock[0]
    assert sorted(car.source_id for _, car in p.sent) == ["201", "202"]


def test_lost_monitor_lease_cannot_commit_or_release_replacement_owner(p, monkeypatch):
    enable(p, monkeypatch)
    drain(p)
    queue(p, 2)
    started, release = threading.Event(), threading.Event()
    def quote(key, uid, sid):
        started.set()
        assert release.wait(5)
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, "fetch_quote", quote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert started.wait(2)
            with Session(p.engine) as db:
                db.get(MonitorControl, "pilot").owner = "replacement"
                budget = db.get(SourceBudget, "auto_ria")
                budget.owner, budget.busy_until = "replacement", p.clock[0] + 90
                db.commit()
        finally:
            release.set()
            work.result(timeout=5)
    dispatch(p)
    with Session(p.engine) as db:
        assert db.get(MonitorControl, "pilot").owner == "replacement"
        assert db.get(SourceBudget, "auto_ria").busy_until > p.clock[0]
        assert list(db.scalars(select(MonitorMatch))) == []
        assert all(job.state == "pending" for job in db.scalars(select(MonitorJob)))
    assert not p.sent


def test_two_hundred_subscribers_share_four_parallel_valuations(p, monkeypatch):
    # Same activation boundary, no old ads or historical windows.
    p.clock[0] -= 2
    for sid in range(2, 201):
        add_search(p, sid=sid, uid=1000 + sid)
    p.clock[0] += 2
    quotes = enable(p, monkeypatch)
    drain(p)
    queue(p, 4)
    with Session(p.engine) as db:
        initial_calls = db.get(SourceBudget, "auto_ria").total
    p.runner.tick()
    enqueue(p.engine, allow_active_window=False, require_provider_range=True)
    enqueue(p.engine, allow_active_window=False, require_provider_range=True)
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total - initial_calls == 8
        pairs = list(db.execute(select(Delivery.user_id, Delivery.listing_id)))
        assert len(pairs) == len(set(pairs)) == 800
        assert all(job.state == "checked" for job in db.scalars(select(MonitorJob)))
    assert sorted(quotes) == ["200", "201", "202", "203"]
    assert all(len(details(p, sid)) == 1 for sid in quotes)
