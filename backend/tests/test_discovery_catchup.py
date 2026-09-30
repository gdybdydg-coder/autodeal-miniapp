"""Error recovery finishes its frozen window before catching up, without gaps."""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auto_ria import RiaError
from backend.models import MonitorFeed, MonitorJob, MonitorWatch, Search, SourceBudget, User
from backend.monitor import delivery_batch, reset_watch
from backend.tests.test_monitor import p, drain, wake, details, searches
from backend.tests.test_ria_ai_price import enable


def fail_first_search(p, error):
    factory = p.runner.search_factory
    failed = []
    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def call(key, path, params):
            if path == "search" and not failed:
                failed.append(dict(params))
                raise RiaError(error)
            return fetch(key, path, params)
        source.fetch = call
        return source
    p.runner.search_factory = source_factory
    return failed


@pytest.mark.parametrize("error", ["connection_error", "upstream_error", "quota_exceeded"])
def test_recovered_stale_window_catches_up_immediately_without_rechecking_old_ads(p, monkeypatch, error):
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    failed = fail_first_search(p, error)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        initial_budget = db.get(SourceBudget, "auto_ria").total
    assert p.runner.tick()
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        frozen_end = feed.context["finish_at"]
        original_cursor = feed.cursor
        assert feed.status == error and feed.next_poll > p.clock[0]
        assert db.get(SourceBudget, "auto_ria").total == initial_budget + 1
    p.clock[0] += 3700 if error == "quota_exceeded" else 180
    p.ads["125"] = p.clock[0] - 1
    # No wake or checkpoint edit: recover the exact window that failed.
    assert p.runner.tick()
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.cursor == frozen_end > original_cursor and feed.context == {}
        assert feed.next_poll == p.clock[0]
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(MonitorJob, "124").state == "pending"
        assert db.get(MonitorJob, "125") is None
    assert searches(p)[-1] == failed[0]
    drain(p)
    for _ in range(4):
        asyncio.run(delivery_batch(p.engine, p.settings, p.runner.sender))
    assert sorted(quotes) == ["124", "125"]
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "124"), (111, "125")]
    assert len(details(p, "124")) == len(details(p, "125")) == 1
    assert not details(p, "123")


def test_stop_during_error_wait_cannot_restart_the_preserved_search(p, monkeypatch):
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    fail_first_search(p, "connection_error")
    assert p.runner.tick()
    with Session(p.engine) as db:
        db.get(User, 111).ready = False
        db.get(Search, 1).enabled = False
        reset_watch(db, 1, False)
        db.commit()
    p.clock[0] += 180
    before = len(p.calls)
    drain(p)
    assert len(p.calls) == before and not quotes and not p.sent
    with Session(p.engine) as db:
        assert db.get(Search, 1).enabled is False
        assert db.get(MonitorWatch, 1) is None
        assert db.get(MonitorJob, "124") is None
