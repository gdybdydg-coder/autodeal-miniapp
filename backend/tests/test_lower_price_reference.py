"""A wide upper tail must not hide a supported lower asking-price reference."""
import copy
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from backend.models import MonitorJob
from backend.reference_valuation import notification_estimate
from backend import peer_cache
from backend.ria_search import RiaSearch
from backend.tests.test_backend import car as delivery_car
from backend.tests.test_conservative_valuation import priced_sample
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_search import engine
from backend.valuation import evidence_valid, is_deal
from backend.worker import TelegramSender


def test_one_expensive_peer_does_not_hide_a_supported_lower_reference():
    # Synthetic regression distribution, not a claim about the screenshot's peers.
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    result = notification_estimate(candidate, peers)
    assert result["market"] == 1300 and result["discount"] == 26.9
    assert result["valuation"] == "reference_lower_quartile"
    proof = result["valuation_evidence"]
    assert proof["confidence"] == "indicative" and proof["reference_kind"] == "lower_price_band"
    assert proof["lower_price_support"]["count"] == 3
    assert sorted(peer["price_usd"] for peer in proof["peers"]) == [900, 1300, 1600, 1800, 6000]
    assert proof["median_usd"] == 1600


@pytest.mark.parametrize("prices", [
    [100, 1300, 1600, 1800, 6000],  # Never remove a cheap observation to raise the reference.
    [900, 1300, 6000, 6500, 7000],  # Only two peers support the lower price band.
    [900, 1300, 6000],              # Three observations do not support a stable lower band.
    [900, 1200, 1400, 6000, 6500, 7000],  # Lower half is not a majority.
])
def test_an_unsupported_lower_band_remains_unpriced(prices):
    candidate, peers = priced_sample(prices, asking=950)
    assert notification_estimate(candidate, peers)["market"] is None


def test_price_band_fallback_cannot_raise_exact_quartile_using_broader_expensive_peers():
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    broader = [{**peers[0], "id": str(9000 + index), "price_usd": 7000, "year": candidate["year"] + 2}
               for index in range(6)]
    result = notification_estimate(candidate, [*peers, *broader])
    assert result["market"] == 1300 and result["comparables"] == 5
    assert not is_deal(1300, result["market"], 10)


def test_wide_sample_proof_replays_all_prices_and_rejects_price_tampering():
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    result = notification_estimate(candidate, peers)
    car = delivery_car(source="auto_ria", source_id=candidate["id"], price=950,
        market=result["market"], comparables=result["comparables"], year=candidate["year"],
        mileage=candidate["mileage"], observed_at=candidate["observed_at"],
        valuation_evidence=result["valuation_evidence"])
    assert evidence_valid(car, candidate["observed_at"])
    assert not evidence_valid(car.model_copy(update={"market": 1600}), candidate["observed_at"])
    altered = copy.deepcopy(car.valuation_evidence)
    altered["peers"][0]["price_usd"] = 1
    assert not evidence_valid(car.model_copy(update={"valuation_evidence": altered}), candidate["observed_at"])


def test_monitor_sends_lower_reference_once_and_keeps_minimum_discount(p):
    p.prices.update({str(90000 + i): price for i, price in enumerate([900, 1300, 1600, 1800, 6000])})
    p.ads["124"], p.prices["124"] = p.clock[0] + 1, 950
    wake(p)
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][1].market == 1300
    with Session(p.engine) as db:
        assert not db.get(MonitorJob, "124").result["informational_notification"]
    p.ads["125"], p.prices["125"] = p.clock[0] + 1, 1200
    wake(p)
    drain(p)
    assert len(p.sent) == 1  # A priced car above the saved discount threshold is not sent.


def test_cached_wide_sample_needs_no_more_provider_requests(engine):
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    for peer in peers:
        peer_cache.observe(engine, peer, create=True)
    def forbidden(*args):
        pytest.fail("A retained exact cohort must not trigger more searches")
    source = RiaSearch(engine, "test-key", forbidden)
    source.acquire()
    try:
        result = notification_estimate(candidate, source.notification_comparisons(candidate))
        assert result["market"] == 1300 and source.requests_made == 0
    finally:
        source.release()


def test_telegram_discloses_wide_prices_and_uses_lower_reference(monkeypatch):
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    result = notification_estimate(candidate, peers)
    car = delivery_car(source="auto_ria", source_id=candidate["id"], price=950,
        market=result["market"], comparables=result["comparables"], year=candidate["year"],
        mileage=candidate["mileage"], observed_at=candidate["observed_at"],
        valuation_evidence=result["valuation_evidence"])
    requests, client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
                        client(transport=httpx.MockTransport(handler), **kw))
    assert TelegramSender("test-only")(111, car)["ok"]
    text = requests[0]["text"]
    assert "≈ $1 300" in text and "Нижче орієнтира: 26,9%" in text
    assert "Приблизний орієнтир: великий розкид цін аналогів" in text
    assert "фактична ціна продажу невідома" in text and "/stop" in text
    assert "$1 600" not in text
