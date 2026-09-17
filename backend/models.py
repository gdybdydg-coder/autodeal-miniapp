import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import BigInteger, Boolean, Float, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Range(StrictModel):
    from_: Annotated[float | None, Field(alias="from", ge=0)] = None
    to: Annotated[float | None, Field(ge=0)] = None

    @model_validator(mode="after")
    def ordered(self):
        if self.from_ is not None and self.to is not None and self.from_ > self.to:
            raise ValueError("From exceeds to")
        return self


Choice = Annotated[str, Field(min_length=1, max_length=150)]


class Filters(StrictModel):
    brand: str = Field(default="", max_length=150)
    model: str = Field(default="", max_length=150)
    region: str = Field(default="", max_length=150)
    price: Range = Field(default_factory=Range)
    year: Range = Field(default_factory=Range)
    mileage: Range = Field(default_factory=Range)
    body: list[Choice] = Field(default_factory=list, max_length=30)
    fuel: list[Choice] = Field(default_factory=list, max_length=30)
    transmission: list[Choice] = Field(default_factory=list, max_length=30)
    onlyDeals: bool = True
    minDiscount: float = Field(default=15, ge=0, le=100, strict=True)

    def canonical(self):
        data = self.model_dump(by_alias=True)
        # Preserve existing subscription fingerprints and activation checkpoints.
        if data["minDiscount"] == 15:
            data.pop("minDiscount")
        for key in ("body", "fuel", "transmission"):
            data[key] = sorted(set(data[key]))
        if "Електро" not in data["fuel"]:
            data["transmission"] = [v for v in data["transmission"] if v != "Редуктор"]
        return data

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


class SearchEditRequest(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    filters: Filters


class SearchRequest(SearchEditRequest):
    enabled: bool = False


class EnabledRequest(StrictModel):
    enabled: bool


class Car(StrictModel):
    source: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    source_id: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    photo: HttpUrl | None = None
    brand: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    region: str = Field(max_length=150)
    body: str = Field(max_length=100)
    fuel: str = Field(max_length=100)
    transmission: str = Field(max_length=100)
    year: int = Field(ge=1900, le=2100)
    mileage: int | None = Field(default=None, ge=0)
    price: float = Field(gt=0)
    market: float | None = Field(default=None, gt=0)
    # A trusted future source/valuation process must supply these, not Mini App.
    comparables: int = Field(ge=0)
    observed_at: float = Field(gt=0)
    valuation_evidence: dict | None = None
    pipeline: dict | None = None

    @model_validator(mode="after")
    def https_only(self):
        if self.url.scheme != "https" or (self.photo and self.photo.scheme != "https"):
            raise ValueError("HTTPS required")
        return self


class Base(DeclarativeBase):
    pass


class SourceProbe(Base):
    __tablename__ = "source_probes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(40))
    checked_at: Mapped[float] = mapped_column(Float)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON)


class SourceBudget(Base):
    __tablename__ = "source_budgets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    calls: Mapped[list] = mapped_column(JSON, default=list)
    total: Mapped[int] = mapped_column(Integer, default=2)
    busy_until: Mapped[float] = mapped_column(Float, default=0)
    blocked_until: Mapped[float] = mapped_column(Float, default=0)
    owner: Mapped[str] = mapped_column(String(40), default="")


