"""Compile normalized radars into the existing discovery/evaluation pipeline.

Search rows are private execution branches, never a second configuration store.
They keep existing frozen-window pagination, source leases and delivery claims.
The migration marker gates legacy rows even during a rolling backend release.
"""
import hashlib
import math
import time

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from .billing_models import Entitlement
from .models import (Filters, MonitorMatch, MonitorMembership, MonitorSeen,
                     MonitorWatch, Search, StarsTestOrder, User)
from .radar_models import MIGRATION_VERSION, Radar, RadarMigration, RadarSearch
from .radar_service import PRICE_BOUNDS



def marker_clause():
    return exists(select(RadarMigration.version).where(RadarMigration.version == MIGRATION_VERSION))


def enabled(db):
    transaction = db.get_transaction()
    cached = db.info.get("radar_mode_transaction")
    if cached and cached[0] is transaction:
        return cached[1]
    value = bool(db.scalar(select(marker_clause())))
    db.info["radar_mode_transaction"] = (db.get_transaction(), value)
    return value


def subscription_clause(uid, now=None):
    """Same time-based entitlement interpretation as billing.expiry, no whitelist."""
    now = time.time() if now is None else now
    return or_(exists(select(Entitlement.user_id).where(Entitlement.user_id == uid,
        Entitlement.expires_at > now)), exists(select(StarsTestOrder.id).where(
        StarsTestOrder.user_id == uid, StarsTestOrder.state.in_(
            ("paid", "refund_sending", "refund_uncertain")), StarsTestOrder.paid_until > now)))


def subscription_until(uid):
    entitlement = func.coalesce(select(Entitlement.expires_at).where(Entitlement.user_id == uid).scalar_subquery(), 0)
    pilot = func.coalesce(select(func.max(StarsTestOrder.paid_until)).where(StarsTestOrder.user_id == uid,
        StarsTestOrder.state.in_(("paid", "refund_sending", "refund_uncertain"))).scalar_subquery(), 0)
    return case((entitlement >= pilot, entitlement), else_=pilot)


def search_clause(search_id=Search.id, uid=Search.user_id, now=None):
    """Reject unbound legacy rows and paused/expired radars in every source query."""
    now = time.time() if now is None else now
    binding = exists(select(RadarSearch.search_id).join(Radar, Radar.id == RadarSearch.radar_id)
        .where(RadarSearch.search_id == search_id, Radar.user_id == uid,
               Radar.status == "active", Radar.active_until > now, subscription_clause(uid, now)))
    return ~marker_clause() | binding


def remove_radar(db, radar):
    """Caller owns the user lock and transaction; never delete delivery history."""
    ids = list(db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id == radar.id)))
    if not ids:
        return
    for model, column in ((MonitorMatch, MonitorMatch.search_id), (MonitorSeen, MonitorSeen.search_id),
                          (MonitorMembership, MonitorMembership.search_id), (MonitorWatch, MonitorWatch.search_id)):
        db.execute(delete(model).where(column.in_(ids)))
    db.execute(delete(RadarSearch).where(RadarSearch.search_id.in_(ids)))
    db.execute(delete(Search).where(Search.id.in_(ids)))
    db.flush()


