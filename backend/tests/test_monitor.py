import time
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import Settings
from backend.models import (Base, Delivery, Filters, MonitorControl, MonitorMatch,
                            MonitorSeen, MonitorWatch, Search, SourceBudget, SourceProbe, User)
from backend.monitor import Monitor, initialize, reset_watch
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch, initialize_budget
from backend.tests.test_ria_search import fixture_fetch, raw
from backend.worker import deliver_one, enqueue


@pytest.fixture
def pilot(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "monitor.db"))
    Base.metadata.create_all(engine)
    initialize_budget(engine)
    initialize(engine)
    settings = Settings("unused", "test-token", "x" * 32, True, True,
                        auto_ria_api_key="test-only", monitor_enabled=True)
    filters = Filters(brand="Volkswagen", model="Golf", region="Хмельницька область", fuel=["Дизель"])
    with Session(engine) as db:
        db.add(SourceProbe(id="telegram-webhook-v1", status="configured", checked_at=time.time(), result={}))
        db.add(User(id=111, ready=True))
        db.add(Search(id=1, user_id=111, name="Golf", filters=filters.canonical(),
                      fingerprint=filters.fingerprint(), enabled=True))
        db.flush()
        reset_watch(db, 1, True)
        db.commit()
    calls, sent, window = [], [], ["123"]
    base = fixture_fetch(calls)
    prices = {}
    def fetch(key, path, params):
        if path == "search":
            calls.append((path, params))
            ids = [str(n) for n in range(200, 205)] if "generation_id[0][0]" in params else list(window)
            return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
        if path == "info":
            calls.append((path, params))
            sid = params["auto_id"]
            data = raw(sid, USD=prices.get(sid, 15000 if int(sid) >= 200 else 10000))
            # Display labels differ from UI filters; IDs must be authoritative.
            data["autoData"]["fuelName"] = "Дизель, 2 л."
            return data
        return base(key, path, params)
    def factory(engine, key):
        return RiaSearch(engine, key, fetch=fetch, limits=BudgetLimits(900, 3000, 90000))
    def sender(uid, car):
        sent.append((uid, car))
        return {"ok": True, "result": {"message_id": 10}}
    monitor = Monitor(engine, settings, factory, sender)
    yield engine, settings, monitor, calls, sent, window, prices
    engine.dispose()


def due(engine):
    with Session(engine) as db:
        db.get(MonitorWatch, 1).next_poll = 0
        db.commit()


def test_baseline_restart_and_new_match_delivered_once(pilot):
    engine, settings, runner, calls, sent, window, _ = pilot
    runner.tick()
    assert not sent and not any(path == "info" for path, _ in calls)
    window.insert(0, "124")
    due(engine)
    restarted = Monitor(engine, settings, runner.search_factory, runner.sender)
    restarted.tick()
    assert len(sent) == 1 and sent[0][0] == 111
    assert sent[0][1].source_id == "124"
    assert sent[0][1].market == 15000 and sent[0][1].comparables == 5
    due(engine)
    restarted.tick()
    assert len(sent) == 1
    assert len([p for path, p in calls if path == "search" and "generation_id[0][0]" not in p]) == 3
    with Session(engine) as db:
        assert len(list(db.scalars(select(Delivery)))) == 1
        assert db.get(MonitorSeen, (1, "123")).state == "baseline"


def test_candidate_details_are_rechecked_despite_manual_cache(pilot):
    engine, _, runner, _, sent, window, prices = pilot
    runner.tick()
    source = runner.search_factory(engine, "test")
    source.acquire()
    source.car("124")  # Low price cached by an earlier manual search.
    source.release()
    prices["124"] = 30000
    window.insert(0, "124")
    due(engine)
    runner.tick()
    assert not sent


def test_stop_during_poll_invalidates_old_work(pilot):
    engine, _, runner, _, sent, window, _ = pilot
    runner.tick()
    window.insert(0, "124")
    factory = runner.search_factory
    def stop_on_info(engine, key):
        source = factory(engine, key)
        original = source.car
        def car(sid, **kwargs):
            if sid == "124":
                with Session(engine) as db:
                    db.get(Search, 1).enabled = False
                    reset_watch(db, 1, False)
                    db.commit()
            return original(sid, **kwargs)
        source.car = car
        return source
    runner.search_factory = stop_on_info
    due(engine)
    runner.tick()
    assert not sent
    with Session(engine) as db:
        assert not list(db.scalars(select(MonitorMatch)))


def test_quota_failure_preserves_cursor_and_makes_no_upstream_calls(pilot):
    engine, _, runner, calls, sent, window, _ = pilot
    runner.tick()
    before = len(calls)
    window.insert(0, "124")
    with Session(engine) as db:
        db.get(SourceBudget, "auto_ria").total = 90000
        db.commit()
    due(engine)
    runner.tick()
    assert len(calls) == before and not sent
    with Session(engine) as db:
        watch = db.get(MonitorWatch, 1)
        assert watch.status == "quota_exceeded" and watch.window == ["123"]
        assert watch.next_poll > time.time()


def test_full_window_gap_pauses_without_sending_backlog(pilot):
    engine, _, runner, calls, sent, window, _ = pilot
    window[:] = [str(n) for n in range(100, 150)]
    runner.tick()
    window[:] = [str(n) for n in range(300, 350)]
    due(engine)
    runner.tick()
    assert not sent and not any(path == "info" for path, _ in calls)
    with Session(engine) as db:
        assert db.get(MonitorWatch, 1).status == "window_gap"
    count = len(calls)
    runner.tick()
    assert len(calls) == count


def test_lease_excludes_second_process_and_disabled_does_not_fetch(pilot):
    engine, settings, runner, calls, sent, _, _ = pilot
    assert runner.claim()
    other = Monitor(engine, settings, runner.search_factory, runner.sender)
    other.tick()
    assert not calls and not sent
    runner.release("idle")
    other.settings = replace(settings, monitor_enabled=False)
    other.tick()
    assert not calls


def test_reenable_starts_new_baseline_and_stale_match_cannot_send(pilot):
    engine, settings, runner, _, sent, window, _ = pilot
    runner.tick()
    window.insert(0, "124")
    due(engine)
    runner.sender = lambda *_: {"error_code": 429, "parameters": {"retry_after": 1}}
    runner.tick()
    with Session(engine) as db:
        reset_watch(db, 1, True)
        db.commit()
    assert deliver_one(engine, settings, lambda *_: pytest.fail("old epoch"), now=time.time()+2) == "cancelled"
    runner.tick()
    assert not sent


def test_old_pending_candidates_are_expired_without_info_requests(pilot):
    engine, _, runner, calls, sent, window, _ = pilot
    runner.tick()
    with Session(engine) as db:
        watch = db.get(MonitorWatch, 1)
        db.add(MonitorSeen(search_id=1, source_id="124", epoch=watch.epoch,
                           state="pending", first_seen=time.time()-301))
        db.commit()
    window.insert(0, "124")
    due(engine)
    runner.tick()
    assert not sent and not any(path == "info" for path, _ in calls)
    with Session(engine) as db:
        assert db.get(MonitorSeen, (1, "124")).state == "expired"
