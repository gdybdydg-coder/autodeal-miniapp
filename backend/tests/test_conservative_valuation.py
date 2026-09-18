"""Conservative asking-price policy; examples are fixtures, not live sale prices."""
import copy
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Delivery, MonitorJob, MonitorSeen, MonitorWatch
from backend.monitor import NOTIFICATION_VERSION
from backend.reference_valuation import estimate as reference_estimate
from backend.tests.test_backend import car as delivery_car
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_valuation import sample
from backend.valuation import estimate, evidence_valid, is_deal
from backend.worker import TelegramSender


def priced_sample(prices, asking=3550):
    candidate, peers = sample(asking)
    peers = [{**peers[0], "id": str(1000 + i), "price_usd": price}
             for i, price in enumerate(prices)]
    return candidate, peers


def test_expensive_asking_prices_do_not_make_an_ordinary_price_a_bargain():
    # Illustrative distribution: the old median was $4,000; a $3,550 car
    # wrongly passed a 10% threshold. These are NOT the screenshot's peers.
    candidate, peers = priced_sample([3300, 3500, 4000, 4500, 4900])
    result = estimate(candidate, peers)
    assert result["market"] == 3500
    assert result["valuation_evidence"]["median_usd"] == 4000
    assert result["valuation_evidence"]["lower_quartile_usd"] == 3500
    assert result["valuation_evidence"]["pricing_method"] == "lower_quartile"
    assert not is_deal(candidate["price_usd"], result["market"], 10)
    assert result["assessment"] == "not_deal"


@pytest.mark.parametrize("prices, expected", [
    ([3300, 3500, 4000, 4500, 4900], 3500),
    ([3000, 3400, 3800, 4000, 4500, 4800], 3500),
    ([3000, 4000, 4100, 4200, 5000], 4000),
    ([4000] * 5, 4000),
])
def test_lower_quartile_is_order_independent_and_not_an_arbitrary_discount(prices, expected):
    candidate, peers = priced_sample(prices)
    for sequence in (peers, list(reversed(peers))):
        result = estimate(candidate, sequence)
        assert result["market"] == expected
        assert result["market"] <= result["valuation_evidence"]["median_usd"]
        assert min(prices) <= result["market"] <= max(prices)


def test_indicative_fallback_uses_the_same_conservative_policy():
    candidate, peers = priced_sample([3300, 4000, 4900])
    candidate["gear_id"] = None
    result = reference_estimate(candidate, peers)
    assert result["market"] == 3650  # Linear p25 of three real observations.
    assert result["valuation_evidence"]["median_usd"] == 4000
    assert result["valuation_evidence"]["confidence"] == "indicative"
    assert not is_deal(candidate["price_usd"], result["market"], 10)


def test_exact_quartile_threshold_is_not_relaxed_by_percentage_rounding():
    candidate, peers = priced_sample([3300, 3500, 4000, 4500, 4900], asking=2975)
    assert estimate(candidate, peers)["assessment"] == "deal"
    candidate["price_usd"] = 2975.01
    result = estimate(candidate, peers)
    assert result["discount"] == 15 and result["assessment"] == "not_deal"


def test_exact_telegram_card_shows_the_price_basis_without_claiming_resale_profit(monkeypatch):
    candidate, peers = priced_sample([3300, 3500, 4000, 4500, 4900], asking=2800)
    result = estimate(candidate, peers)
    car = delivery_car(source="auto_ria", source_id=candidate["id"], price=2800,
                       market=result["market"], comparables=5, year=candidate["year"],
                       mileage=candidate["mileage"], observed_at=candidate["observed_at"],
                       photo="https://cdn.example.com/car.jpg",
                       valuation_evidence=result["valuation_evidence"])
    requests, client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
                        client(transport=httpx.MockTransport(handler), **kw))
    assert TelegramSender("test-only")(111, car)["ok"]
    caption = requests[0]["caption"]
    assert "Обережний ціновий орієнтир: ≈ $3 500" in caption
    assert "Нижче орієнтира: 20%" in caption and "Нижній квартиль цін 5 схожих авто" in caption
    assert "фактична ціна продажу невідома" in caption and "/stop" in caption
    assert "Вигода" not in caption and "$4 000" not in caption and "Ринкова ціна" not in caption
    assert requests[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == str(car.url)


def test_old_median_proof_cannot_validate_a_new_conservative_delivery():
    candidate, peers = priced_sample([3300, 3500, 4000, 4500, 4900], asking=2800)
    result = estimate(candidate, peers)
    car = delivery_car(source="auto_ria", source_id=candidate["id"], price=2800,
                       market=result["market"], comparables=5, year=candidate["year"],
                       mileage=candidate["mileage"], observed_at=candidate["observed_at"],
                       valuation_evidence=result["valuation_evidence"])
    assert evidence_valid(car, candidate["observed_at"])
    legacy = copy.deepcopy(car.valuation_evidence)
    legacy["version"] = "asking-v4"
    assert not evidence_valid(car.model_copy(update={"valuation_evidence": legacy}), candidate["observed_at"])


def test_monitor_uses_lower_reference_for_thresholds_and_keeps_delivery_deduplication(p):
    add_search(p, minDiscount=10)
    p.prices.update({str(90000+i): price for i, price in enumerate([3300, 3500, 4000, 4500, 4900])})
    p.ads["124"], p.prices["124"] = p.clock[0] + 1, 3550
    wake(p)
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        result = db.get(MonitorJob, "124").result
        assert result["rating"]["market"] == 3500
        assert result["informational_notification"] is False
    p.ads["125"], p.prices["125"] = p.clock[0] + 1, 2975
    wake(p)
    drain(p)
    assert {(uid, car.source_id) for uid, car in p.sent} == {(111, "125"), (222, "125")}
    with Session(p.engine) as db:
        for row in db.scalars(select(Delivery)):
            if row.user_id == 222:
                row.state = "uncertain"
        db.commit()
    wake(p)
    drain(p)
    assert len(p.sent) == 2


def test_policy_upgrade_does_not_reopen_finished_history_in_new_publications_mode(p):
    drain(p)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        for index, state in enumerate(("unvalued", "checked", "informational")):
            sid = str(700 + index)
            db.add(MonitorJob(source_id=sid, state=state, first_seen=p.clock[0] - 7200,
                             result={"notification_version": NOTIFICATION_VERSION,
                                     "rating": {"valuation_version": "asking-v4"}}))
            db.add(MonitorSeen(search_id=1, source_id=sid, state=state,
                              first_seen=p.clock[0] - 7200, epoch=epoch))
        db.commit()
    before = len(p.calls)
    assert p.runner.claim()
    try:
        p.runner.sync()
    finally:
        p.runner.release("idle")
    assert len(p.calls) == before and not p.sent
    with Session(p.engine) as db:
        assert [(db.get(MonitorJob, str(700+i)).state, db.get(MonitorSeen, (1, str(700+i))).state)
                for i in range(3)] == [(s, s) for s in ("unvalued", "checked", "informational")]