def sync_radar(db, radar):
    """Atomically regenerate private branches after a configuration/epoch change."""
    from .radar_service import config
    from .monitor import reset_watch
    data = config(db, radar)
    remove_radar(db, radar)
    low, high = PRICE_BOUNDS[radar.price_category]
    # The existing source matcher uses inclusive bounds. One representable step
    # below the category end gives [low, high) exactly for JSON USD numbers.
    # AUTO.RIA parameters round this upper bound upwards, then post-filter it.
    upper = math.nextafter(float(high), -math.inf) if high is not None else None
    regions = [r["name"] for r in data["regions"]]
    active = (radar.status == "active" and radar.active_until > time.time()
              and bool(db.scalar(select(subscription_clause(radar.user_id)))))
    for brand in data["brands"]:
        models = brand["models"] or [{"model_id": None, "name": ""}]
        for model in models:
            filters = Filters(brand=brand["brand_name"], model=model["name"], region=regions,
                price={"from": low, "to": upper}, year={"from": radar.year_from, "to": radar.year_to},
                onlyDeals=True, minDiscount=float(radar.min_profit))
            fingerprint = hashlib.sha256(("radar:" + radar.id + ":" + str(brand["brand_id"]) + ":"
                + str(model["model_id"]) + ":" + filters.fingerprint()).encode()).hexdigest()
            row = Search(user_id=radar.user_id, name=radar.name, filters=filters.canonical(),
                         fingerprint=fingerprint, enabled=active, after_listing=0)
            db.add(row)
            db.flush()
            db.add(RadarSearch(search_id=row.id, radar_id=radar.id))
            if active:
                reset_watch(db, row.id, True)
    db.flush()


def pause_user(db, uid, reason="paused", now=None, *, radar_ids=None):
    """Pause without losing normalized filters; renewal never invokes resume."""
    from .monitor import reset_watch
    now = time.time() if now is None else now
    query = select(Radar).where(Radar.user_id == uid, Radar.status == "active")
    if radar_ids is not None:
        query = query.where(Radar.id.in_(radar_ids))
    radars = list(db.scalars(query))
    ids = [row.id for row in radars]
    if not ids:
        return 0
    search_ids = list(db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id.in_(ids))))
    for sid in search_ids:
        reset_watch(db, sid, False)
    if search_ids:
        db.execute(update(Search).where(Search.id.in_(search_ids)).values(enabled=False))
    for row in radars:
        row.status, row.active_until, row.updated_at, row.revision = reason, 0, now, row.revision + 1
    return len(radars)


def sync_expired(bind, uid=None, now=None):
    """Own a worker transaction, or participate in the API caller's transaction."""
    if not isinstance(bind, Session):
        with Session(bind) as db:
            changed = sync_expired(db, uid=uid, now=now)
            if changed:
                db.commit()
            return changed
    db = bind
    if not enabled(db):
        return 0
    checked_at = time.time() if now is None else now
    # If access is visibly continuous *before* the old expiry, extend the
    # activation's ceiling. An elapsed ceiling is never repaired by renewal.
    until = subscription_until(Radar.user_id)
    query = select(Radar.user_id).where(Radar.status == "active",
        (Radar.active_until <= checked_at) | ~subscription_clause(Radar.user_id, checked_at)
        | (until > Radar.active_until))
    if uid is not None:
        query = query.where(Radar.user_id == uid)
    users = list(db.scalars(query.distinct().order_by(Radar.user_id)))
    changed = 0
    for owner_id in users:
        db.scalar(select(User).where(User.id == owner_id).with_for_update())
        # An old active_until still pauses after renewal: resume is explicit.
        checked_at = time.time() if now is None else now
        changed += db.execute(update(Radar).where(Radar.user_id == owner_id, Radar.status == "active",
            Radar.active_until > checked_at, until > Radar.active_until).values(active_until=until)
            .execution_options(synchronize_session="fetch")).rowcount
        ids = list(db.scalars(select(Radar.id).where(Radar.user_id == owner_id, Radar.status == "active",
            (Radar.active_until <= checked_at) | ~subscription_clause(owner_id, checked_at))))
        if ids:
            changed += pause_user(db, owner_id, "subscription_expired", checked_at, radar_ids=ids)
    return changed


def parameters(db, filters):
    """Use normalized official catalog IDs; never fetch dictionaries during polling."""
    from .radar_catalog import resolve
    from .ria_search import RiaSearch
    return RiaSearch.parameters(None, filters, resolver=lambda path, names: resolve(db, path, names))


def local_parameters(engine, filters, fallback):
    with Session(engine) as db:
        if enabled(db):
            return parameters(db, filters)
    return fallback(filters)
