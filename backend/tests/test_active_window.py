from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import active_window
from backend.models import (Delivery, MonitorActiveWindow, MonitorControl, MonitorFeed,
                            MonitorJob, MonitorSeen, MonitorWatch, Range, Search, SourceBudget)
from backend.monitor import Monitor, reset_watch, runtime_status
from backend.tests.test_monitor import p, drain, wake, details, add_search


def enable_window(p, monkeypatch):
    for name, value in (("HOURLY", 900), ("DAILY", 3000), ("TOTAL", 90000)):
        monkeypatch.setenv("RIA_REQUESTS_" + name + "_CAP", str(value))
    p.stock = ["123"]
    p.settings = replace(p.settings, ria_active_window_enabled=True)
    p.runner.settings = p.settings
    factory = p.runner.search_factory
    def with_stock(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            if path == "search" and "generation_id[0][0]" not in params and "published_after" not in params:
                p.calls.append((path, dict(params)))
                assert params["page"] == 0 and params["countpage"] == 50
                return {"result": {"search_result": {"ids": p.stock[:50], "count": len(p.stock)}}}
            return original(api_key, path, params)
        source.fetch = fetch
        return source
    p.runner.search_factory = with_stock


def test_old_active_listing_is_checked_without_claiming_it_is_new(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "123")]
    assert p.sent[0][1].pipeline["discovery_kind"] == "active_window"
    assert p.sent[0][1].market == 15000
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    wake(p, 301)
    drain(p)
    assert len(p.sent) == 1 and len(details(p, "123")) == 1


def test_first_page_and_one_candidate_are_hard_limits_not_a_catalog_scan(p, monkeypatch):
    enable_window(p, monkeypatch)
    p.stock = [str(n) for n in range(1000, 1100)]
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["1000"]
    with Session(p.engine) as db:
        window = db.scalar(select(MonitorActiveWindow))
        assert len(window.window) == 50 and window.source_total == 100
        assert window.status == "limited_window"
        assert len(list(db.scalars(select(MonitorJob)))) == 1
    wake(p, 301)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["1000", "1001"]
    state = runtime_status(p.engine, True, 111, active_window_enabled=True)["active_window"]
    assert state["coverage"] == "latest_active_window_only" and not state["historical_pagination"]
    assert "1000" not in str(state)
    assert not runtime_status(p.engine, True, 999, active_window_enabled=True)["active_window"]["state_counts"]


def test_price_drop_on_seen_non_deal_can_qualify_once(p, monkeypatch):
    enable_window(p, monkeypatch)
    p.prices["123"] = 16000
    drain(p)
    assert not p.sent
    p.prices["123"] = 10000
    wake(p, 301)
    drain(p)
    assert not p.sent and len(details(p, "123")) == 1
    wake(p, 1801)
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][1].price == 10000
    wake(p, 1801)
    drain(p)
    assert len(p.sent) == 1


def test_supplement_pauses_before_consuming_reserved_primary_budget(p, monkeypatch):
    enable_window(p, monkeypatch)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [p.clock[0]] * 430
        db.commit()
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124"]
    assert not details(p, "123")
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorActiveWindow)).status == "reserved_for_new_publications"
    assert not any(path == "search" and "published_after" not in params
                   and "generation_id[0][0]" not in params for path, params in p.calls)


def test_new_arrival_is_evaluated_before_older_supplemental_job(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="999", first_seen=p.clock[0] - 100,
                          result={"discovery_kind": "active_window"}))
        db.add(MonitorSeen(search_id=1, source_id="999", epoch=db.get(MonitorWatch, 1).epoch,
                           state="pending", first_seen=p.clock[0] - 100))
        db.get(MonitorControl, "pilot").status = "idle"
        db.commit()
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["123", "124", "999"]


def test_stop_during_supplemental_fetch_cannot_create_interest(p, monkeypatch):
    enable_window(p, monkeypatch)
    factory = p.runner.search_factory
    def stopped(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            if path == "search" and "published_after" not in params and "generation_id[0][0]" not in params:
                with Session(p.engine) as db:
                    db.get(Search, 1).enabled = False
                    reset_watch(db, 1, False)
                    db.commit()
            return original(api_key, path, params)
        source.fetch = fetch
        return source
    p.runner.search_factory = stopped
    drain(p)
    assert not p.sent and not details(p, "123")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "123") is None


@pytest.mark.parametrize("first_delivery", ["sent", "uncertain"])
@pytest.mark.parametrize("second_active", [True, False])
def test_shared_informational_job_refreshes_only_unsent_current_interests(
        p, monkeypatch, first_delivery, second_active):
    enable_window(p, monkeypatch)
    p.stock = ["124"]
    add_search(p, sid=2, uid=222, price=Range.model_validate({"to": 5000}))
    p.ads["124"] = p.clock[0] + 1
    p.clock[0] += 2
    factory, sender = p.runner.search_factory, p.runner.sender
    def information_only(engine, key):
        source = factory(engine, key)
        source.comparisons = lambda _: []
        return source
    def send(uid, car):
        result = sender(uid, car)
        return {"uncertain": True} if uid == 111 and first_delivery == "uncertain" else result
    p.runner.search_factory, p.runner.sender = information_only, send
    # Both searches observed the same ID before evaluation. Search indexing can
    # lag the fresh detail price, so the second search must reject USD 10,000.
    assert p.runner.claim()
    try:
        p.runner.sync()
        with Session(p.engine) as db:
            feeds = list(db.scalars(select(MonitorFeed.id)))
        for feed_id in feeds:
            source = p.runner.search_factory(p.engine, "test-only")
            source.acquire()
            try:
                p.runner.discover(feed_id, source, 60)
            finally:
                source.release()
    finally:
        p.runner.release("discover")
    drain(p)
    assert [(uid, car.price) for uid, car in p.sent] == [(111, 10000)]
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "informational"
        assert db.get(MonitorSeen, (2, "124")).state == "informational"
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == first_delivery
        if not second_active:
            db.get(Search, 2).enabled = False
            reset_watch(db, 2, False)
            db.commit()
    p.prices["124"] = 4000
    wake(p, 1801)
    drain(p)
    expected = [(111, 10000), (222, 4000)] if second_active else [(111, 10000)]
    assert [(uid, car.price) for uid, car in p.sent] == expected
    wake(p, 1801)
    drain(p)
    assert [(uid, car.price) for uid, car in p.sent] == expected
