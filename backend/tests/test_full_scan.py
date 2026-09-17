import time
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import full_scan
from backend.app import Settings, create_app
from backend.auto_ria import RiaError
from backend.models import Delivery, Filters, FullScan, Listing, ScanItem, SourceBudget
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch
from backend.tests.test_backend import TOKEN, SECRET, headers
from backend.tests.test_ria_search import engine, raw

LIMITS = BudgetLimits(900, 3000, 90000)


def provider(calls, total=123):
    def fetch(key, path, params):
        calls.append((path, dict(params)))
        if path == "search":
            start = params["page"] * params["countpage"]
            return {"result": {"search_result": {"ids": list(map(str, range(100 + start, 100 + min(total, start + params["countpage"])))), "count": total}}}
        return raw(params["auto_id"], technicalCondition=None)
    return fetch


def start(engine, filters=None, uid=111, restart=False):
    with Session(engine) as db:
        scan_id = full_scan.start(db, uid, filters or Filters(onlyDeals=False), restart)
        db.commit()
    return scan_id


def status(engine, scan_id, **kwargs):
    with Session(engine) as db:
        return full_scan.view(db, 111, scan_id, **kwargs)


def ready(engine, scan_id):
    with Session(engine) as db:
        db.get(FullScan, scan_id).next_run = 0
        db.commit()


def runner(engine, fetch, limits=LIMITS):
    return full_scan.Scanner(engine, "test-only", lambda e, k: RiaSearch(e, k, fetch, limits))


def finish(engine, scan_id, scan):
    for _ in range(50):
        ready(engine, scan_id)
        scan.tick()
        result = status(engine, scan_id)
        if result["status"] not in full_scan.ACTIVE:
            return result
    raise AssertionError("Scan did not finish")


def test_all_123_listings_across_pages_are_checked_without_manual_cursors(engine):
    calls = []
    scan_id = start(engine)
    result = finish(engine, scan_id, runner(engine, provider(calls)))
    assert result["complete"] and result["inspected"] == result["discovered"] == result["source_total"] == 123
    assert result["more_results"] and len(result["cars"]) == 100
    rest = status(engine, scan_id, after=result["after"])
    assert len(rest["cars"]) == 23 and not rest["more_results"]
    assert {c["id"] for c in result["cars"] + rest["cars"]} == set(map(str, range(100, 223)))
    assert [p["page"] for path, p in calls if path == "search"] == [0, 1, 2]
    assert [path for path, _ in calls[:3]] == ["search", "info", "search"]
    assert len([1 for path, _ in calls if path == "info"]) == 123
    before = len(calls)
    status(engine, scan_id);status(engine, scan_id, only_deals=True)
    runner(engine, provider(calls)).tick()
    assert len(calls) == before  # Reads and completed jobs spend no provider calls.
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Delivery)) == 0
        assert db.scalar(select(func.count()).select_from(Listing)) == 0


def test_restart_after_network_error_preserves_checked_ids_and_retries_failed_id(engine):
    calls, failed = [], False
    base = provider(calls, 65)
    def fetch(key, path, params):
        nonlocal failed
        if path == "info" and params["auto_id"] == "117" and not failed:
            failed = True
            raise RiaError("connection_error")
        return base(key, path, params)
    scan_id = start(engine)
    runner(engine, fetch).tick()
    first = status(engine, scan_id)
    assert first["status"] == "waiting" and first["inspected"] == 17
    result = finish(engine, scan_id, runner(engine, fetch))  # New process/worker instance.
    assert result["complete"] and result["inspected"] == 65
    assert len([1 for path, p in calls if path == "info" and p["auto_id"] == "116"]) == 1
    assert any(c["id"] == "117" for c in result["cars"])


def test_daily_quota_resumes_next_day_without_a_fifteen_minute_cursor(engine, monkeypatch):
    calls = []
    scan_id = start(engine)
    limits = BudgetLimits(15, 15, 200)
    scan = runner(engine, provider(calls, 20), limits)
    scan.tick()
    first = status(engine, scan_id)
    assert first["status"] == "waiting" and 0 < first["inspected"] < 20
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 86401)
    result = finish(engine, scan_id, runner(engine, provider(calls, 20), limits))
    assert result["complete"] and result["inspected"] == 20
    assert len([1 for path, _ in calls if path == "info"]) == 20
    stale = next(c for c in result["cars"] if c["id"] == "100")
    assert stale["stale"] and stale["market"] is None


def test_total_budget_exhaustion_does_not_reset_or_retry_automatically(engine):
    calls = []
    scan_id = start(engine)
    scan = runner(engine, provider(calls), BudgetLimits(10, 10, 10))
    scan.tick()
    assert status(engine, scan_id)["status"] == "budget_exhausted"
    before = len(calls)
    ready(engine, scan_id);scan.tick()
    assert len(calls) == before
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 10


