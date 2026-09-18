import copy
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import peer_cache
from backend.auto_ria import RiaError
from backend.models import SourceBudget, ValuationPeer
from backend.ria_search import RiaSearch, parse_car
from backend.ria_validation import comparison_report
from backend.tests.test_backend import car as delivery_car
from backend.tests.test_ria_search import engine, fixture_fetch, raw
from backend.valuation import estimate, evidence_valid, is_deal, vehicle_key


def sample(price=8500):
    candidate = parse_car(raw(USD=price), "123")
    peers = [parse_car(raw(str(i)), str(i)) for i in range(124, 129)]
    return candidate, peers


@pytest.mark.parametrize("price,assessment", [(8500, "deal"), (8500.01, "not_deal"), (10000, "not_deal"), (12000, "not_deal")])
def test_exact_threshold_is_independent_of_display_rounding(price, assessment):
    candidate, peers = sample(price)
    rating = estimate(candidate, peers)
    assert rating["assessment"] == assessment
    assert rating["market"] == 10000 and rating["valuation_evidence"]["deal_threshold_usd"] == 8500
    assert is_deal(price, rating["market"]) is (assessment == "deal")
    if price == 8500.01:
        assert rating["discount"] == 15  # Rounded label cannot admit this car.
    assert comparison_report(candidate, peers)["qualifies_as_deal"] is (assessment == "deal")


@pytest.mark.parametrize("change,reason", [({"generation_id": 999}, "generation_id"),
    ({"modification_id": 999}, "modification_id"), ({"fuel_id": None}, "missing_fuel_id"),
    ({"gear_id": 999}, "gear_id"), ({"body_id": 999}, "body_id"),
    ({"year": 2015}, "year"), ({"mileage": 131000}, "mileage"),
    ({"comparable_condition": False}, "unverified_condition"),
    ({"price_usd": 0}, "invalid_price"), ({"price_usd": float("nan")}, "invalid_price")])
def test_ineligible_peer_cannot_supply_the_fifth_comparison(change, reason):
    candidate, peers = sample()
    peers[-1].update(change)
    rating = estimate(candidate, peers)
    assert rating["assessment"] == "unknown" and rating["market"] is None
    assert rating["comparables"] == 4
    assert reason in rating["valuation_evidence"]["rejected"][0]["reasons"]


def test_full_vin_deduplicates_relisted_vehicle_without_retaining_the_vin():
    candidate, peers = sample()
    vin = "WVWZZZ3CZEE123456"
    peers[0] = parse_car(raw("124", VIN=vin), "124")
    peers[1] = parse_car(raw("125", VIN=vin), "125")
    result = estimate(candidate, peers)
    assert vin not in json.dumps(result)
    assert result["comparables"] == 4 and result["assessment"] == "unknown"
    assert any("duplicate_vehicle" in item["reasons"] for item in result["valuation_evidence"]["rejected"])
    assert comparison_report(candidate, peers)["calculation_matches"]
    # A relisting of the candidate itself must not influence its own median.
    candidate["vehicle_key"] = vehicle_key(vin)
    assert estimate(candidate, peers)["comparables"] == 3
    for partial in ("WVWZZZ3CZEE******", "WVWZZZ3CZXXXXXXXX", "XXXXXXXXXXXXXXXXX", "", None):
        assert vehicle_key(partial) is None


def test_newest_observation_wins_and_stale_or_future_details_are_unknown():
    candidate, peers = sample()
    now = time.time()
    old = {**peers[0], "observed_at": now - 901, "price_usd": 30000}
    assert estimate(candidate, [*peers, old], now=now)["market"] == 10000
    assert estimate(candidate, [old, *peers], now=now)["market"] == 10000
    for stamp in (now - 901, now + 31):
        rating = estimate(candidate, [*peers[:-1], {**peers[-1], "observed_at": stamp}], now=now)
        assert rating["assessment"] == "unknown" and rating["comparables"] == 4
        assert "stale_details" in rating["valuation_evidence"]["rejected"][0]["reasons"]
        assert estimate({**candidate, "observed_at": stamp}, peers, now=now)["assessment"] == "unknown"


