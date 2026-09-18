import json
import time

from sqlalchemy.orm import Session

from backend import market_cache
from backend.models import Filters, ScanItem, SourceBudget
from backend.ria_search import RiaSearch, parse_car
from backend.tests.test_ria_search import engine, fixture_fetch, raw


def car(source_id="123", **changes):
    return {**parse_car(raw(source_id), source_id), "market": 15000, "discount": 33.3,
            "comparables": 5, "valuation": "sample_lower_quartile", **changes}


def test_cache_uses_all_filters_and_never_calls_the_provider(engine):
    calls = []
    source = RiaSearch(engine, "test-only", fixture_fetch(calls))
    filters = Filters(brand="Volkswagen", model="Golf", region="Хмельницька область",
                      body=["Хетчбек"], fuel=["Дизель"], transmission=["Автомат"],
                      price={"from": 9900, "to": 10100}, year={"from": 2017, "to": 2017},
                      mileage={"from": 99, "to": 101})
    source.acquire()
    try:
        source.parameters(filters)
    finally:
        source.release()
    with Session(engine) as db:
        market_cache.put(db, car(), time.time())
        # Every known dimension and range is significant; no widened filter.
        for i, field in enumerate(("brand_id", "model_id", "region_id", "body_id", "fuel_id", "gear_id",
                                   "price_usd", "year", "mileage"), start=200):
            market_cache.put(db, car(str(i), **{field: 999999}), time.time())
        db.commit()
        before = db.get(SourceBudget, "auto_ria").total
        result = market_cache.query(db, filters)
        assert [c["id"] for c in result["cars"]] == ["123"]
        assert result["total"] == 1 and result["coverage"] == "partial"
        assert all(c["from_cache"] for c in result["cars"])
        assert db.get(SourceBudget, "auto_ria").total == before
        assert "user_id" not in json.dumps(result) and "test-only" not in json.dumps(result)
        assert market_cache.query(db, Filters(brand="Unmapped"))["cars"] == []


def test_missing_dictionary_returns_no_unfiltered_cars(engine):
    with Session(engine) as db:
        market_cache.put(db, car(), time.time())
        db.commit()
        result = market_cache.query(db, Filters(brand="Volkswagen"))
        assert result["status"] == "catalog_pending" and result["cars"] == []
        assert db.get(SourceBudget, "auto_ria").total == 2


def test_legacy_median_cannot_be_relabelled_as_a_current_lower_quartile(engine):
    with Session(engine) as db:
        market_cache.put(db, car(valuation="sample_median"), time.time())
        db.commit()
        before = db.get(SourceBudget, "auto_ria").total
        assert market_cache.fresh(db, "123") is None
        assert not market_cache.query(db, Filters(onlyDeals=True))["cars"]
        old = market_cache.query(db, Filters(onlyDeals=False))["cars"][0]
        assert old["stale"] and old["market"] is None and old["discount"] is None
        assert db.get(SourceBudget, "auto_ria").total == before


def test_cache_is_paginated_and_old_prices_are_not_presented_as_current(engine):
    now = time.time()
    with Session(engine) as db:
        for i in range(100, 223):
            market_cache.put(db, car(str(i)), now - (901 if i == 100 else 0))
        db.commit()
        pages, offset = [], 0
        for _ in range(3):
            page = market_cache.query(db, Filters(), after=offset)
            pages.append(page)
            offset = page["after"]
        assert [len(p["cars"]) for p in pages] == [50, 50, 23]
        assert [p["more"] for p in pages] == [True, True, False]
        assert len({c["id"] for p in pages for c in p["cars"]}) == 123
        old = pages[0]["cars"][0]
        assert old["stale"] and old["historical_match"] and old["market"] is None and old["discount"] is None
        assert market_cache.fresh(db, "100") is None
        assert market_cache.fresh(db, "101")[0]["market"] == 15000


def test_backfill_reuses_old_scans_but_never_resurrects_a_known_removed_car(engine):
    now = time.time()
    with Session(engine) as db:
        db.add(ScanItem(scan_id="old", source_id="123", state="checked", car=car(), checked_at=now - 5))
        db.commit()
        market_cache.backfill(db)
        db.commit()
        assert market_cache.query(db, Filters())["total"] == 1
        market_cache.discard(db, "123")
        db.commit()
        market_cache.backfill(db)
        db.commit()
        assert market_cache.query(db, Filters())["total"] == 0
        assert market_cache.fresh(db, "123") is None
        # A later verified response can make the listing available again.
        market_cache.put(db, car(), now + 10)
        db.commit()
        assert market_cache.query(db, Filters())["total"] == 1


def test_expired_cached_cars_do_not_reappear_from_backfill(engine):
    now = time.time() - 86401
    with Session(engine) as db:
        market_cache.put(db, car(), now)
        db.add(ScanItem(scan_id="old", source_id="123", state="checked", car=car(), checked_at=now))
        db.commit()
        market_cache.backfill(db)
        db.commit()
        assert market_cache.query(db, Filters())["total"] == 0
