import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auto_ria import RiaError
from backend.models import Filters, MonitorFeed, MonitorWatch, Search
from backend.monitor import reset_watch
from backend.ria_search import RiaSearch, matches, parse_car, parse_ids
from backend.tests.test_backend import setup, headers, ready, subscribe
from backend.tests.test_monitor import p, drain, wake, searches, details
from backend.tests.test_ria_search import engine, fixture_fetch, raw


REGIONS = ["Вінницька область", "Чернівецька область", "Хмельницька область", "Тернопільська область"]
STATES = [{"name": name.removesuffix(" область"), "value": index + 1}
          for index, name in enumerate(REGIONS)]


def test_region_sets_preserve_existing_fingerprints_and_ignore_order():
    assert Filters(region=[]).fingerprint() == "18873a860d1e270a6b0ee4dbea9c6c7026576b1fad4fc5d82f587ef4f74a1a6f"
    assert Filters(region=["Хмельницька область"]).fingerprint() == "50236a704d1a54d9a89076a96a6e9963e3e485d573bd04210bb08aaa768ae182"
    assert Filters(region=REGIONS).fingerprint() == Filters(region=REGIONS[::-1] + REGIONS).fingerprint()
    assert Filters(region=REGIONS).canonical()["region"] == sorted(REGIONS)
    for invalid in ([""], [1], ["x"] * 31, "x" * 151):
        with pytest.raises(ValueError):
            Filters(region=invalid)


def test_regions_use_one_provider_query_and_strict_location_matching(engine):
    calls = []
    base = fixture_fetch(calls)
    def fetch(key, path, params):
        return STATES if path == "states" else base(key, path, params)
    source = RiaSearch(engine, "test-only", fetch)
    filters = Filters(region=REGIONS)
    source.acquire()
    try:
        params, resolved = source.discovery_parameters(filters)
        source.request("search", params, parse_ids)
    finally:
        source.release()
    assert sorted(value for key, value in params.items() if key.startswith("state[")) == [1, 2, 3, 4]
    assert [params[f"city[{index}]"] for index in range(4)] == [0, 0, 0, 0]
    assert len([path for path, _ in calls if path == "search"]) == 1
    candidate = parse_car(raw(), "123")
    for state in (1, 2, 3, 4, 5, None):
        assert matches({**candidate, "region_id": state}, filters, resolved) is (state in (1, 2, 3, 4))
    source.acquire()
    try:
        with pytest.raises(RiaError, match="unsupported_filter"):
            source.parameters(Filters(region=REGIONS + ["Невідома область"]))
    finally:
        source.release()


def test_multi_region_subscription_save_read_rename_and_duplicate_protection(setup):
    engine, _, client = setup
    sid = ready(setup, region=REGIONS)
    with Session(engine) as db:
        epoch = db.get(MonitorWatch, sid).epoch
    response = client.put(f"/api/subscriptions/{sid}", headers=headers(),
                          json={"name": "Чотири області", "filters": {"region": REGIONS[::-1]}})
    assert response.status_code == 200 and response.json()["enabled"]
    rows = client.get("/api/subscriptions", headers=headers()).json()
    assert len(rows) == 1 and rows[0]["filters"]["region"] == sorted(REGIONS)
    assert subscribe(client, enabled=False, region=REGIONS + [REGIONS[0]]).status_code == 409
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid).epoch == epoch


def test_one_multi_region_feed_delivers_only_new_matching_cars_once(p):
    with Session(p.engine) as db:
        search = db.get(Search, 1)
        filters = p.filters.model_copy(update={"region": REGIONS})
        search.filters, search.fingerprint = filters.canonical(), filters.fingerprint()
        reset_watch(db, 1, True)
        db.commit()
    factory = p.runner.search_factory
    def regions_source(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            if path == "states":
                return STATES
            data = original(api_key, path, params)
            if path == "info":
                state = int(params["auto_id"]) - 123
                data["stateData"] = {"stateId": state, "regionName": "Fixture"}
                data["autoData"].pop("fuelId", None)
            return data
        source.fetch = fetch
        return source
    p.runner.search_factory = regions_source
    drain(p)
    p.ads.update({str(sid): p.clock[0] + 1 for sid in range(124, 129)})
    wake(p)
    drain(p)
    assert sorted(car.source_id for _, car in p.sent) == ["124", "125", "126", "127"]
    assert all(uid == 111 for uid, _ in p.sent)
    assert not details(p, "123")
    assert all(len([key for key in query if key.startswith("state[")]) == 4 for query in searches(p))
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorFeed)))) == 1
    wake(p)
    drain(p)
    assert len(p.sent) == 4