def test_mixed_sample_is_unknown_even_with_a_low_candidate_price():
    candidate, peers = sample(100)
    peers[-1]["price_usd"] = 20001
    rating = estimate(candidate, peers)
    assert rating["assessment"] == "unknown" and rating["market"] is None
    assert rating["valuation_reasons"] == ["mixed_sample"]


def test_missing_peer_modification_uses_verified_engine_without_accepting_conflicts():
    candidate, peers = sample()
    candidate["engine_cc"] = 2000
    for peer in peers:
        peer.update(modification_id=None, engine_cc=2000)
    result = estimate(candidate, peers)
    assert result["comparables"] == 5 and result["market"] == 10000
    assert result["valuation_evidence"]["comparison_basis"] == "modification_with_engine_fallback"
    for change in ({"engine_cc": None}, {"engine_cc": 1600}, {"modification_id": 999},
                   {"generation_id": 999}, {"fuel_id": 1}, {"gear_id": 1}):
        changed = [*peers[:-1], {**peers[-1], **change}]
        rating = estimate(candidate, changed)
        assert rating["market"] is None and rating["comparables"] == 4
    candidate["engine_cc"] = None
    assert estimate(candidate, peers)["market"] is None


def test_delivery_requires_consistent_evidence_and_fresh_peer_prices():
    candidate, peers = sample()
    now = time.time()
    for peer in peers:
        peer["observed_at"] = now - 890
    rating = estimate(candidate, peers, now=now)
    car = delivery_car(source_id=candidate["id"], source="auto_ria", price=candidate["price_usd"],
        market=rating["market"], year=candidate["year"], mileage=candidate["mileage"],
        observed_at=candidate["observed_at"], comparables=rating["comparables"],
        valuation_evidence=rating["valuation_evidence"])
    assert evidence_valid(car, now)
    assert not evidence_valid(car, now + 11)
    assert not evidence_valid(car.model_copy(update={"price": 8400}), now)
    assert not evidence_valid(car.model_copy(update={"market": 11000}), now)
    for bad in (None, {}, {"version": "strict-v1"}, {"version": "strict-v2", "candidate": None}):
        assert not evidence_valid(car.model_copy(update={"valuation_evidence": bad}), now)


def test_second_similar_car_reuses_comparisons_across_processes_without_requests(engine):
    calls = []
    source = RiaSearch(engine, "test-only", fixture_fetch(calls))
    candidate = parse_car(raw(), "123")
    source.acquire()
    try:
        assert estimate(candidate, source.comparisons(candidate))["assessment"] == "deal"
    finally:
        source.release()
    assert len(calls) == 6  # One peer search and five details.
    with Session(engine) as db:
        total = db.get(SourceBudget, "auto_ria").total
        # Candidate is not seeded into the price sample just because it is cheap.
        assert db.get(ValuationPeer, "123") is None
    def forbidden(*args):
        pytest.fail("fresh shared comparisons should make no provider call")
    restarted = RiaSearch(engine, "test-only", forbidden)
    other = {**candidate, "id": "777", "mileage": 115000, "year": 2018}
    batch = restarted.comparisons(other)
    assert estimate(other, batch)["market"] == 15000
    assert batch.diagnostics["cached_peers"] == 5 and batch.diagnostics["requests_used"] == 0
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == total
    for changes in ({"modification_id": 999}, {"mileage": 200000}, {"year": 2020}):
        assert not peer_cache.candidates(engine, {**other, **changes})


