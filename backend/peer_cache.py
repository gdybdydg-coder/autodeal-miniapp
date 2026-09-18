"""Reuse only details actually retrieved for comparable searches, for 15 minutes."""
import copy
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import ValuationPeer
from .valuation import FIELDS, MAX_AGE, comparable, group_key, reasons
from . import reference_valuation as reference
from . import ria_market_range


def observe(engine, car, *, create=False):
    with Session(engine) as db:
        row = db.get(ValuationPeer, car["id"])
        if row and (row.observed_at > car["observed_at"] or
                    (not row.available and row.observed_at >= car["observed_at"])):
            if not row.available:
                raise RiaError("listing_unavailable")
            return copy.deepcopy(row.car)
        if row is None and not create:
            return car
        if row is None:
            row = ValuationPeer(source_id=car["id"])
            db.add(row)
        row.car, row.observed_at, row.available = copy.deepcopy(car), car["observed_at"], True
        row.group_key = group_key(car) if not reasons(car, time.time()) else ""
        db.execute(delete(ValuationPeer).where(ValuationPeer.observed_at < time.time() - MAX_AGE))
        db.commit()
        return car


def invalidate(engine, source_id):
    with Session(engine) as db:
        row = db.get(ValuationPeer, source_id)
        if row is None:
            row = ValuationPeer(source_id=source_id, car={})
            db.add(row)
        row.group_key, row.available, row.observed_at = "", False, time.time()
        db.commit()


def candidates(engine, candidate, limit=20):
    now = time.time()
    key = group_key(candidate)
    if not key or reasons(candidate, now):
        return []
    with Session(engine) as db:
        rows = db.scalars(select(ValuationPeer).where(ValuationPeer.group_key == key,
            ValuationPeer.source_id != candidate["id"], ValuationPeer.available.is_(True),
            ValuationPeer.observed_at >= now - MAX_AGE))
        peers = [copy.deepcopy(row.car) for row in rows if not comparable(candidate, row.car, now)]
    # Match closeness/freshness, never price, chooses the reused sample.
    peers.sort(key=lambda peer: (abs(peer["year"] - candidate["year"]),
        abs(peer["mileage"] - candidate["mileage"]), -peer["observed_at"], peer["id"]))
    return peers[:limit]


def evidence_current(db, evidence):
    """A known removal or changed observation supersedes even unexpired proof."""
    used = {car["id"]: car for car in [evidence["candidate"], *evidence["peers"]]}
    for row in db.scalars(select(ValuationPeer).where(ValuationPeer.source_id.in_(used))):
        prior = used[row.source_id]
        if row.observed_at >= prior["observed_at"]:
            fields = reference.EVIDENCE_FIELDS if evidence.get("version") == reference.VERSION else FIELDS
            if evidence.get("version") == ria_market_range.VERSION:
                fields = ria_market_range.EVIDENCE_FIELDS
            if not row.available or any(row.car.get(key) != prior.get(key) for key in fields if key != "observed_at"):
                return False
    return True


def reference_candidates(engine, candidate, limit=20):
    """Reuse only prior comparison observations, never the user's cheap candidates."""
    now = time.time()
    if reference.reasons(candidate, now):
        return []
    with Session(engine) as db:
        rows = db.scalars(select(ValuationPeer).where(ValuationPeer.available.is_(True),
            ValuationPeer.source_id != candidate["id"], ValuationPeer.observed_at >= now - MAX_AGE,
            ValuationPeer.car["brand_id"].as_integer() == candidate["brand_id"],
            ValuationPeer.car["model_id"].as_integer() == candidate["model_id"])
            .order_by(ValuationPeer.observed_at.desc(), ValuationPeer.source_id).limit(200))
        peers = [copy.deepcopy(row.car) for row in rows if not reference.comparable(candidate, row.car, now)]
    peers.sort(key=lambda peer: (abs(peer["year"] - candidate["year"]), -peer["observed_at"], peer["id"]))
    return peers[:limit]
