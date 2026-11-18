"""Normalized Radar configuration; existing Search rows are derived executors only."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Float, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

MIGRATION_VERSION = "20261118_radars_v1"


class RadarMigration(Base):
    __tablename__ = "radar_schema_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[float] = mapped_column(Float)


class BrandCatalog(Base):
    __tablename__ = "radar_catalog_brands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    models_available: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelCatalog(Base):
    __tablename__ = "radar_catalog_models"
    brand_id: Mapped[int] = mapped_column(ForeignKey("radar_catalog_brands.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(150))
    __table_args__ = (UniqueConstraint("brand_id", "name", name="radar_catalog_model_name"),)


class RegionCatalog(Base):
    __tablename__ = "radar_catalog_regions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(150), unique=True)


class Radar(Base):
    __tablename__ = "radars"
    __table_args__ = (
        UniqueConstraint("user_id", "slot", name="radar_owner_slot"),
        UniqueConstraint("user_id", "create_key", name="radar_owner_create_key"),
        CheckConstraint("slot BETWEEN 1 AND 3", name="radar_three_slots"),
        CheckConstraint("price_category BETWEEN 1 AND 6", name="radar_price_category"),
        CheckConstraint("year_from >= 1900 AND year_to <= 2100 AND year_to >= year_from", name="radar_year_range"),
        CheckConstraint("min_profit IN (5,10,15,20,30,40,50)", name="radar_profit_choices"),
        CheckConstraint("status IN ('active','paused','subscription_expired')", name="radar_status"),
        Index("radar_owner_status", "user_id", "status"),
        Index("radar_active_updated", "status", "updated_at"),
        Index("radar_active_expiry", "status", "active_until"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    slot: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60))
    price_category: Mapped[int] = mapped_column(Integer)
    year_from: Mapped[int] = mapped_column(Integer)
    year_to: Mapped[int] = mapped_column(Integer)
    min_profit: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="paused")
    active_until: Mapped[float] = mapped_column(Float, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    create_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RadarBrand(Base):
    __tablename__ = "radar_brands"
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("radar_catalog_brands.id"), primary_key=True)


class RadarModel(Base):
    __tablename__ = "radar_models"
    radar_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    brand_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["radar_id", "brand_id"], ["radar_brands.radar_id", "radar_brands.brand_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["brand_id", "model_id"], ["radar_catalog_models.brand_id", "radar_catalog_models.id"]),
    )


class RadarRegion(Base):
    __tablename__ = "radar_regions"
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("radar_catalog_regions.id"), primary_key=True)


class RadarSearch(Base):
    __tablename__ = "radar_search_bindings"
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True)
    radar_id: Mapped[str] = mapped_column(ForeignKey("radars.id", ondelete="CASCADE"), index=True)


class RadarLegacySearchState(Base):
    """One-time activation backup; not user-facing configuration or a new pipeline."""
    __tablename__ = "radar_legacy_search_states"
    search_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean)
    captured_at: Mapped[float] = mapped_column(Float)


RADAR_TABLES = [cls.__table__ for cls in (RadarMigration, BrandCatalog, ModelCatalog,
    RegionCatalog, Radar, RadarBrand, RadarModel, RadarRegion, RadarSearch, RadarLegacySearchState)]
