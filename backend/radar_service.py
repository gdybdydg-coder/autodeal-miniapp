"""Radar ownership, atomic slot allocation, configuration and subscription lifecycle."""
import time
import uuid
import hashlib
import json
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select, text, tuple_, update

from . import billing
from .models import User
from .radar_models import (BrandCatalog, ModelCatalog, Radar, RadarBrand,
                          RadarModel, RadarRegion, RegionCatalog)

PRICE_BOUNDS = {1:(0,2000), 2:(2000,5000), 3:(5000,10000), 4:(10000,15000),
                5:(15000,20000), 6:(20000,None)}
PERCENTAGES = frozenset({5,10,15,20,30,40,50})
Identifier = Annotated[int, Field(strict=True, gt=0, lt=2**31)]


class RadarError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BrandSelection(StrictInput):
    brand_id: Identifier
    model_ids: list[Identifier] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_models(self):
        if len(set(self.model_ids)) != len(self.model_ids):
            raise ValueError("Не вибирайте одну модель двічі.")
        return self


class RadarInput(StrictInput):
    name: str = Field(min_length=1, max_length=60)
    all_brands: bool = False
    brands: list[BrandSelection] = Field(max_length=30)
    price_category: int = Field(ge=1, le=6)
    region_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    year_from: int = Field(ge=1900, le=2100)
    year_to: int = Field(ge=1900, le=2100)
    min_profit: int

    @model_validator(mode="after")
    def validate_configuration(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Вкажіть назву радара.")
        if self.year_to < self.year_from:
            raise ValueError("Рік до не може бути раніше за рік від.")
        if self.year_to > datetime.now().year + 1:
            raise ValueError("Виберіть рік зі списку доступних років.")
        if self.min_profit not in PERCENTAGES:
            raise ValueError("Виберіть один із запропонованих відсотків.")
        if self.all_brands and self.brands:
            raise ValueError("Виберіть усі марки або окремі марки, а не обидва варіанти одночасно.")
        if not self.all_brands and not self.brands:
            raise ValueError("Виберіть принаймні одну марку або позначте «Усі марки».")
        if len({b.brand_id for b in self.brands}) != len(self.brands):
            raise ValueError("Не вибирайте одну марку двічі.")
        if len(set(self.region_ids)) != len(self.region_ids):
            raise ValueError("Не вибирайте одну область двічі.")
        if sum(max(1,len(b.model_ids)) for b in self.brands) > 200:
            raise ValueError("В одному радарі можна вибрати до 200 моделей.")
        return self


class RadarEdit(RadarInput):
    revision: int = Field(ge=1)


class RadarCreate(RadarInput):
    client_request_id: str | None = Field(default=None,min_length=16,max_length=64,pattern=r"^[A-Za-z0-9_-]+$")


class RadarStatus(StrictInput):
    active: bool
    revision: int = Field(ge=1)


def lock_user(db, uid):
    """Same owner serialization as delivery; no-op UPDATE also serializes SQLite."""
    # First statement is a write: SQLite never upgrades a stale read transaction.
    # PostgreSQL/SQLite both support the same race-safe owner insert.
    db.execute(text("INSERT INTO users (id,ready,last_update,last_command_at) "
                    "VALUES (:uid,false,-1,0) ON CONFLICT (id) DO NOTHING"),{"uid":uid})
    db.execute(update(User).where(User.id == uid).values(last_update=User.last_update))
    return db.scalar(select(User).where(User.id == uid).with_for_update().execution_options(populate_existing=True))


def monitoring_allowed(db, uid, now=None):
    return billing.expiry(db, uid) > (time.time() if now is None else now)


def access(db, uid, now=None):
    until = billing.expiry(db, uid)
    return {"active": until > (time.time() if now is None else now), "expires_at": until or None}


def owned(db, uid, radar_id, revision=None):
    radar = db.scalar(select(Radar).where(Radar.id == radar_id, Radar.user_id == uid))
    if radar is None:
        raise RadarError(404, "radar_not_found", "Радар не знайдено.")
    if revision is not None and radar.revision != revision:
        raise RadarError(409, "radar_changed", "Радар уже змінено в іншому вікні. Оновіть список і повторіть дію.")
    return radar


def validate_catalog(db, payload):
    brands = {b.brand_id for b in payload.brands}
    valid = set(db.scalars(select(BrandCatalog.id).where(BrandCatalog.id.in_(brands))))
    if brands != valid:
        raise RadarError(422, "brand_not_found", "Виберіть марки з довідника AUTO.RIA.")
    selected = {(b.brand_id, ident) for b in payload.brands for ident in b.model_ids}
    if selected:
        valid_models = set(db.execute(select(ModelCatalog.brand_id, ModelCatalog.id).where(
            tuple_(ModelCatalog.brand_id,ModelCatalog.id).in_(selected))).all())
        if not selected <= valid_models:
            raise RadarError(422, "model_brand_mismatch", "Вибрана модель не належить до відповідної марки або відсутня в довіднику.")
    if payload.region_ids:
        valid_regions = set(db.scalars(select(RegionCatalog.id).where(RegionCatalog.id.in_(payload.region_ids))))
        if set(payload.region_ids) != valid_regions:
            raise RadarError(422, "region_not_found", "Виберіть області з довідника AUTO.RIA.")


def write_configuration(db, radar, payload, *, replace=False):
    validate_catalog(db, payload)
    for field in ("name", "price_category", "year_from", "year_to", "min_profit"):
        setattr(radar, field, getattr(payload, field))
    db.flush()
    if replace:
        db.execute(delete(RadarModel).where(RadarModel.radar_id == radar.id))
        db.execute(delete(RadarBrand).where(RadarBrand.radar_id == radar.id))
        db.execute(delete(RadarRegion).where(RadarRegion.radar_id == radar.id))
    db.add_all(RadarBrand(radar_id=radar.id, brand_id=b.brand_id) for b in payload.brands)
    db.flush()
    db.add_all(RadarModel(radar_id=radar.id, brand_id=b.brand_id, model_id=model)
               for b in payload.brands for model in b.model_ids)
    db.add_all(RadarRegion(radar_id=radar.id, region_id=region) for region in payload.region_ids)
    db.flush()


def configurations(db, radars):
    if not radars:
        return []
    by_id = {r.id: {"id":r.id,"slot":r.slot,"name":r.name,"price_category":r.price_category,
        "year_from":r.year_from,"year_to":r.year_to,"min_profit":r.min_profit,
        "status":r.status,"revision":r.revision,"created_at":r.created_at,"updated_at":r.updated_at,
        "all_brands":True,"brands":[],"region_ids":[],"regions":[]} for r in radars}
    brands = {}
    for rid,bid,name in db.execute(select(RadarBrand.radar_id,BrandCatalog.id,BrandCatalog.name)
            .join(BrandCatalog,BrandCatalog.id==RadarBrand.brand_id).where(RadarBrand.radar_id.in_(by_id))
            .order_by(BrandCatalog.name)):
        entry = {"brand_id":bid,"brand_name":name,"model_ids":[],"models":[]}
        brands[(rid,bid)] = entry
        by_id[rid]["all_brands"] = False
        by_id[rid]["brands"].append(entry)
    for rid,bid,mid,name in db.execute(select(RadarModel.radar_id,RadarModel.brand_id,RadarModel.model_id,ModelCatalog.name)
            .join(ModelCatalog,(ModelCatalog.id==RadarModel.model_id)&(ModelCatalog.brand_id==RadarModel.brand_id))
            .where(RadarModel.radar_id.in_(by_id)).order_by(ModelCatalog.name)):
        brands[(rid,bid)]["model_ids"].append(mid)
        brands[(rid,bid)]["models"].append({"model_id":mid,"name":name})
    for rid,ident,name in db.execute(select(RadarRegion.radar_id,RegionCatalog.id,RegionCatalog.name)
            .join(RegionCatalog,RegionCatalog.id==RadarRegion.region_id).where(RadarRegion.radar_id.in_(by_id))
            .order_by(RegionCatalog.name)):
        by_id[rid]["region_ids"].append(ident)
        by_id[rid]["regions"].append({"id":ident,"name":name})
    return [by_id[r.id] for r in radars]


def config(db, radar):
    return configurations(db,[radar])[0]


def create(db, uid, payload):
    lock_user(db,uid)
    create_key = getattr(payload,"client_request_id",None)
    canonical = payload.model_dump(exclude={"client_request_id"})
    if not payload.all_brands:
        # Older mobile retries predate this optional field; preserve their
        # persisted idempotency fingerprint for the unchanged specific mode.
        canonical.pop("all_brands", None)
    create_hash = hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest()
    if create_key:
        prior=db.scalar(select(Radar).where(Radar.user_id==uid,Radar.create_key==create_key))
        if prior:
            if prior.create_hash != create_hash:
                raise RadarError(409,"request_key_reused","Цей запит уже використано для іншого радара. Оновіть список.")
            return config(db,prior)
    occupied = set(db.scalars(select(Radar.slot).where(Radar.user_id==uid)))
    free = next((slot for slot in range(1,4) if slot not in occupied), None)
    if free is None:
        raise RadarError(409,"radar_slots_full","Усі 3 слоти зайняті. Видаліть один радар, щоб створити новий.")
    validate_catalog(db,payload)
    now = time.time()
    radar = Radar(id=uuid.uuid4().hex,user_id=uid,slot=free,name=payload.name,
        price_category=payload.price_category,year_from=payload.year_from,year_to=payload.year_to,
        min_profit=payload.min_profit,status="paused",active_until=0,revision=1,created_at=now,updated_at=now,
        create_key=create_key,create_hash=create_hash if create_key else None)
    db.add(radar)
    write_configuration(db,radar,payload)
    from .radar_monitor import sync_radar
    sync_radar(db,radar)
    return config(db,radar)


def edit(db, uid, radar_id, payload):
    lock_user(db,uid)
    radar = owned(db,uid,radar_id,payload.revision)
    write_configuration(db,radar,payload,replace=True)
    now = time.time()
    if radar.status=="active" and (radar.active_until<=now or not monitoring_allowed(db,uid,now)):
        radar.status,radar.active_until="subscription_expired",0
    radar.revision += 1
    radar.updated_at=now
    from .radar_monitor import sync_radar
    sync_radar(db,radar)
    return config(db,radar)


def set_status(db, uid, radar_id, payload):
    user=lock_user(db,uid)
    radar=owned(db,uid,radar_id,payload.revision)
    now=time.time()
    if payload.active:
        if not monitoring_allowed(db,uid,now):
            raise RadarError(402,"subscription_required","Для запуску радара потрібна активна підписка. Налаштування збережено.")
        if not user.ready:
            raise RadarError(409,"telegram_not_ready","Надішліть /start боту в Telegram, а потім увімкніть радар.")
        radar.status,radar.active_until="active",billing.expiry(db,uid)
    else:
        radar.status,radar.active_until="paused",0
    radar.revision+=1
    radar.updated_at=now
    from .radar_monitor import sync_radar
    sync_radar(db,radar)
    return config(db,radar)


def remove(db, uid, radar_id, revision):
    lock_user(db,uid)
    radar=owned(db,uid,radar_id,revision)
    from .radar_monitor import remove_radar
    remove_radar(db,radar)
    # Explicit child deletion also protects legacy SQLite connections without FK PRAGMA.
    db.execute(delete(RadarModel).where(RadarModel.radar_id==radar.id))
    db.execute(delete(RadarBrand).where(RadarBrand.radar_id==radar.id))
    db.execute(delete(RadarRegion).where(RadarRegion.radar_id==radar.id))
    db.delete(radar)
    db.flush()