def test_duplicate_start_toggle_and_pause_keep_one_owned_job(engine):
    filters = Filters(brand="Volkswagen", onlyDeals=True)
    scan_id = start(engine, filters)
    assert start(engine, filters.model_copy(update={"onlyDeals": False})) == scan_id
    other = start(engine, uid=222)
    with Session(engine) as db:
        assert full_scan.change(db, 222, scan_id, False) is False
        assert full_scan.change(db, 111, scan_id, False)
        db.commit()
    assert status(engine, scan_id)["status"] == "paused"
    new = start(engine, Filters(region="Київська область"))
    with Session(engine) as db:
        full_scan.change(db, 111, scan_id, True)
        db.commit()
        assert db.get(FullScan, new).status == "paused"
        assert db.get(FullScan, other).status == "queued"


def test_expired_worker_lease_is_reclaimed_and_previous_owner_cannot_write(engine):
    scan_id = start(engine)
    first, second = runner(engine, provider([])), runner(engine, provider([]))
    assert first.claim() == scan_id and second.claim() is None
    with Session(engine) as db:
        db.get(FullScan, scan_id).lease_until = time.time() - 1
        db.commit()
    assert second.claim() == scan_id
    with Session(engine) as db:
        assert first.current(db, scan_id) is None
        assert second.current(db, scan_id) is not None


def test_incomplete_or_repeated_provider_pages_never_claim_all_checked(engine):
    calls = []
    base = provider(calls, 60)
    def fetch(key, path, params):
        if path == "search":
            return base(key, path, {**params, "page": 0})
        return base(key, path, params)
    scan_id = start(engine)
    result = finish(engine, scan_id, runner(engine, fetch))
    assert result["status"] == "incomplete" and not result["complete"]
    assert result["inspected"] == 50 and result["source_total"] == 60
    assert result["error"] == "pagination_incomplete"


def test_deal_beyond_first_page_is_found_and_old_discounts_are_not_current(engine, monkeypatch):
    calls = []
    base = provider(calls, 65)
    def fetch(key, path, params):
        if path == "search" and "generation_id[0][0]" in params:
            return {"result": {"search_result": {"ids": list(map(str, range(500, 505))), "count": 5}}}
        if path == "info" and (params["auto_id"] == "160" or int(params["auto_id"]) >= 500):
            return raw(params["auto_id"], USD=8700 if params["auto_id"] == "160" else 11700)
        return base(key, path, params)
    scan_id = start(engine)
    finish(engine, scan_id, runner(engine, fetch))
    result = status(engine, scan_id, only_deals=True)
    assert len(result["cars"]) == 1 and result["cars"][0]["id"] == "160"
    assert result["cars"][0]["market"] == 11700
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 901)
    old = status(engine, scan_id, only_deals=True)["cars"][0]
    assert old["stale"] and old["historical_match"]
    assert old["market"] is None and old["discount"] is None


def test_scan_api_requires_telegram_ownership_and_get_never_launches_work(engine):
    settings = Settings(str(engine.url), TOKEN, SECRET, auto_ria_api_key="test-only", full_scan_enabled=True)
    # No lifespan here: injected local worker is driven by tests, never real HTTP.
    client = TestClient(create_app(settings, engine))
    assert client.post("/api/cars/scans", json={}).status_code == 401
    before = None
    with Session(engine) as db:
        before = db.get(SourceBudget, "auto_ria").total
    result = client.post("/api/cars/scans", json={}, headers=headers())
    assert result.status_code == 200
    scan_id = result.json()["scan_id"]
    path = "/api/cars/scans/" + scan_id
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(222)).status_code == 404
    assert client.patch(path, json={"enabled": False}, headers=headers(222)).status_code == 404
    assert client.get(path + "?after=-1", headers=headers()).status_code == 422
    assert client.get(path, headers=headers()).json()["inspected"] == 0
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before
    offline = TestClient(create_app(replace(settings, auto_ria_api_key=""), engine))
    assert offline.post("/api/cars/scans", json={}, headers=headers()).status_code == 503


def test_restart_changes_generation_and_does_not_reuse_old_progress(engine):
    scan_id = start(engine)
    done = finish(engine, scan_id, runner(engine, provider([], 3)))
    assert start(engine) == scan_id
    assert status(engine, scan_id)["generation"] == done["generation"]
    assert start(engine, restart=True) == scan_id
    fresh = status(engine, scan_id)
    assert fresh["generation"] != done["generation"] and fresh["inspected"] == 0 and fresh["cars"] == []


