"""Internal contract tests, not a claim of live AUTO.RIA range access."""
import copy
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from backend import ria_market_range as market_range
from backend.models import ValuationPeer
from backend.tests.test_backend import car as delivery_car
from backend.tests.test_ria_search import engine
from backend.tests.test_valuation import sample
from backend.valuation import evidence_valid, is_deal
from backend.worker import TelegramSender, fresh


def fixture(price=4299, lower=4168, upper=4607):
    # User screenshot values; synthetic ID 123. No live listing is reconstructed.
    candidate, _ = sample(price)
    quote = {"source_id": candidate["id"], "basis": market_range.BASIS,
             "currency": "USD", "lower_usd": lower, "upper_usd": upper,
             "observed_at": candidate["observed_at"]}
    return candidate, quote


def priced_car(candidate, quote):
    rating = market_range.estimate(candidate, quote, now=candidate["observed_at"])
    return delivery_car(source="auto_ria", source_id=candidate["id"],
        price=candidate["price_usd"], market=rating["market"], comparables=0,
        year=candidate["year"], mileage=candidate["mileage"],
        observed_at=candidate["observed_at"], valuation_evidence=rating["valuation_evidence"])


@pytest.mark.parametrize("price,lower,upper,expected,discount", [
    (4299, 4168, 4607, 3959.6, -8.6),
    (4600, 3724, 4116, 3537.8, -30.0),
    (950, 1300, 1600, 1235, 23.1),
])
def test_lower_boundary_not_average_or_upper_boundary(price, lower, upper, expected, discount):
    candidate, quote = fixture(price, lower, upper)
    rating = market_range.estimate(candidate, quote)
    assert rating["market"] == expected and rating["discount"] == discount
    assert rating["comparables"] == 0 and rating["valuation_evidence"]["peers"] == []
    assert rating["valuation_evidence"]["source_range"] == quote
    assert rating["assessment"] == ("deal" if price == 950 else "not_deal")


def test_saved_discount_applies_after_haircut_without_display_rounding():
    candidate, quote = fixture(3365.66)
    car = priced_car(candidate, quote)
    assert is_deal(car.price, car.market, 15)
    assert not is_deal(car.price + .01, car.market, 15)
    assert not is_deal(car.price, car.market, 15.01)
    assert not is_deal(4299, car.market, 0)


@pytest.mark.parametrize("change", [
    {"lower_usd": None}, {"lower_usd": 0}, {"lower_usd": -1},
    {"lower_usd": True}, {"lower_usd": "4168"}, {"lower_usd": float("nan")},
    {"upper_usd": float("inf")}, {"lower_usd": 5000}, {"currency": "UAH"},
    {"source_id": "124"}, {"basis": "asking_prices"},
    {"observed_at": 1}, {"observed_at": float("inf")},
])
def test_invalid_range_never_falls_back_to_a_guessed_price(change):
    candidate, quote = fixture()
    quote.update(change)
    rating = market_range.estimate(candidate, quote)
    assert rating["market"] is None and rating["discount"] is None
    assert rating["assessment"] == "unknown"


@pytest.mark.parametrize("quote", [None, {}, [],
    {"statisticData": [{"type": "avgPrice", "price": {"USD": 4388}}]},
    {"averagePrice": 4388}, {"lower_quartile_usd": 4168},
])
def test_average_only_missing_or_legacy_payload_is_not_a_provider_range(quote):
    candidate, _ = fixture()
    assert market_range.estimate(candidate, quote)["market"] is None


def test_range_cannot_outlive_either_observation():
    candidate, quote = fixture()
    now = candidate["observed_at"]
    car = priced_car(candidate, quote)
    assert fresh(car, now)
    assert not fresh(car, now + 301)
    assert market_range.estimate(candidate, {**quote, "observed_at": now - 301}, now=now)["market"] is None
    assert market_range.estimate(candidate, {**quote, "observed_at": now + 31}, now=now)["market"] is None
    assert market_range.estimate({**candidate, "observed_at": now - 301}, quote, now=now)["market"] is None


@pytest.mark.parametrize("change", [
    {"adjustment_percent": 10}, {"adjustment_percent": True},
    {"currency": "UAH"}, {"basis": "asking_prices"},
    {"pricing_method": "lower_quartile"}, {"candidate": None},
    {"source_range": None}, {"peers": [{}]}, {"condition_notices": ["damage"]},
])
def test_replayed_proof_rejects_changed_formula_or_mismatched_evidence(change):
    candidate, quote = fixture(950, 1300, 1600)
    car = priced_car(candidate, quote)
    assert evidence_valid(car, candidate["observed_at"])
    proof = {**copy.deepcopy(car.valuation_evidence), **change}
    assert not evidence_valid(car.model_copy(update={"valuation_evidence": proof}), candidate["observed_at"])


def test_changed_listing_or_reference_is_rejected_at_dispatch(engine):
    candidate, quote = fixture(950, 1300, 1600)
    car = priced_car(candidate, quote)
    now = candidate["observed_at"]
    assert not fresh(car.model_copy(update={"market": 1300}), now)
    assert not fresh(car.model_copy(update={"price": 960}), now)
    assert not fresh(car.model_copy(update={"source_id": "124"}), now)
    assert not fresh(car.model_copy(update={"comparables": 5}), now)
    with Session(engine) as db:
        assert fresh(car, now, db)
        db.add(ValuationPeer(source_id=candidate["id"], car={**candidate, "price_usd": 960},
                             observed_at=now, available=True, group_key=""))
        db.commit()
        assert not fresh(car, now, db)
        db.get(ValuationPeer, candidate["id"]).available = False
        db.commit()
        assert not fresh(car, now, db)


@pytest.mark.parametrize("flag,allowed", [("damage", True), ("technical_condition", True),
                                        ("onRepairParts", False), ("abroad", False), ("custom", False)])
def test_repair_notice_and_remaining_exclusions(flag, allowed):
    candidate, quote = fixture(950, 1300, 1600)
    candidate["condition_exclusions"] = [flag]
    candidate["comparable_condition"] = False
    rating = market_range.estimate(candidate, quote)
    assert (rating["market"] is not None) is allowed
    if allowed:
        assert rating["valuation_evidence"]["condition_notices"] == [flag]


@pytest.mark.parametrize("price,expected", [(950, "Вигода: 23,1%"),
                                          (1300, "Вище ринкової ціни: 5,3%")])
def test_telegram_card_uses_concise_pricing_and_does_not_invent_profit(monkeypatch, price, expected):
    candidate, quote = fixture(price, 1300, 1600)
    car = priced_car(candidate, quote)
    requests, client = [], httpx.Client
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw:
                        client(transport=httpx.MockTransport(handler), **kw))
    assert TelegramSender("test-only")(111, car)["ok"]
    text = requests[0]["text"]
    assert "Ринкова ціна: ≈ $1 235" in text and expected in text
    assert "нижня межа" not in text and "− 5%" not in text and "нашого орієнтира" not in text
    assert "квартиль" not in text and "схожих авто" not in text
    assert "прибуток" not in text and "/stop" in text
    assert requests[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == str(car.url)


def test_formatter_rejects_unverified_range_before_network(monkeypatch):
    candidate, quote = fixture()
    car = priced_car(candidate, quote)
    car.valuation_evidence["source_range"] = {"averagePrice": 4388}
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw: pytest.fail("network"))
    with pytest.raises(ValueError, match="invalid_provider_range_evidence"):
        TelegramSender("test-only")(111, car)
