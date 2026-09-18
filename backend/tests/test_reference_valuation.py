import copy
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import peer_cache
from backend.auto_ria import RiaError
from backend.models import Delivery, MonitorJob, MonitorSeen, MonitorWatch, User
from backend.monitor import NOTIFICATION_VERSION
from backend.launch import activity
from backend.reference_valuation import VERSION, estimate, notification_estimate
from backend.ria_search import RiaSearch, parse_car
from backend.tests.test_backend import car as delivery_car
from backend.tests.test_informational_alerts import alter_details
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_ria_search import engine, raw
from backend.valuation import evidence_valid
from backend.worker import TelegramSender, fresh


def sample():
    candidate = parse_car(raw(USD=8000), "123")
    peers = [parse_car(raw(str(i), USD=10000), str(i)) for i in range(124, 129)]
    for car in [candidate, *peers]:
        car["engine_cc"] = 2000
    return candidate, peers


@pytest.mark.parametrize("change", [{"gear_id": None}, {"fuel_id": None}, {"body_id": None},
    {"generation_id": None}, {"mileage": None}, {"modification_id": 999},
    {"comparable_condition": False}, {"year": 2019}, {"mileage": 155000}])
def test_incomplete_or_broader_candidate_has_a_labelled_real_reference(change):
    candidate, peers = sample()
    candidate.update(change)
    rating = notification_estimate(candidate, peers[:3])
    assert rating["valuation"] == "reference_lower_quartile" and rating["market"] == 10000
    assert rating["discount"] == 20 and rating["comparables"] == 3
    assert rating["valuation_evidence"]["confidence"] == "indicative"
    assert notification_estimate(candidate, peers[:2])["market"] is None


def test_exact_result_keeps_priority_and_mixed_prices_are_not_cherry_picked():
    candidate, peers = sample()
    prices = [8000, 10000, 13000]
    assert estimate(candidate, [{**p, "price_usd": price} for p, price in zip(peers, prices)])["market"] == 9000
    assert notification_estimate(candidate, peers)["valuation"] == "sample_lower_quartile"
    assert notification_estimate(candidate, peers[:4])["valuation"] == "reference_lower_quartile"
    peers[-1]["price_usd"] = 25000
    assert notification_estimate(candidate, peers)["market"] == 10000
    assert notification_estimate({**candidate, "gear_id": None}, peers)["market"] == 10000
    peers[0]["price_usd"] = 100
    assert notification_estimate(candidate, peers)["valuation"] == "mixed_sample"
    assert notification_estimate({**candidate, "gear_id": None}, peers)["market"] is None


@pytest.mark.parametrize("change", [{"brand_id": 999}, {"model_id": 999}, {"generation_id": 999},
    {"body_id": 999}, {"fuel_id": 999}, {"gear_id": 999}, {"engine_cc": 3000},
    {"year": 2020}, {"mileage": 161000}, {"mileage": None}, {"price_usd": 0},
    {"price_usd": float("nan")}, {"comparable_condition": False}, {"condition_exclusions": ["damage"]}])
def test_known_conflicts_or_invalid_peer_cannot_supply_a_third_reference(change):
    candidate, peers = sample()
    peers = peers[:3]
    peers[-1].update(change)
    assert estimate(candidate, peers)["market"] is None
    assert estimate(candidate, peers)["comparables"] == 2


def test_reference_rejects_missing_core_data_adverse_condition_stale_and_duplicate_cars():
    candidate, peers = sample()
    for change in ({"brand_id": None}, {"model_id": None}, {"price_usd": 0},
                   {"condition_exclusions": ["custom"]}, {"condition_exclusions": None},
                   {"observed_at": candidate["observed_at"] - 901}):
        assert estimate({**candidate, **change}, peers)["market"] is None
    assert estimate(candidate, [peers[0]] * 5)["comparables"] == 1
    peers[1]["vehicle_key"] = peers[0]["vehicle_key"] = "same-hashed-vehicle"
    assert estimate(candidate, peers[:3])["market"] is None
    peers = sample()[1][:3]
    peers[-1]["observed_at"] -= 901
    assert estimate(candidate, peers)["market"] is None
    assert estimate(candidate, [{**p, "observed_at": candidate["observed_at"] + 31} for p in peers])["market"] is None


