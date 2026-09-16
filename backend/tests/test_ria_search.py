import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import Settings, create_app
from backend.auto_ria import RiaError
from backend.models import Base, Filters, SourceBudget, SourceCache
from backend.ria_search import RiaSearch, estimate, initialize_budget, matches, parse_car


def raw(source_id="123", **overrides):
    data = {"title": "Volkswagen Golf", "USD": 10000, "markId": 84, "modelId": 30,
            "markName": "Volkswagen", "modelName": "Golf", "subCategoryName": "Хетчбек",
            "linkToView": f"/auto_volkswagen_golf_{source_id}.html",
            "stateData": {"stateId": 4, "regionName": "Хмельницька"},
            "autoInfoBar": {"damage": False, "onRepairParts": False, "abroad": False, "custom": False},
            "technicalCondition": {"id": 1},
            "autoData": {"autoId": int(source_id), "year": 2017, "isSold": False, "active": True,
                         "statusId": 0, "raceInt": 100, "bodyId": 4, "fuelId": 2, "gearBoxId": 2,
                         "generationId": 10, "modificationId": 20, "fuelName": "Дизель", "gearboxName": "Автомат"}}
    data.update(overrides)
    return data


@pytest.fixture
def engine(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "search.db"))
    Base.metadata.create_all(engine)
    initialize_budget(engine)
    yield engine
    engine.dispose()


def fixture_fetch(calls):
    def fetch(key, path, params):
        calls.append((path, params))
        if path == "categories/1/marks":
            return [{"name": "Volkswagen", "value": 84}]
        if path == "categories/1/marks/84/models":
            return [{"name": "Golf", "value": 30}]
        if path == "states":
            return [{"name": "Хмельницька", "value": 4}]
        if path == "categories/1/bodystyles":
            return [{"name": "Хетчбек", "value": 4}]
        if path == "type":
            return [{"name": "Дизель", "value": 2}]
        if path == "categories/1/gearboxes":
            return [{"name": "Автомат", "value": 2}]
        if path == "search":
            ids = ["124", "125", "126", "127", "128"] if "generation_id[0][0]" in params else ["123"]
            return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
        if path == "info":
            return raw(params["auto_id"], USD=10000 if params["auto_id"] == "123" else 15000)
        raise AssertionError(path)
    return fetch


def test_filters_and_independent_valuation_with_cache(engine):
    calls = []
    filters = Filters(brand="Volkswagen", model="Golf", region="Хмельницька область",
                      body=["Хетчбек"], fuel=["Дизель"], transmission=["Автомат"],
                      price={"to": 11000}, mileage={"from": 90, "to": 110})
    result = RiaSearch(engine, "secret", fixture_fetch(calls)).search(filters)
    assert len(result["cars"]) == 1
    car = result["cars"][0]
    assert car["market"] == 15000 and car["comparables"] == 5
    assert car["mileage"] == 100000
    searches = [params for path, params in calls if path == "search"]
    assert searches[0]["price_do"] == 11000 and searches[0]["state[0]"] == 4
    assert searches[0]["raceFrom"] == 90
    assert "price_do" not in searches[1] and "state[0]" not in searches[1]
    before = len(calls)
    RiaSearch(engine, "secret", fixture_fetch(calls)).search(filters)
    assert len(calls) == before
    with Session(engine) as db:
        assert "secret" not in str([row.payload for row in db.query(SourceCache)])


def test_unknown_choice_is_never_dropped(engine):
    calls = []
    with pytest.raises(RiaError, match="unsupported_filter"):
        RiaSearch(engine, "key", fixture_fetch(calls)).search(Filters(brand="Unknown"))
    assert all(path != "search" for path, _ in calls)


@pytest.mark.parametrize("kind", ["hour", "day", "total", "blocked"])
def test_durable_budget_stops_network(engine, kind):
    with Session(engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        if kind == "hour": row.calls = [time.time()] * 24
        if kind == "day": row.calls = [time.time() - 4000] * 60
        if kind == "total": row.total = 900
        if kind == "blocked": row.blocked_until = time.time() + 3600
        db.commit()
    calls = []
    with pytest.raises(RiaError, match="quota_exceeded"):
        RiaSearch(engine, "key", fixture_fetch(calls)).search(Filters())
    assert not calls
    initialize_budget(engine)  # Startup must not reset persisted limits.
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").busy_until == 0


def test_busy_and_failure_count(engine):
    first = RiaSearch(engine, "key")
    first.acquire()
    with pytest.raises(RiaError, match="busy"):
        RiaSearch(engine, "key").acquire()
    first.release()
    def fail(*args): raise RiaError("quota_exceeded")
    with pytest.raises(RiaError):
        RiaSearch(engine, "key", fail).search(Filters())
    with Session(engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        assert row.total == 3 and len(row.calls) == 1
        assert row.blocked_until > time.time()


def test_insufficient_duplicate_self_and_mismatched_peers():
    candidate = parse_car(raw(), "123")
    peers = [parse_car(raw(str(i), USD=15000), str(i)) for i in range(124, 129)]
    assert estimate(candidate, peers)["market"] == 15000
    assert estimate(candidate, peers[:4])["market"] is None
    assert estimate(candidate, [candidate] + peers[:4])["market"] is None
    assert estimate(candidate, [peers[0]] * 5)["market"] is None
    for field, value in [("modification_id", 999), ("mileage", 900000), ("year", 2010),
                          ("comparable_condition", False), ("fuel_id", 6)]:
        altered = [{**p, field: value} for p in peers]
        assert estimate(candidate, altered)["market"] is None


def test_exact_15_percent_boundary():
    candidate = parse_car(raw(USD=8500), "123")
    peers = [parse_car(raw(str(i)), str(i)) for i in range(124, 129)]
    result = estimate(candidate, peers)
    assert result["discount"] == 15
    assert candidate["price_usd"] <= result["market"] * .85
    assert 8501 > result["market"] * .85


def test_detail_rechecks_and_private_data_removed():
    car = parse_car(raw(VIN="secret-vin", userId=123, userPhoneData={"phone": "secret-phone"}), "123")
    assert "secret" not in str(car)
    assert not matches(car, Filters(price={"to": 9000}), {})
    assert not matches(car, Filters(), {"region_id": 10})
    assert not matches(car, Filters(), {"fuel_id": [6]})


def test_search_route_requires_telegram_before_network(engine):
    with TestClient(create_app(Settings(str(engine.url), "test-token", "x" * 32), engine)) as client:
        assert client.post("/api/cars/search", json={}).status_code == 401
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 2
