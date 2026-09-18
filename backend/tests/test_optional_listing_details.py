import copy
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Filters, MonitorJob, MonitorSeen, User
from backend.ria_search import RiaSearch, engine_capacity, matches, parse_car
from backend.ria_validation import comparison_report
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_search import engine, fixture_fetch, raw
from backend.valuation import estimate
from backend.reference_valuation import VERSION as REFERENCE_VERSION


def sparse(source_id="123", price=10000, capacity="1.9", modification=None):
    data = raw(source_id, USD=price, technicalCondition=None)
    data["autoData"].update(modificationId=modification, fuelName=f"Дизель, {capacity} л.")
    return data


def test_missing_optional_condition_is_not_a_damage_assertion():
    candidate = parse_car(raw(technicalCondition=None), "123")
    peers = [parse_car(raw(str(i), USD=15000, technicalCondition=None), str(i)) for i in range(124, 129)]
    assert candidate["comparable_condition"] is True
    assert estimate(candidate, peers)["assessment"] == "deal"
    for state in (2, 3, 4, "1", True, 0):
        assert not parse_car(raw(technicalCondition={"id": state}), "123")["comparable_condition"]
    for flag in ("damage", "onRepairParts", "custom", "abroad"):
        for value in (True, None, 0, "false"):
            data = raw(technicalCondition=None)
            data["autoInfoBar"][flag] = value
            assert not parse_car(data, "123")["comparable_condition"]


@pytest.mark.parametrize("label,expected", [("Дизель, 1.9 л.", 1900), ("Бензин, 1,6 л.", 1600),
    ("Бензин, 2 л.", 2000), ("Electric 100 kW", None), ("1.9 / 2.0 л.", None),
    ("1.9 л. або 2.0 л.", None), ("12.5 л.", None), (None, None)])
def test_capacity_requires_an_explicit_unit(label, expected):
    assert engine_capacity(label) == expected


def test_engine_basis_still_requires_five_matching_generation_body_fuel_gear_peers(engine):
    calls = []
    def fetch(key, path, params):
        calls.append((path, params))
        if path == "search":
            assert "modifications[0][0][0]" not in params
            assert params["engineVolumeFrom"] == params["engineVolumeTo"] == 1.9
            assert "price_do" not in params and "state[0]" not in params
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 130)], "count": 6}}}
        sid = params["auto_id"]
        # Provider ignores the volume filter for the first item: recheck details.
        return sparse(sid, 15000, "2.5" if sid == "124" else "1.9", modification=20)
    candidate = parse_car(sparse(), "123")
    source = RiaSearch(engine, "fake", fetch)
    source.acquire()
    try:
        peers = source.comparisons(candidate)
    finally:
        source.release()
    rating = estimate(candidate, peers)
    assert rating["assessment"] == "deal" and rating["comparables"] == 5
    assert rating["valuation_evidence"]["comparison_basis"] == "engine_capacity"
    assert comparison_report(candidate, peers)["calculation_matches"]
    assert "engine_cc" in rating["valuation_evidence"]["rejected"][0]["reasons"]
    for key in ("generation_id", "body_id", "gear_id", "fuel_id"):
        changed = copy.deepcopy(peers)
        changed[-1][key] = 999
        assert estimate(candidate, changed)["assessment"] == "unknown"
    # Known variants must still match exactly even with the same engine size.
    assert estimate({**candidate, "modification_id": 999}, peers)["assessment"] == "unknown"
    assert estimate({**candidate, "engine_cc": None}, peers)["assessment"] == "unknown"
    # Cache reuse remains possible across jobs without gathering a market mirror.
    before = len(calls)
    assert estimate({**candidate, "id": "777"}, source.comparisons({**candidate, "id": "777"}))["assessment"] == "deal"
    assert len(calls) == before


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("supplemental", [True, False])
def test_policy_rechecks_require_active_interests_and_supplemental_mode(p, ready, supplemental):
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "124")
        seen = db.scalar(select(MonitorSeen).where(MonitorSeen.source_id == "124"))
        job.state, seen.state = "unvalued", "unvalued"
        job.result = {"rating": {"valuation_version": "strict-v2"}}
        db.get(User, 111).ready = ready
        db.commit()
    p.runner.settings = replace(p.settings, ria_active_window_enabled=supplemental)
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")
    with Session(p.engine) as db:
        expected = "pending" if ready and supplemental else "unvalued"
        assert db.get(MonitorJob, "124").state == expected
        assert db.scalar(select(MonitorSeen).where(MonitorSeen.source_id == "124")).state == expected


def test_unknown_optional_values_match_but_known_conflicts_do_not():
    filters = Filters(body=["Універсал"], fuel=["Дизель"], transmission=["Автомат"],
                      mileage={"from": 50, "to": 200})
    ids = {"body_id": [4], "fuel_id": [2], "gear_id": [2]}
    candidate = parse_car(raw(), "123")
    candidate.update(body_id=None, fuel_id=None, gear_id=None, mileage=None)
    assert matches(candidate, filters, ids)
    for field, value in (("body_id", 99), ("fuel_id", 99), ("gear_id", 99)):
        assert not matches({**candidate, field: value}, filters, ids)
    assert not matches({**candidate, "mileage": 201000}, filters, ids)


def test_monitor_discovery_does_not_hide_ads_with_missing_optional_fields(engine):
    calls = []
    source = RiaSearch(engine, "fake", fixture_fetch(calls))
    source.acquire()
    try:
        filters = Filters(brand="Volkswagen", model="Golf", body=["Хетчбек"], fuel=["Дизель"],
                          transmission=["Автомат"], mileage={"from": 50, "to": 200}, price={"to": 20000})
        params, ids = source.discovery_parameters(filters)
    finally:
        source.release()
    assert ids["body_id"] and ids["fuel_id"] and ids["gear_id"]
    assert not any(key.startswith(("bodystyle[", "type[", "gearbox[")) for key in params)
    assert "raceFrom" not in params and "raceTo" not in params
    assert params["price_do"] == 20000


def test_monitor_sends_fresh_price_when_optional_details_are_missing(p):
    factory = p.runner.search_factory

    def incomplete_factory(engine, key):
        source = factory(engine, key)
        original = source.fetch

        def fetch(api_key, path, params):
            data = original(api_key, path, params)
            if path == "info" and params["auto_id"] == "124":
                data["autoData"].update(bodyId=None, fuelId=None, gearBoxId=None,
                                        raceInt=None, fuelName=None, gearboxName=None)
                data["subCategoryName"] = None
            return data

        source.fetch = fetch
        return source

    p.runner.search_factory = incomplete_factory
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    car = p.sent[0][1]
    assert car.source_id == "124" and car.price == 10000
    assert car.market == 15000 and car.comparables == 5 and car.mileage is None
    assert car.fuel == "" and car.transmission == "" and car.body == ""
    assert car.valuation_evidence["version"] == REFERENCE_VERSION
    # One bounded peer-price page can now value partial details, without scanning
    # old candidates or inventing the candidate's missing attributes.
    comparisons = [params for path, params in p.calls if path == "search" and "published_after" not in params]
    assert len(comparisons) == 1 and comparisons[0]["page"] == 0
    assert "gearbox[0]" not in comparisons[0] and "type[0]" not in comparisons[0]
    discovery = [params for path, params in p.calls if path == "search" and "published_after" in params]
    assert discovery and all("type[0]" not in params for params in discovery)
