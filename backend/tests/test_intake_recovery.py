"""New regressions for quota pauses and durable unresolved public-detail work."""
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from backend import recent_publications as rp
from backend.auto_ria import RiaError
from backend.models import MonitorJob, MonitorSeen
from backend.monitor import Monitor
from backend.tests.test_monitor import p, drain, details
from backend.tests.test_recent_publications import setup, offer, probe, card


def take(p, source):
    assert p.runner.claim()
    try:
        return rp.take(p.runner, source)
    finally:
        p.runner.release("watching")


def fail(reason):
    def car(*args, **kwargs):
        raise RiaError(reason)
    return SimpleNamespace(car=car)


@pytest.mark.parametrize("reason", ["reserved_for_new_publications", "quota_exceeded", "busy", "search_limit"])
def test_capacity_pauses_do_not_consume_details_retry_budget_and_recover_after_restart(p, monkeypatch, reason):
    quotes = setup(p, monkeypatch)
    offer(p)
    assert p.runner.tick()  # Ordinary feed resolves its dictionaries first.
    before = len(p.calls)
    for _ in range(4):
        assert take(p, fail(reason))
        p.clock[0] += 61
    data = probe(p)
    assert "77" in data["pending"]
    assert data["pending"]["77"]["attempts"] == 0
    assert len(p.calls) == before and not quotes and not p.sent
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert len(details(p, "77")) == 1 and quotes == ["77"]
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "77")]


@pytest.mark.parametrize("reason", ["connection_error", "upstream_error", "invalid_response"])
def test_exhausted_details_are_retained_unresolved_without_seen_or_delivery(p, monkeypatch, reason):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    for _ in range(3):
        assert take(p, fail(reason))
        p.clock[0] += 121
    assert "77" not in probe(p)["pending"]
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        assert job is not None and job.state == "unresolved"
        assert job.reason == "details_retry_exhausted"
        assert job.result["detail_failure"] == reason
        assert job.result["detail_attempts"] == 3
        assert job.result["html_verified"] is False
        assert db.get(MonitorSeen, (1, "77")) is None
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert not p.sent
    assert rp.status(p.engine, p.settings)["unresolved_detail_jobs"] == 1


def test_temporary_invalid_details_retry_instead_of_final_rejection(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    assert take(p, fail("invalid_response"))
    assert probe(p)["pending"]["77"]["attempts"] == 1
    p.clock[0] += 61
    drain(p)
    assert len(details(p, "77")) == 1 and quotes == ["77"]
    assert len(p.sent) == 1


def test_zero_preview_price_is_unknown_until_authoritative_details(p, monkeypatch):
    setup(p, monkeypatch)
    added = offer(p)
    row = rp.parse(card("77", added, price=0))
    assert row == [{"id": "77", "added_at": int(added), "preview_usd": None}]


def test_expired_pending_details_are_retained_without_requesting_old_cars(p, monkeypatch):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    p.clock[0] += rp.MAX_AGE + 1
    assert take(p, SimpleNamespace(car=lambda *a, **k: pytest.fail("expired detail requested")))
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        assert job is not None and job.state == "unresolved"
        assert job.reason == "details_expired"
    assert not probe(p)["pending"] and not p.sent


@pytest.mark.parametrize("partial", [False, True])
def test_temporary_missing_filter_catalogue_defers_without_consuming_candidate(p, monkeypatch, partial):
    from backend.ria_search import RiaSearch
    from backend.tests.test_monitor import add_search
    if partial:
        add_search(p)
    quotes = setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    original = RiaSearch.cached_parameters
    resolved = []
    def missing(*args, **kwargs):
        if partial and not resolved:
            resolved.append(True)
            return original(*args, **kwargs)
        raise RiaError("catalog_not_cached")
    monkeypatch.setattr(RiaSearch, "cached_parameters", missing)
    assert take(p, SimpleNamespace(car=lambda *a, **k: pytest.fail("unresolved filters requested details")))
    assert probe(p)["pending"]["77"]["attempts"] == 0
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    monkeypatch.setattr(RiaSearch, "cached_parameters", original)
    p.clock[0] += 61
    drain(p)
    assert quotes == ["77"] and len(p.sent) == (2 if partial else 1)


def test_fresh_primary_discovery_can_recover_retained_unresolved_details(p, monkeypatch):
    from backend.tests.test_monitor import wake
    quotes = setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    for _ in range(3):
        assert take(p, fail("upstream_error"))
        p.clock[0] += 121
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").state == "unresolved"
    p.ads["77"] = p.clock[0] + 1  # Genuine primary publication evidence.
    wake(p, 61)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "77")]
    assert quotes == ["77"]
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        assert job.state == "checked"
        assert job.result["discovery_kind"] == "new_publication"
        assert job.result["detail_failure"] == "upstream_error"
        assert job.result["detail_attempts"] == 3
        assert db.get(MonitorSeen, (1, "77")) is not None


def test_unresolved_archive_conflict_preserves_primary_and_collector_transaction(p, monkeypatch):
    from backend.models import SourceProbe
    with Session(p.engine) as db, db.begin():
        db.add(MonitorJob(source_id="77", first_seen=p.clock[0], state="pending",
            result={"discovery_kind": "new_publication", "primary": "keep"}))
    with Session(p.engine) as db, db.begin():
        original = db.get
        monkeypatch.setattr(db, "get", lambda model, key, **kwargs:
            None if model is MonitorJob else original(model, key, **kwargs))
        # Simulate a primary job committing just after the initial missing check.
        rp.retain_unresolved(db, "77", {"added_at": p.clock[0], "attempts": 3},
            "details_retry_exhausted", "upstream_error")
        db.add(SourceProbe(id="fixture-collector-commit", status="watching",
            checked_at=p.clock[0], result={"saved": True}))
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").result == {"discovery_kind": "new_publication", "primary": "keep"}
        assert db.get(SourceProbe, "fixture-collector-commit").result["saved"] is True
