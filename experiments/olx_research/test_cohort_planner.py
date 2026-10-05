import copy
import json
from pathlib import Path

from .cohort_planner import plan_detail_batch, sanitized_candidate, title_hints


def card(id, *, title="Skoda Octavia A5 1.9 TDI", year=2007, mileage=330000, price="5000"):
    return sanitized_candidate({
        "source": "olx", "id": id,
        "url": f"https://www.olx.ua/d/uk/obyavlenie/car-ID{id}.html",
        "title": title, "price": price, "currency": "USD", "year": year,
        "mileage_km": mileage, "engine_cc": 1900, "fuel": "diesel",
        "transmission": "manual", "body": None, "brand": None, "model": None,
        "checked_at": 1, "first_seen_at": 1, "observed_search_reason": "organic",
        "field_conflicts": None,
    })


def test_title_hints_are_non_reversible_and_ambiguous_values_are_not_promoted():
    hints = title_hints("Call me: x@example.test, VIN ABCDEFGH123456789; A5 1.9 TDI Combi 105 к.с.")
    assert hints == {
        "explicit_a5": True, "other_generation_or_tour": False,
        "body_hint": "wagon", "modification_hint": "1.9 tdi",
        "power_hp_hint": 105, "ambiguous_hint": False,
    }
    assert "example" not in json.dumps(hints)
    ambiguous = title_hints("A5 1.9 TDI 2.0 TDI wagon liftback 105 hp 140 hp")
    assert ambiguous["ambiguous_hint"] is True
    assert ambiguous["modification_hint"] is ambiguous["body_hint"] is ambiguous["power_hp_hint"] is None


def test_sanitizer_keeps_hints_out_of_vehicle_attributes():
    result = card("one", title="A5 1.9 TDI Combi 105 hp")
    assert "title" not in result and "description" not in result and "vin" not in result
    assert result["body"] is None and "modification" not in result and "power_hp" not in result
    assert result["title_hints"]["body_hint"] == "wagon"


def test_plan_is_price_independent_and_does_not_claim_physical_independence():
    rows = [card(str(i), mileage=330000 + i * 1000, price=str(1000 + i)) for i in range(8)]
    first = plan_detail_batch(rows)
    changed = copy.deepcopy(rows)
    for i, row in enumerate(changed):
        row["price"] = str(999999 - i * 10000)
        row["currency"] = "UAH" if i % 2 else "EUR"
    second = plan_detail_batch(changed)
    assert [c["id"] for c in first["candidates"]] == [c["id"] for c in second["candidates"]]
    assert first["status"] == "detail_batch_plannable" and first["planned_calls"] == 8
    assert first["price_used_for_selection"] is False
    assert first["distinct_ads_are_verified_physical_vehicles"] is False


def test_conflicting_modification_is_not_used_to_fill_batch():
    compatible = [card(str(i)) for i in range(7)]
    incompatible = card("bad", title="A5 2.0 TDI")
    result = plan_detail_batch(compatible + [incompatible])
    assert result["status"] == "insufficient_hint_density"
    assert result["planned_calls"] == 0 and result["candidates"] == []


def test_missing_explicit_a5_cannot_fill_batch_when_search_model_is_unverified():
    compatible = [card(str(i)) for i in range(7)]
    wrong_model = card("wrong", title="Golf 5 Plus 1.9 TDI")
    result = plan_detail_batch(compatible + [wrong_model])
    assert result["status"] == "insufficient_hint_density"
    assert result["planned_calls"] == 0


def test_saved_real_batch_is_consumed_and_not_planned_twice():
    path = Path("experiments/olx_research/examples/pending-candidates.json")
    rows = json.loads(path.read_text())["candidates"]
    frozen = json.loads(Path("experiments/olx_research/examples/night0300-plan.json").read_text())
    assert frozen["selection"]["planned_calls"] == frozen["request_cap"] == 9
    assert frozen["selection"]["price_used_for_selection"] is False
    remaining_ids = {row["id"] for row in rows}
    assert not remaining_ids.intersection(row["id"] for row in frozen["selection"]["candidates"])
    result = plan_detail_batch(rows)
    assert result["required_sample"] == 8
    assert result["price_used_for_selection"] is False
    assert result["status"] == "insufficient_hint_density"
    assert result["planned_calls"] == 0 and result["candidates"] == []
