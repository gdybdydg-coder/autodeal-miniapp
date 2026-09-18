"""Owner-selected repair candidates are labelled and delivered, not hidden."""
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from backend.models import Delivery, MonitorJob, SourceProbe, User
from backend.notification_recovery import probe_id
from backend.tests.test_informational_alerts import alter_details
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_notification_recovery import enable_recovery
from backend.valuation import evidence_valid, price_only_evidence_valid
from backend.worker import TelegramSender


@pytest.mark.parametrize("flag", ["damage", "technical_condition"])
@pytest.mark.parametrize("with_peers", [False, True])
def test_repair_candidate_reaches_telegram_once_with_condition_disclosed(p, monkeypatch, flag, with_peers):
    def mutate(data):
        if flag == "damage":
            data["autoInfoBar"]["damage"] = True
        else:
            data["technicalCondition"] = {"id": 3}
    alter_details(p, mutate)
    if not with_peers:
        monkeypatch.setattr("backend.ria_search.RiaSearch.notification_comparisons", lambda *_: [])
    p.ads["124"] = p.clock[0] + 1
    p.prices["124"] = 1700
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    car = p.sent[0][1]
    proof = car.valuation_evidence
    assert proof["condition_notices"] == [flag]
    assert proof["candidate"]["condition_exclusions"] == [flag]
    if with_peers:
        assert car.market == 15000 and evidence_valid(car, p.clock[0])
        assert proof["confidence"] == "indicative"
    else:
        assert car.market is None and price_only_evidence_valid(car, p.clock[0])
        assert "repair_condition" in proof["uncertainty_reasons"]
    validator = evidence_valid if with_peers else price_only_evidence_valid
    assert not validator(car.model_copy(update={"valuation_evidence": {**proof, "condition_notices": []}}), p.clock[0])
    assert not validator(car.model_copy(update={"valuation_evidence": {**proof, "candidate": None}}), p.clock[0])
    requests, client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
                        client(transport=httpx.MockTransport(handler), **kw))
    assert TelegramSender("test-only")(111, car)["ok"]
    assert "позначка про пошкодження / ремонт" in requests[0]["text"]
    assert "/stop" in requests[0]["text"]
    wake(p)
    drain(p)
    assert len(p.sent) == 1
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state != "excluded"


def test_repair_candidate_still_obeys_saved_minimum_discount(p):
    alter_details(p, lambda data: data.update(technicalCondition={"id": 3}))
    p.ads["124"], p.prices["124"] = p.clock[0] + 1, 14000
    wake(p)
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").result["rating"]["market"] == 15000


def test_reported_repair_listing_outside_publication_window_uses_normal_worker_once(p):
    # Incident shape: exact publication query omits the ID, technical condition
    # is 3, and generation is missing. No historical discovery is enabled.
    def mutate(data):
        data["technicalCondition"] = {"id": 3}
        data["autoData"]["generationId"] = None
    alter_details(p, mutate)
    factory = p.runner.search_factory
    def with_broad_peers(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def wrapped(key, path, params):
            if path == "search" and "published_after" not in params:
                p.calls.append((path, dict(params)))
                ids = [str(n) for n in range(90000, 90005)]
                return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
            return fetch(key, path, params)
        source.fetch = wrapped
        return source
    p.runner.search_factory = with_broad_peers
    p.ads["124"], p.prices["124"] = p.clock[0] - 3600, 1700
    enable_recovery(p, "124")
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][1].source_id == "124"
    assert p.sent[0][1].market == 15000
    assert p.sent[0][1].valuation_evidence["condition_notices"] == ["technical_condition"]
    with Session(p.engine) as db:
        assert db.get(SourceProbe, probe_id("124")).result["report"]["delivery_states"] == {"sent": 1}
        db.query(Delivery).update({Delivery.state: "uncertain"})
        db.commit()
    before = len(p.calls)
    drain(p)
    assert len(p.sent) == 1 and len(p.calls) == before


def test_stop_during_repair_comparison_prevents_delivery(p):
    alter_details(p, lambda data: data.update(technicalCondition={"id": 3}))
    factory = p.runner.search_factory
    def stopping(engine, key):
        source = factory(engine, key)
        comparisons = source.notification_comparisons
        def stopped(candidate):
            peers = comparisons(candidate)
            with Session(engine) as db:
                db.get(User, 111).ready = False
                db.commit()
            return peers
        source.notification_comparisons = stopped
        return source
    p.runner.search_factory = stopping
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert not p.sent