class SourceCache(Base):
    __tablename__ = "source_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class ValuationPeer(Base):
    """Short-lived, sanitized comparable observations; not a source-market mirror."""
    __tablename__ = "valuation_peers"
    __table_args__ = (Index("valuation_peer_group_age", "group_key", "observed_at"),)
    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), default="")
    car: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[float] = mapped_column(Float)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class FullScan(Base):
    __tablename__ = "full_scans"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    generation: Mapped[str] = mapped_column(String(32))
    filters: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    error: Mapped[str] = mapped_column(String(40), default="")
    source_total: Mapped[int] = mapped_column(Integer, default=0)
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    checked: Mapped[int] = mapped_column(Integer, default=0)
    unavailable: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    next_run: Mapped[float] = mapped_column(Float, default=0, index=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    owner: Mapped[str] = mapped_column(String(32), default="")


class ScanItem(Base):
    __tablename__ = "scan_items"
    __table_args__ = (UniqueConstraint("scan_id", "source_id"), Index("scan_pending", "scan_id", "state", "id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    car: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deal: Mapped[bool] = mapped_column(Boolean, default=False)
    valued: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[float] = mapped_column(Float, default=0)


class MarketCar(Base):
    """Shared public listing data only: no account, subscription or Telegram data."""
    __tablename__ = "market_cars"
    __table_args__ = (Index("market_brand_model", "brand_id", "model_id", "id"), {"sqlite_autoincrement": True})
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), unique=True)
    car: Mapped[dict] = mapped_column(JSON)
    checked_at: Mapped[float] = mapped_column(Float, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deal: Mapped[bool] = mapped_column(Boolean, index=True)
    brand_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fuel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gear_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float] = mapped_column(Float)
    year: Mapped[int] = mapped_column(Integer)
    mileage: Mapped[int] = mapped_column(Integer)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    last_update: Mapped[int] = mapped_column(BigInteger, default=-1)
    last_command_at: Mapped[int] = mapped_column(BigInteger, default=0)


class Search(Base):
    __tablename__ = "searches"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(60))
    filters: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    after_listing: Mapped[int] = mapped_column(Integer, default=0)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(100))
    car: Mapped[dict] = mapped_column(JSON)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("user_id", "listing_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    listing_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    retry_at: Mapped[float] = mapped_column(Float, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DeliveryTiming(Base):
    """Server observations; Telegram acceptance is not a phone push receipt."""
    __tablename__ = "delivery_timings"
    delivery_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    queued_at: Mapped[float] = mapped_column(Float)
    discovered_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_added_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    send_started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    telegram_date: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MonitorControl(Base):
    __tablename__ = "monitor_control"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner: Mapped[str] = mapped_column(String(40), default="")
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    heartbeat: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(40), default="starting")


class MonitorWatch(Base):
    __tablename__ = "monitor_watches"
    search_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    epoch: Mapped[str] = mapped_column(String(32))
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    window: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="starting")
    next_poll: Mapped[float] = mapped_column(Float, default=0)
    checked_at: Mapped[float] = mapped_column(Float, default=0)


class MonitorSeen(Base):
    __tablename__ = "monitor_seen"
    search_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(20))
    first_seen: Mapped[float] = mapped_column(Float)


class MonitorMatch(Base):
    __tablename__ = "monitor_matches"
    search_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64))


class MonitorMembership(Base):
    """An explicit activation epoch and its shared, canonical source filter."""
    __tablename__ = "monitor_memberships"
    search_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    epoch: Mapped[str] = mapped_column(String(32))
    feed_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[float] = mapped_column(Float)


class MonitorFeed(Base):
    __tablename__ = "monitor_feeds"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filters: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[float] = mapped_column(Float)
    cursor: Mapped[float] = mapped_column(Float)
    # Frozen date windows, recipient epochs, page and verification-pass progress.
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    next_poll: Mapped[float] = mapped_column(Float, default=0, index=True)
    checked_at: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(40), default="starting")


class MonitorJob(Base):
    """One durable valuation job per source ID, shared by all subscriptions."""
    __tablename__ = "monitor_jobs"
    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    first_seen: Mapped[float] = mapped_column(Float)
    next_run: Mapped[float] = mapped_column(Float, default=0, index=True)
    last_attempt: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(40), default="")
    # Monitor-only evidence; manual search caches cannot authorize notifications.
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class MonitorActiveWindow(Base):
    """A bounded, supplemental first-page check; never a historical scan cursor."""
    __tablename__ = "monitor_active_windows"
    feed_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_poll: Mapped[float] = mapped_column(Float, default=0, index=True)
    checked_at: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(40), default="starting")
    window: Mapped[list] = mapped_column(JSON, default=list)
    source_total: Mapped[int] = mapped_column(Integer, default=0)


class TelegramTest(Base):
    __tablename__ = "telegram_tests"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    attempted_at: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(20))