def test_reference_proof_recomputes_price_and_tracks_changed_condition(engine):
    candidate, peers = sample()
    candidate.update(gear_id=None, comparable_condition=False)
    rating = notification_estimate(candidate, peers)
    car = delivery_car(source="auto_ria", source_id="123", price=candidate["price_usd"],
        market=rating["market"], comparables=rating["comparables"], year=candidate["year"],
        mileage=candidate["mileage"], observed_at=candidate["observed_at"], valuation_evidence=rating["valuation_evidence"])
    now = candidate["observed_at"]
    assert evidence_valid(car, now)
    assert not evidence_valid(car.model_copy(update={"market": 20000}), now)
    assert not evidence_valid(car.model_copy(update={"price": 1}), now)
    assert not fresh(car, now + 301)
    proof = copy.deepcopy(car.valuation_evidence)
    proof["confidence"] = "exact"
    assert not evidence_valid(car.model_copy(update={"valuation_evidence": proof}), now)
    proof = copy.deepcopy(car.valuation_evidence)
    proof["candidate"]["condition_exclusions"] = ["damage"]
    assert not evidence_valid(car.model_copy(update={"valuation_evidence": proof}), now)
    peer_cache.observe(engine, candidate, create=True)
    peer_cache.observe(engine, {**candidate, "condition_exclusions": ["damage"], "observed_at": now + 1})
    with Session(engine) as db:
        assert not fresh(car, now + 1, db)


@pytest.mark.parametrize("missing", ["gear_id", "generation_id"])
def test_partial_candidate_uses_one_broad_query_and_reuses_peers_without_calls(engine, missing):
    calls = []
    candidate, _ = sample()
    candidate[missing] = None
    def fetch(key, path, params):
        calls.append((path, dict(params)))
        if path == "search":
            assert {"gear_id": "gearbox[0]", "generation_id": "generation_id[0][0]"}[missing] not in params
            assert "state[0]" not in params and "price_do" not in params
            assert params["s_yers[0]"] == 2015 and params["po_yers[0]"] == 2019
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 129)], "count": 5}}}
        data = raw(params["auto_id"])
        data["autoData"]["fuelName"] = "Дизель, 2 л."
        return data
    source = RiaSearch(engine, "test-only", fetch)
    source.acquire()
    try:
        peers = source.notification_comparisons(candidate)
        assert notification_estimate(candidate, peers)["market"] == 10000
        assert len(calls) == 6
        assert source.notification_comparisons({**candidate, "id": "777"})
        assert len(calls) == 6
    finally:
        source.release()


def test_three_retrieved_peers_survive_later_upstream_failure(engine):
    candidate, _ = sample()
    candidate["gear_id"] = None
    calls = []
    def fetch(key, path, params):
        calls.append(path)
        if path == "search":
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 129)], "count": 5}}}
        if params["auto_id"] == "127":
            raise RiaError("connection_error")
        data = raw(params["auto_id"])
        data["autoData"]["fuelName"] = "Дизель, 2 л."
        return data
    source = RiaSearch(engine, "test-only", fetch)
    source.acquire()
    try:
        peers = source.notification_comparisons(candidate)
    finally:
        source.release()
    result = notification_estimate(candidate, peers)
    assert result["market"] == 10000 and result["comparables"] == 3
    assert len(calls) == 5 and peers.diagnostics["unavailable"] == "connection_error"


