import hashlib
import json
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import market_cache
from backend.models import Filters, MonitorFeed, MonitorWatch
from backend.tests.test_backend import headers, ready, setup, subscribe
from backend.tests.test_market_cache import car
from backend.tests.test_monitor import add_search, details, drain, p, wake
from backend.tests.test_ria_search import engine
from backend.valuation import is_deal


def test_manual_percent_accepts_fractional_values_and_preserves_legacy_identity():
    legacy = Filters(brand="Volkswagen").model_dump(by_alias=True)
    legacy.pop("minDiscount")
    old_fingerprint = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert Filters.model_validate(legacy).fingerprint() == old_fingerprint
    assert Filters(**legacy, minDiscount=15).fingerprint() == old_fingerprint
    for percent in (0, .5, 7, 12.5, 70, 83, 99.99, 100):
        parsed = Filters(**legacy, minDiscount=percent)
        assert Filters.model_validate(parsed.canonical()).minDiscount == percent
        assert parsed.fingerprint() != old_fingerprint
    for bad in (-1, 100.01, float("nan"), float("inf"), True, "12.5", None):
        with pytest.raises(ValidationError):
            Filters(minDiscount=bad)


def test_threshold_uses_exact_price_without_rounding_the_selected_percent():
    assert is_deal(8750, 10000, 12.5)
    assert not is_deal(8750.01, 10000, 12.5)
    assert not is_deal(8750, 10000, 12.5001)
    assert is_deal(10000, 10000, 0)
    assert not is_deal(10000.01, 10000, 0)
    assert not is_deal(.01, 10000, 100)
    assert not is_deal(0, 10000, 100)


def test_api_saves_and_edits_percent_without_resetting_unchanged_subscriptions(setup):
    engine, _, client = setup
    sid = ready(setup, brand="Volkswagen")
    with Session(engine) as db:
        epoch = db.get(MonitorWatch, sid).epoch
    same = client.put(f"/api/subscriptions/{sid}", headers=headers(), json={
        "name": "Volkswagen", "filters": {"brand": "Volkswagen", "minDiscount": 15}})
    assert same.status_code == 200 and same.json()["enabled"]
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid).epoch == epoch
    changed = client.put(f"/api/subscriptions/{sid}", headers=headers(), json={
        "name": "Volkswagen", "filters": {"brand": "Volkswagen", "minDiscount": 12.5}})
    assert changed.status_code == 200 and not changed.json()["enabled"]
    saved = client.get("/api/subscriptions", headers=headers()).json()
    assert len(saved) == 1 and saved[0]["id"] == sid and saved[0]["filters"]["minDiscount"] == 12.5
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid) is None
    for bad in (-1, 100.01, "12.5", True):
        assert subscribe(client, enabled=False, minDiscount=bad).status_code == 422


def test_shared_discovery_and_valuation_deliver_each_subscriptions_own_threshold(p):
    add_search(p, sid=2, uid=222, minDiscount=12.5)
    add_search(p, sid=3, uid=333, minDiscount=70)
    drain(p)
    p.ads.update({"124": p.clock[0] + 1, "125": p.clock[0] + 2})
    p.prices.update({"124": 13125, "125": 4500})  # Exactly 12.5% and 70% below 15000.
    wake(p)
    drain(p)
    # Dispatch continues independently after discovery becomes idle.
    for _ in range(4):
        p.runner.deliver_tick()
    assert {(uid, car.source_id) for uid, car in p.sent} == {
        (222, "124"), (111, "125"), (222, "125"), (333, "125")}
    assert len(p.sent) == 4
    assert all(len(details(p, sid)) == 1 for sid in ("124", "125"))
    assert len([params for path, params in p.calls
                if path == "search" and "generation_id[0][0]" in params]) == 1
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorFeed)))) == 1
    wake(p)
    drain(p)
    assert len(p.sent) == 4


def test_manual_cache_can_return_a_deal_below_the_old_fifteen_percent_floor(engine):
    with Session(engine) as db:
        market_cache.put(db, car("124", price_usd=13125), time.time())
        market_cache.put(db, car("125", price_usd=13125.01), time.time())
        db.commit()
        assert market_cache.query(db, Filters())["cars"] == []
        result = market_cache.query(db, Filters(minDiscount=12.5))
        assert [item["id"] for item in result["cars"]] == ["124"]
