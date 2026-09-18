import json

import httpx
import pytest
from sqlalchemy.orm import Session

from backend.auto_ria import RiaError
from backend.models import Car, MonitorJob, MonitorSeen, MonitorWatch
from backend.monitor import NOTIFICATION_VERSION
from backend.ria_search import RiaSearch, parse_car
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_search import engine, raw
from backend.valuation import VERSION, estimate, evidence_valid, price_only_evidence_valid
from backend.worker import TelegramSender, fresh


def alter_details(p, mutate):
    factory = p.runner.search_factory
    def changed(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            data = original(api_key, path, params)
            if path == "info" and params["auto_id"] == "124":
                mutate(data)
            return data
        source.fetch = fetch
        return source
    p.runner.search_factory = changed


@pytest.mark.parametrize("flag", ["onRepairParts", "abroad", "custom"])
def test_parts_abroad_and_custom_exclusions_are_not_bypassed_by_incomplete_details(p, flag):
    def mutate(data):
        data["autoData"]["gearBoxId"] = None
        if flag == "technical":
            data["technicalCondition"] = {"id": 2}
        else:
            data["autoInfoBar"][flag] = True
    alter_details(p, mutate)
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "excluded"


@pytest.mark.parametrize("with_reference", [False, True])
def test_unknown_condition_is_explicitly_labelled_without_a_fake_discount(p, monkeypatch, with_reference):
    alter_details(p, lambda data: data.update(autoInfoBar={"damage": False}))
    if not with_reference:
        monkeypatch.setattr(RiaSearch, "notification_comparisons", lambda *_: [])
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert len(p.sent) == 1
    car = p.sent[0][1]
    if with_reference:
        assert car.market == 15000 and evidence_valid(car, p.clock[0])
        assert car.valuation_evidence["confidence"] == "indicative"
    else:
        assert car.market is None and price_only_evidence_valid(car, p.clock[0])
        assert "unverified_condition" in car.valuation_evidence["uncertainty_reasons"]
    requests, real_client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
        real_client(transport=httpx.MockTransport(handler), **kw))
    TelegramSender("test-only")(111, car)
    text = requests[0]["text"]
    assert ("Обережний ціновий орієнтир" if with_reference else "Ринкову оцінку не підтверджено") in text
    assert "Стан авто не підтверджено" in text
    assert "Вигода:" not in text
    assert ("%" in text) is with_reference
    # A stale price, price change or tampered exclusion can never authorize send.
    assert not fresh(car, p.clock[0] + 301)
    assert not fresh(car.model_copy(update={"price": 1}), p.clock[0])
    proof = {**car.valuation_evidence, "candidate": {
        **car.valuation_evidence["candidate"], "condition_exclusions": ["damage"]}}
    assert not fresh(car.model_copy(update={"valuation_evidence": proof}), p.clock[0])


@pytest.mark.parametrize("error", ["search_limit", "quota_exceeded", "connection_error", "upstream_error"])
def test_peer_failure_does_not_discard_a_fresh_matching_candidate(p, error):
    factory = p.runner.search_factory
    def failing(engine, key):
        source = factory(engine, key)
        def comparisons(_):
            raise RiaError(error)
        source.comparisons = comparisons
        return source
    p.runner.search_factory = failing
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][1].market is None
    assert p.sent[0][1].price == 10000


def test_pending_insufficient_peers_is_once_not_a_permanent_retry_loop(p):
    candidate = parse_car(raw("124"), "124")
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="124", first_seen=p.clock[0], state="pending",
                          result={"candidate": candidate, "filters": {}, "notification_version": "optional-details-v1",
                                  "rating": {"valuation_version": "asking-v4"}}))
        db.add(MonitorSeen(search_id=1, source_id="124", epoch=db.get(MonitorWatch, 1).epoch,
                           state="pending", first_seen=p.clock[0]))
        db.commit()
    factory = p.runner.search_factory
    def insufficient(engine, key):
        source = factory(engine, key)
        source.notification_comparisons = lambda _: []
        return source
    p.runner.search_factory = insufficient
    drain(p)
    assert len(p.sent) == 1
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "informational"
        assert db.get(MonitorJob, "124").result["notification_version"] == NOTIFICATION_VERSION


def test_missing_peer_mileage_cannot_crash_the_entire_notification_job(engine):
    def fetch(key, path, params):
        if path == "search":
            return {"result": {"search_result": {"ids": ["124"], "count": 1}}}
        data = raw("124", USD=15000)
        data["autoData"]["raceInt"] = None
        return data
    candidate = parse_car(raw(), "123")
    source = RiaSearch(engine, "test-only", fetch)
    source.acquire()
    try:
        rating = estimate(candidate, source.comparisons(candidate))
    finally:
        source.release()
    assert rating["market"] is None
    assert "invalid_mileage" in rating["valuation_evidence"]["rejected"][0]["reasons"]
