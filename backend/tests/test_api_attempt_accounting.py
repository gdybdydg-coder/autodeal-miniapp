"""Fake transports only: account attempts without API keys or paid requests."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend import api_attempt_audit as audit, auto_ria, ria_ai_price
from backend.auto_ria import RiaError
from backend.models import Base, Filters, SourceBudget
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch, discovery_group_key, initialize_budget


@pytest.fixture
def source():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    initialize_budget(engine)
    clock = [1800000000.0]
    value = RiaSearch(engine, "test-secret-key", limits=BudgetLimits(100, 200, 500),
                      audit_clock=lambda: clock[0])
    value.acquire()
    yield value, clock
    value.release()
    engine.dispose()


def rows(source):
    with Session(source.engine) as db:
        return list(db.scalars(select(audit.RiaApiAttempt).order_by(audit.RiaApiAttempt.reserved_at)))


def opener(monkeypatch, module, responses):
    calls = []
    class Response:
        status = 200
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _):
            return json.dumps(self.payload).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(timeout)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return Response(response)
    monkeypatch.setattr(module, "build_opener", lambda *a: Opener())
    return calls


def test_cache_hit_has_no_attempt_and_successful_http_is_observed(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, auto_ria, [{"ok": True}])
    assert client.request("search", {}, lambda value: value) == {"ok": True}
    assert client.request("search", {}, lambda value: value) == {"ok": True}
    records = rows(client)
    assert calls == [8] and client.requests_made == 1 and len(records) == 1
    row = records[0]
    assert row.category == "search" and row.state == "success" and row.http_status == 200
    assert row.transport_started_at is not None and row.call_relation == "first_observed"
    with Session(client.engine) as db:
        result = audit.summary(db, 1800000000, 1800000001)
        assert result["reservations"] == result["transport_observed_attempts"] == 1
        assert result["provider_charged_units"] is None and result["cache_hits_in_attempts"] == 0
        assert result["transport_by_category"] == {"search": 1}
        assert result["cache_hit_calls"] is None


def test_failed_http_and_explicit_retry_have_status_and_relation(source, monkeypatch):
    client, clock = source
    calls = opener(monkeypatch, auto_ria, [HTTPError("private-url", 500, "private", {}, None), {}])
    with pytest.raises(RiaError, match="upstream_error"):
        client.request("info", {"auto_id": "123"}, lambda value: value)
    clock[0] += 1
    client.request("info", {"auto_id": "123"}, lambda value: value)
    a, b = rows(client)
    assert len(calls) == client.requests_made == 2
    assert a.state == "error" and a.http_status == 500 and a.error_code == "upstream_error"
    assert b.state == "success" and b.call_relation == "repeat_after_failure" and b.prior_attempt_id == a.id
    with Session(client.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 4  # Existing seed unchanged.


def test_attempt_crossing_window_boundary_uses_transport_timestamp(source):
    client, clock = source
    def crossing_boundary(key, path, params):
        clock[0] += 2
        audit.current_observer()({"event": "transport_started"})
        return {}
    client.fetch = crossing_boundary
    client.request("search", {}, lambda value: value)
    with Session(client.engine) as db:
        early = audit.summary(db, 1800000000, 1800000001)
        later = audit.summary(db, 1800000001, 1800000003)
    assert early["reservations"] == 1 and early["transport_observed_attempts"] == 0
    assert later["reservations"] == 0 and later["transport_observed_attempts"] == 1
    assert later["transport_by_category"] == {"search": 1}
    assert later["transport_call_relations"] == {"first_observed": 1}


def test_forced_successful_repeat_is_not_mislabelled_failure_retry(source, monkeypatch):
    client, clock = source
    calls = opener(monkeypatch, auto_ria, [{}, {}])
    client.request("search", {}, lambda value: value, force=True)
    clock[0] += 1
    client.request("search", {}, lambda value: value, force=True)
    assert len(calls) == 2 and rows(client)[1].call_relation == "repeat_after_success"
    assert all(row.forced for row in rows(client))


def test_same_clock_multiple_calls_keep_explicit_request_order(source, monkeypatch):
    client, _ = source
    opener(monkeypatch, auto_ria, [{}, {}, {}])
    for _ in range(3):
        client.request("search", {}, lambda value: value, force=True)
    with Session(client.engine) as db:
        records = list(db.scalars(select(audit.RiaApiAttempt).order_by(audit.RiaApiAttempt.request_ordinal)))
    assert [row.request_ordinal for row in records] == [1, 2, 3]
    assert records[2].prior_attempt_id == records[1].id


def test_quota_refusal_precedes_reservation_and_any_transport(source, monkeypatch):
    client, _ = source
    with Session(client.engine) as db:
        db.get(SourceBudget, "auto_ria").total = 500
        db.commit()
    opener(monkeypatch, auto_ria, [])
    with pytest.raises(RiaError, match="quota_exceeded"):
        client.request("search", {}, lambda value: value)
    assert rows(client) == [] and client.requests_made == 0


def test_parser_error_is_distinct_from_http_status_and_sanitized(source, monkeypatch):
    client, _ = source
    opener(monkeypatch, auto_ria, [{}])
    def parse(_):
        raise RiaError("secret-api_key=private-person-ID")
    with pytest.raises(RiaError):
        client.request("states", {}, parse)
    row = rows(client)[0]
    assert row.category == "catalog" and row.state == "error"
    assert row.http_status == 200 and row.error_code == "operation_error"
    with Session(client.engine) as db:
        text = json.dumps(audit.summary(db, 1800000000, 1800000001))
    assert "secret" not in text and "private" not in text and "test-secret-key" not in text


def test_injected_wrapper_failure_is_dispatch_without_observed_http(source):
    client, _ = source
    def before_http(*_):
        raise RiaError("validation_limit")
    client.fetch = before_http
    with pytest.raises(RiaError, match="validation_limit"):
        client.request("info", {"auto_id": "123"}, lambda value: value)
    row = rows(client)[0]
    assert row.dispatched_at is not None and row.transport_started_at is None
    assert row.http_status is None and row.state == "error"
    with Session(client.engine) as db:
        result = audit.summary(db, 1800000000, 1800000001)
    assert result["dispatched_operations"] == 1 and result["transport_observed_attempts"] == 0


def test_real_adapter_inside_legacy_wrapper_observes_http_without_kwarg_change(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, auto_ria, [{}])
    def old_wrapper(key, path, params):
        return auto_ria.fetch_json(key, path, params)
    client.fetch = old_wrapper
    client.request("search", {}, lambda value: value)
    row = rows(client)[0]
    assert calls == [8] and row.transport_started_at is not None and row.http_status == 200
    assert audit.current_observer() is None


def test_observer_context_never_leaks_to_an_independent_probe(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, auto_ria, [{}, {}])
    client.request("search", {}, lambda value: value)
    auto_ria.fetch_json("other-test-key", "search", {})
    assert calls == [8, 8] and len(rows(client)) == 1
    assert audit.current_observer() is None


def test_parallel_observer_contexts_stay_separate_and_reset():
    barrier = threading.Barrier(2)
    def run(n):
        callback = lambda event: n
        with audit.active_observer(callback):
            barrier.wait(timeout=2)
            assert audit.current_observer() is callback
        assert audit.current_observer() is None
        return n
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, [1, 2])) == [1, 2]
    assert audit.current_observer() is None


def test_missing_audit_table_aborts_before_any_http_without_fake_zeroes(source, monkeypatch):
    client, _ = source
    audit.RiaApiAttempt.__table__.drop(client.engine)
    calls = opener(monkeypatch, auto_ria, [])
    with pytest.raises(SQLAlchemyError):
        client.request("search", {}, lambda value: value)
    assert calls == [] and client.requests_made == 0
    with Session(client.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 2
        with pytest.raises(SQLAlchemyError):
            audit.summary(db, 1800000000, 1800000001)


def test_transport_timeout_has_no_invented_http_status(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, auto_ria, [URLError("private-host-name")])
    with pytest.raises(RiaError, match="connection_error"):
        client.request("search", {}, lambda value: value)
    row = rows(client)[0]
    assert len(calls) == 1 and row.transport_started_at is not None
    assert row.http_status is None and row.error_code == "connection_error"


def test_process_exit_retains_incomplete_attempt_and_never_retries(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, auto_ria, [SystemExit(1)])
    with pytest.raises(SystemExit):
        client.request("search", {}, lambda value: value)
    row = rows(client)[0]
    assert len(calls) == 1 and row.state == "transport_started" and row.finished_at is None
    with Session(client.engine) as db:
        assert audit.summary(db, 1800000000, 1800000001)["incomplete"] == 1


def test_ai_transport_uses_same_ledger_without_formula_change(source, monkeypatch):
    client, _ = source
    from backend.tests.test_ria_ai_price import wire
    calls = opener(monkeypatch, ria_ai_price, [wire(15000)])
    quote = client.market_range("123", "42")
    assert client.market_range("123", "42") == quote
    row = rows(client)[0]
    assert calls == [20] and row.category == "valuation" and row.http_status == 200
    assert quote["lower_usd"] == 14250 and quote["upper_usd"] == 15750


def test_pure_group_helper_removes_only_postfilter_constraints():
    a = Filters(brand="Volkswagen", model="Golf", fuel=["Дизель"], transmission=["Автомат"])
    b = Filters(brand="Volkswagen", model="Golf", fuel=["Бензин"], transmission=["Механіка"], minDiscount=30)
    assert a.fingerprint() != b.fingerprint()
    assert discovery_group_key(a) == discovery_group_key(b)
    assert discovery_group_key(a) != discovery_group_key(a.model_copy(update={"model": "Passat"}))
    assert discovery_group_key(a) != discovery_group_key(a.model_copy(update={"region": "Київська область"}))