def test_pause_during_network_call_stops_before_another_listing_and_can_resume(engine):
    calls = []
    scan_id = start(engine)
    base = provider(calls, 12)
    paused = False
    def fetch(key, path, params):
        nonlocal paused
        if path == "info" and not paused:
            paused = True
            with Session(engine) as db:
                full_scan.change(db, 111, scan_id, False)
                db.commit()
        return base(key, path, params)
    runner(engine, fetch).tick()
    assert status(engine, scan_id)["status"] == "paused"
    assert len([1 for path, _ in calls if path == "info"]) == 1
    with Session(engine) as db:
        full_scan.change(db, 111, scan_id, True)
        db.commit()
    result = finish(engine, scan_id, runner(engine, fetch))
    assert result["complete"] and result["inspected"] == 12
    assert len([1 for path, _ in calls if path == "info"]) == 12


def test_first_result_is_available_before_the_rest_of_a_large_catalog(engine):
    calls = []
    scan_id = start(engine)
    base = provider(calls, 35156)
    def fetch(key, path, params):
        if path == "search" and params["page"] == 1:
            result = status(engine, scan_id)
            assert result["inspected"] == 1 and result["cars"][0]["id"] == "100"
            assert result["discovered"] == 50 and not result["complete"]
        return base(key, path, params)
    runner(engine, fetch).tick()
    result = status(engine, scan_id)
    assert result["inspected"] >= 1 and result["phase"] == "discovering"
    assert 50 < result["discovered"] < result["source_total"] == 35156


def test_another_search_reads_cached_cars_immediately_and_reuses_fresh_valuations(engine):
    calls = []
    scan_id = start(engine)
    finish(engine, scan_id, runner(engine, provider(calls, 3)))
    before = len(calls)
    new = start(engine, Filters(price={"to": 12000}, onlyDeals=False))
    result = status(engine, new)
    assert result["inspected"] == 0 and result["cars"] == []
    assert {c["id"] for c in result["cache"]["cars"]} == {"100", "101", "102"}
    assert result["cache"]["coverage"] == "partial" and len(calls) == before
    result = finish(engine, new, runner(engine, provider(calls, 3)))
    assert result["complete"] and result["inspected"] == 3
    assert [path for path, _ in calls[before:]] == ["search"]


def test_refresh_removes_a_cached_car_that_no_longer_matches(engine, monkeypatch):
    filters = Filters(price={"to": 12000}, onlyDeals=False)
    scan_id = start(engine, filters)
    finish(engine, scan_id, runner(engine, provider([], 1)))
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 901)
    start(engine, filters, restart=True)
    base = provider([], 1)
    def fetch(key, path, params):
        return raw(params["auto_id"], USD=20000, technicalCondition=None) if path == "info" else base(key, path, params)
    result = finish(engine, scan_id, runner(engine, fetch))
    assert result["cars"] == result["cache"]["cars"] == []
    assert [entry["id"] for entry in result["removed"]] == ["100"]


def test_old_scan_uses_newer_shared_details_and_cannot_resurrect_removed_cars(engine):
    from backend import market_cache
    scan_id = start(engine, Filters(price={"to": 12000}, onlyDeals=False))
    finish(engine, scan_id, runner(engine, provider([], 1)))
    with Session(engine) as db:
        cached, checked_at = market_cache.fresh(db, "100")
        market_cache.put(db, {**cached, "price_usd": 20000}, checked_at + 1)
        db.commit()
    result = status(engine, scan_id)
    assert result["cars"] == [] and result["removed"][0]["id"] == "100"
    with Session(engine) as db:
        market_cache.discard(db, "100")
        db.commit()
    assert status(engine, scan_id)["cars"] == []


def test_retiring_full_scans_stops_workers_preserves_results_and_blocks_old_clients(engine):
    scan_id = start(engine)
    scanner = runner(engine, provider([], 500))
    scanner.tick()
    before = status(engine, scan_id)
    assert before["inspected"] > 0 and before["status"] in full_scan.ACTIVE
    full_scan.pause_all(engine)
    assert scanner.claim() is None
    settings = Settings(str(engine.url), TOKEN, SECRET, auto_ria_api_key="test-only")
    client = TestClient(create_app(settings, engine))
    assert client.post("/api/cars/scans", json={}, headers=headers()).json()["detail"] == "full_scan_disabled"
    assert client.post("/api/cars/search", json={}, headers=headers()).status_code == 409
    assert client.patch("/api/cars/scans/"+scan_id, json={"enabled": True}, headers=headers()).status_code == 409
    data = client.get("/api/cars/scans/"+scan_id, headers=headers()).json()
    assert data["status"] == "paused" and data["inspected"] == before["inspected"]
    assert data["cars"] == before["cars"]
    assert client.get("/api/source-status").json()["full_scan"] == {"enabled": False, "active_jobs": 0}
