"""Transactional, repeatable Radar activation; never copies legacy configurations."""
import time

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .models import Base, FullScan, Search, User
from .radar_models import (MIGRATION_VERSION, RADAR_TABLES, RadarLegacySearchState,
                          RadarMigration)


def migrate(engine):
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(731823, 20261118)"))
        Base.metadata.create_all(connection, tables=RADAR_TABLES)
        with Session(bind=connection) as db:
            if db.get(RadarMigration, MIGRATION_VERSION):
                return {"applied": False, "version": MIGRATION_VERSION}
            from .radar_catalog import import_existing_cache
            import_existing_cache(db)
            now = time.time()
            # Serialize cutover with commands and notification ownership checks.
            list(db.scalars(select(User.id).order_by(User.id).with_for_update()))
            for search_id, enabled in db.execute(select(Search.id, Search.enabled)):
                db.add(RadarLegacySearchState(search_id=search_id, enabled=enabled, captured_at=now))
            db.execute(update(Search).values(enabled=False))
            from .full_scan import ACTIVE
            db.execute(update(FullScan).where(FullScan.status.in_(ACTIVE)).values(status="paused", error=""))
            db.add(RadarMigration(version=MIGRATION_VERSION, applied_at=now))
            db.flush()
    return {"applied": True, "version": MIGRATION_VERSION}
