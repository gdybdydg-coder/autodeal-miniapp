"""Old unsent cards require current-policy evidence; terminal claims stay final."""
import copy

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import ria_ai_price as ai
from backend.models import Delivery, Listing, MonitorJob, MonitorMatch
from backend.ria_search import RiaSearch
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_native_range_trust import strict
from backend.tests.test_ria_ai_price import discover_new, enable, wire
from backend.worker import deliver_one, enqueue


def test_unsent_v1_card_is_rechecked_using_fresh_current_api_evidence_without_replay(p, monkeypatch):
    production_range = RiaSearch.market_range
    enable(p, monkeypatch)
    strict(p)
    drain(p)
    discover_new(p)
    assert p.runner.tick()
    assert p.runner.tick()
    enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        car = copy.deepcopy(listing.car)
        proof = car["valuation_evidence"]
        proof.update(version="autoria-lower-bound-v1", basis=ai.API_BASIS)
        proof["source_range"] = ai.parse_quote(wire(15000), "124", now=p.clock[0])
        listing.car = car
        job = db.get(MonitorJob, "124")
        evidence = copy.deepcopy(job.result)
        evidence["rating"]["valuation_version"] = "autoria-lower-bound-v1"
        evidence["rating"]["valuation_evidence"] = proof
        job.result = evidence
        assert db.scalar(select(Delivery)).state == "pending"
        db.commit()
    monkeypatch.setattr(RiaSearch, "market_range", production_range)
    monkeypatch.setattr(ai, "fetch_quote", lambda *a, **k: pytest.fail("unverified paid quote"))
    assert deliver_one(p.engine, p.settings, p.runner.sender) == "pending"
    drain(p)
    wake(p, 6)
    drain(p)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == "sent"
        assert db.get(MonitorJob, "124").result["rating"]["valuation_version"] == "autoria-api-lower-bound-v3"
    assert len(p.sent) == 1
    wake(p, 301)
    drain(p)
    assert len(p.sent) == 1
