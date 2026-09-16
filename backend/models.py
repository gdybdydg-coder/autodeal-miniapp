import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import BigInteger, Boolean, Float, Integer, JSON, String, UniqueConstraint
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

    def canonical(self):
        data = self.model_dump(by_alias=True)
        for key in ("body", "fuel", "transmission"):
            data[key] = sorted(set(data[key]))
        if "Електро" not in data["fuel"]:
            data["transmission"] = [v for v in data["transmission"] if v != "Редуктор"]
        return data

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


class SearchRequest(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    filters: Filters
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
    mileage: int = Field(ge=0)
    price: float = Field(gt=0)
    market: float = Field(gt=0)
    # A trusted future source/valuation process must supply these, not Mini App.
    comparables: int = Field(ge=5)
    observed_at: float = Field(gt=0)

    @model_validator(mode="after")
    def https_only(self):
        if self.url.scheme != "https" or (self.photo and self.photo.scheme != "https"):
            raise ValueError("HTTPS required")
        return self


class Base(DeclarativeBase):
    pass


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
