"""Fast database reads of previously checked public cars, with explicit age.

No request to the provider can happen here. This is a partial local index, never a
claim that the whole AUTO.RIA catalog has already been downloaded or valued.
"""
import copy
import hashlib
import json
import time

from sqlalchemy import delete, func, select

from .auto_ria import RiaError
from .models import MarketCar, ScanItem, SourceCache
from .ria_search import FRESH_SECONDS, RiaSearch

HISTORY_SECONDS = 86400
DIMENSIONS = ("brand_id", "model_id", "region_id", "body_id", "fuel_id", "gear_id")


def put(db, car, checked_at):
    row = db.scalar(select(MarketCar).where(MarketCar.source_id == car["id"]))
    if row and row.checked_at >= checked_at:
        return
    if row is None:
        row = MarketCar(source_id=car["id"])
        db.add(row)
    row.car, row.checked_at = copy.deepcopy(car), checked_at
    row.active = True
    row.price, row.year, row.mileage = car["price_usd"], car["year"], car["mileage"]
    row.deal = bool(car["valuation"] == "sample_median" and car["price_usd"] <= car["market"] * .85)
    for key in DIMENSIONS:
        setattr(row, key, car.get(key))


def discard(db, source_id):
    row = db.scalar(select(MarketCar).where(MarketCar.source_id == source_id))
    if row is None:
        row = MarketCar(source_id=source_id, car={}, deal=False, price=0, year=0, mileage=0)
        db.add(row)
    # Keep a timestamped tombstone so an older scan cannot reintroduce this car
    # during backfill. It expires with the same retention window as results.
    row.active, row.checked_at = False, time.time()


def fresh(db, source_id):
    row = db.scalar(select(MarketCar).where(MarketCar.source_id == source_id,
                                           MarketCar.active.is_(True),
                                           MarketCar.checked_at > time.time() - FRESH_SECONDS))
    return (copy.deepcopy(row.car), row.checked_at) if row else None


def backfill(db, limit=200):
    """Reuse results collected before the index existed, without provider calls."""
    cutoff = time.time() - HISTORY_SECONDS
    db.execute(delete(MarketCar).where(MarketCar.checked_at <= cutoff))
    records = db.scalars(select(ScanItem).outerjoin(MarketCar, MarketCar.source_id == ScanItem.source_id)
                        .where(ScanItem.state == "checked", ScanItem.checked_at > cutoff,
                               (MarketCar.id.is_(None)) | (MarketCar.checked_at < ScanItem.checked_at))
                        .order_by(ScanItem.checked_at.desc()).limit(limit))
    for item in records:
        put(db, item.car, item.checked_at)


class CachedCatalog(RiaSearch):
    def __init__(self, db):
        self.db = db

    def request(self, path, params, parser, ttl=900, **kwargs):
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        row = self.db.get(SourceCache, key)
        if row is None or row.expires_at <= time.time():
            raise RiaError("catalog_pending")
        return copy.deepcopy(row.payload)


def query(db, filters, after=0):
    try:
        _, ids = CachedCatalog(db).parameters(filters)
    except RiaError:
        # Missing dictionaries must not silently drop a filter or start an API
        # call on the request path. The background scanner resolves them later.
        return {"cars": [], "after": after, "more": False, "total": 0, "status": "catalog_pending", "coverage": "partial"}
    criteria = [MarketCar.active.is_(True), MarketCar.checked_at > time.time() - HISTORY_SECONDS]
    for key, values in ids.items():
        column = getattr(MarketCar, key)
        if isinstance(values, list):
            if values:
                criteria.append(column.in_(values))
        else:
            criteria.append(column == values)
    for name, span, scale in (("price", filters.price, 1), ("year", filters.year, 1), ("mileage", filters.mileage, 1000)):
        if span.from_ is not None:
            criteria.append(getattr(MarketCar, name) >= span.from_ * scale)
        if span.to is not None:
            criteria.append(getattr(MarketCar, name) <= span.to * scale)
    if filters.onlyDeals:
        criteria.append(MarketCar.deal.is_(True))
    count = db.scalar(select(func.count()).select_from(MarketCar).where(*criteria))
    records = list(db.scalars(select(MarketCar).where(*criteria, MarketCar.id > after)
                             .order_by(MarketCar.id).limit(51)))
    cars = []
    for row in records[:50]:
        car = copy.deepcopy(row.car)
        stale = time.time() - row.checked_at >= FRESH_SECONDS
        car.update(checked_at=row.checked_at, stale=stale, historical_match=bool(stale and row.deal), from_cache=True)
        if stale:
            car.update(market=None, discount=None, comparables=0, valuation="stale")
        cars.append(car)
    return {"cars": cars, "after": records[min(len(records), 50)-1].id if records else after,
            "more": len(records) > 50, "total": count, "status": "ready", "coverage": "partial"}
