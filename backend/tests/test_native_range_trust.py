"""Source trust regression; all transports isolated, no live parity claim."""
import copy
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import ria_ai_price as ai, ria_market_range as native
from backend.models import Delivery, Listing, MonitorJob, MonitorMatch, SourceBudget, SourceProbe
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_ai_price import discover_new, wire
from backend.tests.test_ria_market_range import fixture, priced_car
from backend.worker import TelegramSender, fresh


def strict(p):
    p.settings = replace(p.settings, ria_ai_price_enabled=True,
                         ria_confirmed_deals_only=True, auto_ria_user_id="42")
    p.runner.settings = p.settings


def test_saved_trafic_api_observation_is_not_native_market():
    candidate, verified = fixture(4999, 4707, 5202)
    candidate["id"] = verified["source_id"] = "40517476"
    observation = ai.parse_quote(wire(7012), "40517476", now=candidate["observed_at"])
    assert (observation["lower_usd"], observation["upper_usd"]) == (6661, 7362)
    result = native.estimate(candidate, observation)
    assert result["market"] is None and result["assessment"] == "unknown"
    assert result["valuation_reasons"] == ["native_market_range_unverified"]
    correct = native.estimate(candidate, verified)
    assert correct["market"] == 4471.65 and correct["assessment"] == "not_deal"
    # Matching source ID, radius arithmetic or changing a label is insufficient.
    observation["basis"] = native.BASIS
    assert not native.range_valid(observation, candidate["id"], candidate["observed_at"])


def test_production_adapter_withholds_before_paid_valuation_and_has_no_delivery(p, monkeypatch):
    strict(p)
    monkeypatch.setattr(ai, "fetch_quote", lambda *a, **k: pytest.fail("paid valuation"))
    monkeypatch.setattr("backend.ria_search.RiaSearch.notification_comparisons",
                        lambda *a: pytest.fail("peer fallback"))
    drain(p)
    discover_new(p)
    drain(p)
    with Session(p.engine) as db:
        result = db.get(MonitorJob, "124")
        assert result.state == "unvalued"
        assert result.result["rating"]["market"] is None
        assert "native_market_range_unverified" in result.result["rating"]["valuation_reasons"]
        assert db.scalar(select(MonitorMatch)) is None
        assert db.scalar(select(Delivery)) is None
        used = db.get(SourceBudget, "auto_ria").total
    assert not p.sent
    wake(p, 301)
    drain(p)
    with Session(p.engine) as db:
        # Only continuing source discovery may reserve calls; no quote/replay.
        assert db.get(SourceBudget, "auto_ria").total >= used
    assert not p.sent


def test_independent_native_fixture_rejects_trafic_and_allows_real_threshold(p, monkeypatch):
    strict(p)
    def native_transport(self, sid, uid):
        # Synthetic independent explicit bounds, not produced by AI parsing.
        return {"source_id": sid, "basis": native.BASIS, "currency": "USD",
                "lower_usd": 4707, "upper_usd": 5202, "observed_at": p.clock[0]}
    monkeypatch.setattr("backend.ria_search.RiaSearch.market_range", native_transport)
    p.prices["124"], p.prices["125"] = 4999, 3500
    drain(p)
    discover_new(p)
    drain(p)
    assert not p.sent
    discover_new(p, "125")
    drain(p)
    assert [(uid, car.source_id, car.market) for uid, car in p.sent] == [(111, "125", 4471.65)]


def test_previous_api_card_cannot_reach_telegram_even_with_fresh_data(monkeypatch):
    candidate, quote = fixture(3500, 4707, 5202)
    car = priced_car(candidate, quote)
    proof = copy.deepcopy(car.valuation_evidence)
    proof.update(version="autoria-lower-bound-v1", basis=ai.API_BASIS)
    proof["source_range"] = ai.parse_quote(wire(7012), candidate["id"], now=candidate["observed_at"])
    legacy = car.model_copy(update={"market": 6327.95, "valuation_evidence": proof})
    assert not fresh(legacy, candidate["observed_at"], require_provider_range=True)
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw: pytest.fail("telegram"))
    with pytest.raises(ValueError, match="invalid_provider_range_evidence"):
        TelegramSender("test-only")(111, legacy)


def test_policy_exposes_hold_and_does_not_claim_native_access():
    policy = native.policy(confirmed_deals_only=True)
    assert policy["valuation_status"] == "withheld_native_range_unverified"
    assert policy["native_range_transport_available"] is False
    assert policy["api_range_accepted_for_deal_selection"] is False
    assert policy["provider_calls_per_uncached_listing"] == 0
    assert policy["adjustment_percent"] == 5


@pytest.mark.parametrize("missing_photo", [False, True])
def test_obsolete_photo_repair_is_held_before_source_io_and_does_not_abort_discovery(p, monkeypatch, missing_photo):
    from backend.tests.test_photo_repair import configured, run
    configured(p, monkeypatch)
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        delivery_id = delivery.id
        listing = db.get(Listing, delivery.listing_id)
        car = copy.deepcopy(listing.car)
        car["valuation_evidence"].update(version="autoria-lower-bound-v1", basis=ai.API_BASIS)
        if missing_photo:
            car["photo"] = None
        listing.car = car
        before = db.get(SourceBudget, "auto_ria").total
        db.commit()
    p.runner.search_factory = lambda *a: pytest.fail("unverified photo read")
    run(p, lambda *a: pytest.fail("unverified edit"))
    run(p, lambda *a: pytest.fail("unverified edit retry"))
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before
        assert db.get(SourceProbe, "owner-photo-repair-v1-111-124").status == "valuation_snapshot_invalid"
        assert db.get(Delivery, delivery_id).state == "sent"
        assert db.get(Delivery, delivery_id).message_id == 71
