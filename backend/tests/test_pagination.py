import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app import Settings, create_app
from backend.auto_ria import RiaError
from backend.models import Filters, SourceCache
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch, PAGE_SIZE, PAGE_REQUEST_LIMIT
from backend.tests.test_ria_search import engine, raw

LIMITS = BudgetLimits(900, 3000, 90000)


def pages(calls, total=19):
    def fetch(key, path, params):
        calls.append((path, dict(params)))
        if path == "search":
            start = params["page"] * params["countpage"]
            return {"result": {"search_result": {"ids": [str(100+i) for i in range(start, min(total, start+params["countpage"]))], "count": total}}}
        assert path == "info"
        return raw(params["auto_id"], technicalCondition=None)
    return fetch


def test_more_than_three_pagination_cache_and_filter_binding(engine):
    calls = []
    fetch = pages(calls)
    filters = Filters(onlyDeals=False)
    search = lambda cursor=None, f=filters: RiaSearch(engine, "test-only", fetch, LIMITS).search(f, cursor)
    first = search()
    assert len(first["cars"]) == PAGE_SIZE and first["inspected"] == PAGE_SIZE
    assert first["next_cursor"]
    second = search(first["next_cursor"])
    assert not set(c["id"] for c in first["cars"]) & set(c["id"] for c in second["cars"])
    before = len(calls)
    cached = search(first["next_cursor"])
    assert cached["cached"] and cached["requests_used"] == 0 and len(calls) == before
    with pytest.raises(RiaError, match="search_expired"):
        search(first["next_cursor"], Filters(region="Хмельницька область"))
    assert len(calls) == before
    last = search(second["next_cursor"])
    assert len(last["cars"]) == 3 and last["next_cursor"] is None
    assert {c["id"] for d in (first, second, last) for c in d["cars"]} == set(map(str, range(100, 119)))
    assert [p["page"] for path, p in calls if path == "search"] == [0, 1, 2]


def test_interrupted_details_resume_without_skipping_an_id(engine):
    calls = []
    fetch_page = pages(calls, 8)
    failed = False
    def fetch(key, path, params):
        nonlocal failed
        if path == "info" and params["auto_id"] == "102" and not failed:
            failed = True
            raise RiaError("connection_error")
        return fetch_page(key, path, params)
    first = RiaSearch(engine, "test-only", fetch, LIMITS).search(Filters(onlyDeals=False))
    assert [c["id"] for c in first["cars"]] == ["100", "101"]
    second = RiaSearch(engine, "test-only", fetch, LIMITS).search(Filters(onlyDeals=False), first["next_cursor"])
    assert [c["id"] for c in second["cars"]] == list(map(str, range(102, 108)))
    assert second["next_cursor"] is None


def test_valuation_budget_is_bounded_and_continues_before_next_page(engine):
    calls = []
    def fetch(key, path, params):
        calls.append((path, params))
        if path == "search":
            modification = params.get("modifications[0][0][0]")
            ids = [str(modification*100+i) for i in range(6)] if modification else list(map(str, range(100, 108)))
            return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
        data = raw(params["auto_id"])
        data["autoData"]["modificationId"] = int(params["auto_id"])
        if int(params["auto_id"]) >= 10000:
            data["technicalCondition"] = None
        return data
    filters = Filters(onlyDeals=False)
    data = RiaSearch(engine, "test-only", fetch, LIMITS).search(filters)
    assert data["requests_used"] == PAGE_REQUEST_LIMIT and data["pending_valuations"] > 0
    assert len(data["cars"]) == PAGE_SIZE
    inspections = data["inspected"]
    states = {car["id"]: car for car in data["cars"]}
    attempts = 1
    while data["next_cursor"]:
        data = RiaSearch(engine, "test-only", fetch, LIMITS).search(filters, data["next_cursor"])
        assert data["requests_used"] <= PAGE_REQUEST_LIMIT
        states.update({car["id"]: car for car in data["cars"]})
        inspections += data["inspected"]
        attempts += 1
        assert attempts < 5
    assert inspections == PAGE_SIZE and len(states) == PAGE_SIZE
    assert all(car["valuation"] == "insufficient_data" for car in states.values())


def test_expired_cursor_never_spends_requests(engine):
    first = RiaSearch(engine, "test-only", pages([]), LIMITS).search(Filters(onlyDeals=False))
    with Session(engine) as db:
        db.get(SourceCache, "cursor-" + first["next_cursor"]).expires_at = time.time()-1
        db.commit()
    with pytest.raises(RiaError, match="search_expired"):
        RiaSearch(engine, "test-only", lambda *_: pytest.fail("network"), LIMITS).search(Filters(onlyDeals=False), first["next_cursor"])


def test_official_catalogs_are_complete_and_model_requests_are_shared(engine):
    calls = []
    def fetch(key, path, params):
        calls.append(path)
        if path == "categories/1/marks": return [{"name": "Toyota", "value": 79}, {"name": "Peugeot", "value": 58}]
        if path == "categories/1/marks/79/models": return [{"name": "Corolla", "value": 1}, {"name": "Camry", "value": 2}]
        return [{"name": "Офіційне значення", "value": 1}]
    root = RiaSearch(engine, "test-only", fetch, LIMITS).catalog()
    assert len(root["brands"]) == 2 and len(calls) == 5
    RiaSearch(engine, "test-only", fetch, LIMITS).catalog()
    assert len(calls) == 5
    result = RiaSearch(engine, "test-only", fetch, LIMITS).catalog("Toyota")
    assert [item["name"] for item in result["models"]] == ["Corolla", "Camry"] and len(calls) == 6
    RiaSearch(engine, "test-only", fetch, LIMITS).catalog("Toyota")
    assert len(calls) == 6


def test_catalog_and_continuation_require_telegram(engine):
    with TestClient(create_app(Settings(str(engine.url), "test-token", "x" * 32), engine)) as client:
        assert client.get("/api/catalog").status_code == 401
        assert client.get("/api/catalog?brand=Toyota").status_code == 401
        assert client.post("/api/cars/search?cursor=" + "a"*32, json={}).status_code == 401


def test_rollout_requires_explicit_opt_in_and_never_retries_a_failed_check(engine):
    from backend import ria_rollout
    from backend.models import SourceProbe
    calls = []
    def fail(*args):
        calls.append(1)
        raise RiaError("connection_error")
    ria_rollout.check_once(engine, "test-only", False, fail)
    assert calls == []
    ria_rollout.check_once(engine, "test-only", True, fail)
    ria_rollout.check_once(engine, "test-only", True, fail)
    assert calls == [1]
    with Session(engine) as db:
        row = db.get(SourceProbe, ria_rollout.PROBE_ID)
        assert row.requests == 1 and row.status == "connection_error"
