"""Bounded detail retries using isolated SQLite and fixture-only transports."""
from sqlalchemy import select
from sqlalchemy.orm import Session

import pytest

from backend.auto_ria import RiaError
from backend.models import Delivery, Listing, MonitorJob, MonitorSeen, Search
from backend.monitor import Monitor
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_ai_price import enable


def retrying_details(p, reasons):
    """Fail fixture details before delegating successful responses to the fixture."""
    attempts, pending = [], iter(reasons)
    factory = p.runner.search_factory

    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch

        def get(key, path, params):
            if path == "info" and params["auto_id"] == "124":
                attempts.append(p.clock[0])
                reason = next(pending, None)
                if reason:
                    p.calls.append((path, dict(params)))
                    raise RiaError(reason)
            return fetch(key, path, params)

        source.fetch = get
        return source

    p.runner.search_factory = source_factory
    return attempts


def discover(p):
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    assert p.runner.tick()


def job(p):
    with Session(p.engine) as db:
        row = db.get(MonitorJob, "124")
        return row.state, row.reason, dict(row.result), row.next_run, row.attempts


@pytest.mark.parametrize("reason", ["connection_error", "upstream_error", "invalid_response"])
def test_one_temporary_detail_failure_recovers_delivers_and_clears_counter(p, monkeypatch, reason):
    quotes = enable(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        original = db.get(Search, 1).filters
    attempts = retrying_details(p, [reason])
    discover(p)
    assert p.runner.tick()
    state, why, evidence, due, _ = job(p)
    assert state == "pending" and why == reason
    assert evidence["detail_retry"]["consecutive_failures"] == 1
    assert due == p.clock[0] + 60
    assert not p.sent and not quotes
    p.clock[0] = due
    drain(p)
    assert len(attempts) == 2 and quotes == ["124"]
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    assert "detail_retry" not in job(p)[2]
    # The existing lower bound minus 5% remains the notification reference.
    assert p.sent[0][1].market == pytest.approx(15000 * .95 * .95)
    with Session(p.engine) as db:
        assert db.get(Search, 1).filters == original


@pytest.mark.parametrize("reason", ["connection_error", "upstream_error", "invalid_response"])
def test_persistent_detail_failure_stops_after_five_and_preserves_job_after_sixty_ticks(p, monkeypatch, reason):
    quotes = enable(p, monkeypatch)
    drain(p)
    attempts = retrying_details(p, [reason] * 100)
    discover(p)
    assert p.runner.tick()
    for count, delay in enumerate((60, 120, 240, 480), start=1):
        state, why, evidence, due, _ = job(p)
        assert state == "pending" and why == reason
        assert evidence["detail_retry"]["consecutive_failures"] == count
        assert due - evidence["detail_retry"]["last_failure_at"] == pytest.approx(delay)
        p.clock[0] = due - 1
        p.runner.tick()
        assert len(attempts) == count
        p.clock[0] = due
        drain(p)
    state, why, evidence, _, _ = job(p)
    assert state == "manual_review" and why == reason
    assert evidence["detail_retry"] == {
        "consecutive_failures": 5, "last_reason": reason,
        "last_failure_at": attempts[-1], "automatic_retries_exhausted": True,
    }
    original_proof = {key: value for key, value in evidence.items() if key != "detail_retry"}
    for _ in range(60):
        wake(p)
        drain(p)
    assert len(attempts) == 5 and not quotes and not p.sent
    with Session(p.engine) as db:
        kept = db.get(MonitorJob, "124")
        assert kept.state == "manual_review" and kept.reason == reason
        assert {key: value for key, value in kept.result.items() if key != "detail_retry"} == original_proof
        assert db.get(MonitorSeen, (1, "124")) is not None
        assert db.scalar(select(Delivery)) is None and db.scalar(select(Listing)) is None


def test_restart_retains_counter_and_wait_instead_of_renewing_retry_budget(p, monkeypatch):
    enable(p, monkeypatch)
    drain(p)
    attempts = retrying_details(p, ["connection_error"] * 100)
    discover(p)
    assert p.runner.tick()
    first = job(p)
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    p.clock[0] = first[3] - 1
    p.runner.tick()
    assert len(attempts) == 1 and job(p)[2]["detail_retry"]["consecutive_failures"] == 1
    p.clock[0] = first[3]
    drain(p)
    state, _, evidence, due, _ = job(p)
    assert state == "pending" and evidence["detail_retry"]["consecutive_failures"] == 2
    assert due == evidence["detail_retry"]["last_failure_at"] + 120


@pytest.mark.parametrize("reason", ["quota_exceeded", "busy", "search_limit", "no_eligible_subscription",
                                   "key_rejected", "access_denied", "reserved_for_new_publications"])
def test_non_detail_transport_waits_do_not_burn_failure_budget(p, reason):
    drain(p)
    discover(p)
    with Session(p.engine) as db:
        row = db.get(MonitorJob, "124")
        row.attempts = 99  # Total scheduling attempts are not detail failures.
        row.result = {**row.result, "detail_retry": {
            "consecutive_failures": 4, "last_reason": "connection_error",
            "last_failure_at": p.clock[0] - 1, "automatic_retries_exhausted": False}}
        db.commit()
    assert p.runner.claim()
    source = p.runner.search_factory(p.engine, "fixture-key")
    p.runner.defer("evaluate", "124", reason, source.limits, stage="info")
    p.runner.release("idle")
    state, why, evidence, _, total_attempts = job(p)
    assert state == "pending" and why == reason and total_attempts == 99
    assert evidence["detail_retry"]["consecutive_failures"] == 4


def test_catalog_error_during_evaluation_is_not_mislabelled_as_detail_failure(p):
    drain(p)
    discover(p)
    assert p.runner.claim()
    source = p.runner.search_factory(p.engine, "fixture-key")
    p.runner.defer("evaluate", "124", "upstream_error", source.limits, stage="categories/1/marks")
    p.runner.release("idle")
    state, why, evidence, due, _ = job(p)
    assert state == "pending" and why == "upstream_error"
    assert "detail_retry" not in evidence and due == p.clock[0] + 60


def test_successful_detail_clears_counter_even_if_a_later_catalog_step_fails(p, monkeypatch):
    enable(p, monkeypatch)
    drain(p)
    attempts = retrying_details(p, ["connection_error"])
    discover(p)
    assert p.runner.tick()
    due = job(p)[3]
    from backend.ria_search import RiaSearch
    parameters = RiaSearch.parameters

    def broken_parameters(source, filters):
        if source.stage == "info":
            source.stage = "categories/1/marks"
            raise RiaError("upstream_error")
        return parameters(source, filters)

    monkeypatch.setattr(RiaSearch, "parameters", broken_parameters)
    p.clock[0] = due
    assert p.runner.tick()
    state, why, evidence, _, _ = job(p)
    assert len(attempts) == 2 and state == "pending" and why == "upstream_error"
    assert "detail_retry" not in evidence and not p.sent