def test_pool_observations_expire_and_sold_or_changed_details_cannot_return(engine, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr("backend.peer_cache.time.time", lambda: clock[0])
    state = {"sold": False, "price": 15000}
    base = fixture_fetch([])
    def fetch(key, path, params):
        data = base(key, path, params)
        if path == "info":
            data["USD"] = state["price"]
            data["autoData"]["isSold"] = state["sold"]
        return data
    source = RiaSearch(engine, "test", fetch)
    candidate = parse_car(raw(), "123")
    source.acquire()
    try:
        source.comparisons(candidate)
        prior = source.car("124")
        clock[0] += 1
        state["price"] = 12000
        source.car("124", force=True)
        assert source.car("124")["price_usd"] == 12000
        assert peer_cache.observe(engine, prior)["price_usd"] == 12000
        state["sold"] = True
        with pytest.raises(RiaError, match="listing_unavailable"):
            source.car("124", force=True)
        with pytest.raises(RiaError, match="listing_unavailable"):
            peer_cache.observe(engine, prior)
        assert "124" not in {p["id"] for p in peer_cache.candidates(engine, candidate)}
        # Equal timestamp tombstones also win over old cached detail.
        with pytest.raises(RiaError, match="listing_unavailable"):
            peer_cache.observe(engine, {**prior, "observed_at": clock[0]})
        clock[0] += 901
        assert not peer_cache.candidates(engine, {**candidate, "observed_at": clock[0]})
    finally:
        source.release()


def test_interrupted_comparison_reuses_saved_progress_with_the_same_budget(engine):
    calls = []
    source = RiaSearch(engine, "test", fixture_fetch(calls))
    candidate = parse_car(raw(), "123")
    source.request_limit = 4  # Search plus three peers, then stop.
    source.acquire()
    try:
        with pytest.raises(RiaError, match="search_limit"):
            source.comparisons(candidate)
    finally:
        source.release()
    restarted = RiaSearch(engine, "test", fixture_fetch(calls))
    restarted.acquire()
    try:
        rating = estimate(candidate, restarted.comparisons(candidate))
    finally:
        restarted.release()
    assert rating["assessment"] == "deal" and len(calls) == 6
    assert restarted.requests_made == 2
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 8  # Initialized at 2; no counter reset.


def test_scan_limit_is_unknown_with_an_explicit_reason(engine, monkeypatch):
    monkeypatch.setenv("RIA_COMPARABLE_SCAN_LIMIT", "6")
    def fetch(key, path, params):
        if path == "search":
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 174)], "count": 100}}}
        data = raw(params["auto_id"])
        data["autoData"]["modificationId"] = 999
        return data
    source = RiaSearch(engine, "test", fetch)
    candidate = parse_car(raw(), "123")
    source.acquire()
    try:
        result = estimate(candidate, source.comparisons(candidate))
    finally:
        source.release()
    assert result["assessment"] == "unknown" and result["market"] is None
    assert "comparison_limit" in result["valuation_reasons"]
    assert source.requests_made == 7


def test_archived_real_deal_and_ordinary_price_replay_without_provider_access():
    docs = Path(__file__).parents[1] / "docs"
    found = {}
    for name in ("valuation-audit-2026-09-17.json", "valuation-audit-eligible-2026-09-17.json"):
        audit = json.loads((docs / name).read_text())["valuation_check"]
        for query in audit["queries"]:
            cars = {c["id"]: c for c in query["cars"]}
            for report in query["comparisons"]:
                candidate = copy.deepcopy(cars[report["candidate_id"]])
                as_of = max(c["observed_at"] for c in [candidate, *report["peers"]])
                result = estimate(candidate, report["peers"], now=as_of)
                assert comparison_report(candidate, report["peers"], now=as_of)["calculation_matches"]
                if result["market"] is not None:
                    found[candidate["id"]] = (candidate["price_usd"], result["market"], result["assessment"])
                    assert estimate(candidate, report["peers"], now=as_of + 901)["assessment"] == "unknown"
    # Archived observations are replayed as-of their retrieval time, offline.
    # The new lower quartiles replace the historical medians 13,300 / 11,700.
    assert found == {"39378439": (14400, 13200, "not_deal"), "40444164": (8700, 10500, "deal")}
