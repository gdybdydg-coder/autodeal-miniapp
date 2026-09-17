import copy

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import MonitorJob, MonitorSeen, User
from backend.ria_search import RiaSearch, engine_capacity, parse_car
from backend.ria_validation import comparison_report
from backend.tests.test_monitor import p, drain
from backend.tests.test_ria_search import engine, raw
from backend.valuation import estimate


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
def test_policy_rechecks_only_active_previous_monitor_interests(p, ready):
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "124")
        seen = db.scalar(select(MonitorSeen).where(MonitorSeen.source_id == "124"))
        job.state, seen.state = "unvalued", "unvalued"
        job.result = {"rating": {"valuation_version": "strict-v2"}}
        db.get(User, 111).ready = ready
        db.commit()
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == ("pending" if ready else "unvalued")
        assert db.scalar(select(MonitorSeen).where(MonitorSeen.source_id == "124")).state == ("pending" if ready else "unvalued")