@pytest.mark.parametrize("limit", [4, 20])
def test_broader_lookup_cannot_expand_request_allowance(engine, monkeypatch, limit):
    monkeypatch.setenv("RIA_COMPARABLE_SCAN_LIMIT", "20")
    candidate, _ = sample()
    candidate["gear_id"] = None
    calls = []
    def fetch(key, path, params):
        calls.append(path)
        if path == "search":
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 150)], "count": 26}}}
        data = raw(params["auto_id"])
        data["autoData"]["fuelId"] = 999
        return data
    source = RiaSearch(engine, "test-only", fetch)
    source.request_limit = limit
    source.acquire()
    try:
        peers = source.notification_comparisons(candidate)
    finally:
        source.release()
    assert notification_estimate(candidate, peers)["market"] is None
    assert len(calls) == min(8, limit)
    assert source.request_limit == limit and peers.diagnostics["limited"]
    assert calls.count("search") == 1


def test_comparison_stops_starting_calls_after_short_deadline(engine, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr("backend.ria_search.time.monotonic", lambda: elapsed[0])
    candidate, _ = sample()
    candidate["gear_id"] = None
    calls = []
    def fetch(key, path, params):
        calls.append(path)
        elapsed[0] += 3
        if path == "search":
            return {"result": {"search_result": {"ids": [str(i) for i in range(124, 129)], "count": 5}}}
        return raw(params["auto_id"])
    source = RiaSearch(engine, "test-only", fetch)
    source.acquire()
    try:
        peers = source.notification_comparisons(candidate)
    finally:
        source.release()
    assert len(calls) == 3 and peers.diagnostics["unavailable"] == "search_limit"


def test_reference_alert_obeys_each_subscription_discount_and_never_resends(p, monkeypatch):
    accepted_events = []
    monkeypatch.setattr("backend.worker.delivery_log.info", lambda *args: accepted_events.append(args))
    add_search(p, sid=2, uid=222, minDiscount=40)
    alter_details(p, lambda data: data["autoData"].update(gearBoxId=None, gearboxName=""))
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][0] == 111
    car = p.sent[0][1]
    assert car.market == 15000 and car.valuation_evidence["version"] == VERSION
    assert evidence_valid(car, p.clock[0])
    assert len(accepted_events) == 1 and accepted_events[0][1:] == (
        "124", VERSION, 15000, 15000, 5, [15000] * 5)
    with Session(p.engine) as db:
        assert activity(db, 111)["reference_estimated"] == 1
        assert activity(db, 111)["messages_accepted"] == 1
        assert activity(db, 222)["messages_accepted"] == 0
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        delivery.state = "uncertain"
        db.commit()
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    requests, real_client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
        real_client(transport=httpx.MockTransport(handler), **kw))
    TelegramSender("test-only")(111, car)
    text = requests[0]["text"]
    assert "Обережний ціновий орієнтир: ≈ $15 000" in text and "Нижче орієнтира: 33,3%" in text
    assert "Нижній квартиль цін 5 схожих авто" in text and "оцінка приблизна" in text
    assert "🔥 Вигода:" not in text


def test_stop_during_reference_lookup_prevents_send(p):
    alter_details(p, lambda data: data["autoData"].update(gearBoxId=None))
    factory = p.runner.search_factory
    def stopped(engine, key):
        source = factory(engine, key)
        original = source.notification_comparisons
        def comparisons(candidate):
            result = original(candidate)
            with Session(engine) as db:
                db.get(User, 111).ready = False
                db.commit()
            return result
        source.notification_comparisons = comparisons
        return source
    p.runner.search_factory = stopped
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert not p.sent


def test_new_reference_policy_does_not_replay_already_sent_or_unvalued_history(p):
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "124")
        version = job.result["rating"]["valuation_version"]
        db.add(MonitorJob(source_id="125", state="unvalued", first_seen=p.clock[0] - 7200,
                          result={"notification_version": NOTIFICATION_VERSION,
                                  "rating": {"valuation_version": version}}))
        db.add(MonitorSeen(search_id=1, source_id="125", state="unvalued", first_seen=p.clock[0] - 7200,
                          epoch=db.get(MonitorWatch, 1).epoch))
        db.commit()
    assert version == "asking-v5"
    # Current jobs and finished history are not replayed just to add a new
    # estimate to an old message.
    before = len(p.calls)
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")
    assert len(p.calls) == before and len(p.sent) == 1
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "125").state == db.get(MonitorSeen, (1, "125")).state == "unvalued"
