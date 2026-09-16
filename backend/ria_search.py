"""Bounded, authenticated search. Catalog IDs are resolved from official APIs.

No paid valuation API: estimates use at least five independently retrieved peers.
No search/valuation result is ingested into the notification pipeline.
"""
import hashlib
import json
import math
import re
import statistics
import time
import uuid
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json, listing_preview
from .models import Filters, SourceBudget, SourceCache, SourceProbe


def normalize(value):
    return " ".join(value.casefold().replace("i", "і").split())


def initialize_budget(engine):
    with Session(engine) as db:
        if db.get(SourceBudget, "auto_ria") is None:
            db.add(SourceBudget(id="auto_ria", calls=[], total=2))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()


def valid_id(value):
    return type(value) is int and value > 0


def parse_ids(data):
    try:
        result = data["result"]["search_result"]
        ids = result["ids"]
        if not isinstance(ids, list) or type(result["count"]) is not int:
            raise ValueError()
        parsed = list(dict.fromkeys(str(i) for i in ids))
        if any(not re.fullmatch(r"[1-9][0-9]{0,11}", i) for i in parsed):
            raise ValueError()
        return {"ids": parsed[:10], "total": max(0, result["count"])}
    except (KeyError, ValueError, TypeError):
        raise RiaError("invalid_response") from None


def parse_catalog(data):
    if not isinstance(data, list):
        raise RiaError("invalid_response")
    values = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not valid_id(item.get("value")):
            raise RiaError("invalid_response")
        values.append({"name": item["name"][:150], "value": item["value"]})
    return {"items": values}


def parse_car(data, source_id):
    preview = listing_preview(data, source_id)
    auto = data["autoData"]
    state = data.get("stateData") or {}
    flags = data.get("autoInfoBar") or {}
    mileage = auto.get("raceInt")
    if type(mileage) not in (int, float) or not math.isfinite(mileage) or mileage < 0:
        raise RiaError("invalid_response")
    def label(value):
        return value[:150] if isinstance(value, str) else ""
    def ident(value):
        return value if valid_id(value) else None
    photo = (data.get("photoData") or {}).get("seoLinkM", "")
    url = urlsplit(photo) if isinstance(photo, str) else urlsplit("")
    photo = photo if url.scheme == "https" and (url.hostname or "").endswith(".riastatic.com") else None
    return {**preview, "brand": label(data.get("markName")), "model": label(data.get("modelName")),
            "brand_id": ident(data.get("markId")), "model_id": ident(data.get("modelId")),
            "region_id": ident(state.get("stateId")), "region": label(state.get("regionName")),
            "body_id": ident(auto.get("bodyId")), "body": label(data.get("subCategoryName")),
            "fuel_id": ident(auto.get("fuelId")), "fuel": label(auto.get("fuelName")),
            "gear_id": ident(auto.get("gearBoxId")), "transmission": label(auto.get("gearboxName")),
            "generation_id": ident(auto.get("generationId")), "modification_id": ident(auto.get("modificationId")),
            "mileage": round(mileage * 1000), "image": photo,
            "comparable_condition": (data.get("technicalCondition") or {}).get("id") == 1
                and all(flags.get(k) is False for k in ("damage", "onRepairParts", "abroad", "custom")),
            "observed_at": time.time()}


def estimate(candidate, peers):
    keys = ("brand_id", "model_id", "generation_id", "modification_id", "body_id", "fuel_id", "gear_id")
    result = {"market": None, "discount": None, "comparables": 0, "valuation": "insufficient_data"}
    if not candidate["comparable_condition"] or not all(candidate.get(k) for k in keys):
        return result
    unique = {p["id"]: p for p in peers if p["id"] != candidate["id"]}
    prices = [p["price_usd"] for p in unique.values()
              if p["comparable_condition"] and all(p.get(k) == candidate[k] for k in keys)
              and abs(p["year"] - candidate["year"]) <= 1
              and abs(p["mileage"] - candidate["mileage"]) <= max(30000, candidate["mileage"] * .2)]
    result["comparables"] = len(prices)
    if len(prices) < 5:
        return result
    market = statistics.median(prices)
    # A very mixed sample must not produce a confident-looking discount.
    if max(prices) / min(prices) > 2:
        return {**result, "valuation": "mixed_sample"}
    return {"market": market, "discount": round((1 - candidate["price_usd"] / market) * 100, 1),
            "comparables": len(prices), "valuation": "sample_median"}


class RiaSearch:
    def __init__(self, engine, key, fetch=fetch_json):
        self.engine, self.key, self.fetch = engine, key, fetch
        self.owner = uuid.uuid4().hex
        self.deadline = time.monotonic() + 42
        self.stage = "acquire"

    def acquire(self):
        if not self.key:
            raise RiaError("not_configured")
        with Session(self.engine) as db:
            row = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
            if row.busy_until > time.time():
                raise RiaError("busy")
            row.owner, row.busy_until = self.owner, time.time() + 90
            db.commit()

    def release(self):
        with Session(self.engine) as db:
            row = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
            if row.owner == self.owner:
                row.busy_until = 0
                db.commit()

    def request(self, path, params, parser, ttl=900):
        self.stage = path
        digest = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        with Session(self.engine) as db:
            cached = db.get(SourceCache, digest)
            if cached and cached.expires_at > time.time():
                return cached.payload
            row = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
            now = time.time()
            calls = [t for t in row.calls if t > now - 86400]
            if row.owner != self.owner or time.monotonic() > self.deadline:
                raise RiaError("search_limit")
            if row.blocked_until > now or len([t for t in calls if t > now - 3600]) >= 24 or len(calls) >= 60 or row.total >= 900:
                raise RiaError("quota_exceeded")
            row.calls, row.total = calls + [now], row.total + 1
            db.commit()  # Failed calls consume budget too, even on a process crash.
        try:
            payload = parser(self.fetch(self.key, path, params))
        except RiaError as exc:
            if str(exc) in {"quota_exceeded", "key_rejected", "access_denied"}:
                with Session(self.engine) as db:
                    row = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
                    row.blocked_until = time.time() + 3600
                    db.commit()
            raise
        with Session(self.engine) as db:
            db.execute(delete(SourceCache).where(SourceCache.expires_at < time.time()))
            row = db.get(SourceCache, digest)
            if row is None:
                row = SourceCache(id=digest)
                db.add(row)
            row.payload, row.expires_at = payload, time.time() + ttl
            db.commit()
        return payload

    def resolve(self, path, names):
        if not names:
            return []
        catalog = self.request(path, {}, parse_catalog, ttl=7 * 86400)["items"]
        result = []
        for name in names:
            matches = {item["value"] for item in catalog if normalize(item["name"]) == normalize(name)}
            if len(matches) != 1:
                raise RiaError("unsupported_filter")
            result.append(matches.pop())
        return result

    def parameters(self, filters):
        params = {"category_id": 1, "searchType": 4, "status_id": 0, "page": 0,
                  "order_by": 7, "countpage": 3, "currency": 1}
        ids = {}
        if filters.model and not filters.brand:
            raise RiaError("unsupported_filter")
        if filters.brand:
            ids["brand_id"] = self.resolve("categories/1/marks", [filters.brand])[0]
            params["marka_id[0]"] = ids["brand_id"]
        if filters.model:
            ids["model_id"] = self.resolve(f"categories/1/marks/{ids['brand_id']}/models", [filters.model])[0]
            params["model_id[0]"] = ids["model_id"]
        if filters.region:
            ids["region_id"] = self.resolve("states", [filters.region.removesuffix(" область")])[0]
            params["state[0]"] = ids["region_id"]
        for field, path, param, id_field in (
            ("body", "categories/1/bodystyles", "bodystyle", "body_id"),
            ("fuel", "type", "type", "fuel_id"),
            ("transmission", "categories/1/gearboxes", "gearbox", "gear_id")):
            values = self.resolve(path, getattr(filters, field))
            ids[id_field] = values
            for i, value in enumerate(values):
                params[f"{param}[{i}]"] = value
        for field, low, high in (("price", "price_ot", "price_do"), ("year", "s_yers[0]", "po_yers[0]"),
                                 ("mileage", "raceFrom", "raceTo")):
            value = getattr(filters, field)
            for boundary, name in ((value.from_, low), (value.to, high)):
                if boundary is not None:
                    if field == "year" and boundary != int(boundary):
                        raise RiaError("unsupported_filter")
                    params[name] = math.floor(boundary) if name == low else math.ceil(boundary)
        return params, ids

    def car(self, source_id):
        return self.request("info", {"auto_id": source_id}, lambda raw: parse_car(raw, source_id))

    def comparisons(self, candidate):
        required = ("brand_id", "model_id", "generation_id", "modification_id", "body_id", "fuel_id", "gear_id")
        if not candidate["comparable_condition"] or not all(candidate.get(k) for k in required):
            return []
        # Independent of the user's budget and region, preventing a price-capped median.
        params = {"category_id": 1, "searchType": 4, "status_id": 0, "page": 0, "countpage": 7,
                  "order_by": 7, "marka_id[0]": candidate["brand_id"], "model_id[0]": candidate["model_id"],
                  "generation_id[0][0]": candidate["generation_id"], "bodystyle[0]": candidate["body_id"],
                  "type[0]": candidate["fuel_id"], "gearbox[0]": candidate["gear_id"],
                  "s_yers[0]": candidate["year"] - 1, "po_yers[0]": candidate["year"] + 1,
                  "damage": 1, "abroad": 2, "custom": 1}
        ids = self.request("search", params, parse_ids)["ids"]
        peers = []
        for source_id in [i for i in ids if i != candidate["id"]][:6]:
            try:
                peers.append(self.car(source_id))
            except RiaError as exc:
                if str(exc) not in {"listing_unavailable", "invalid_response"}:
                    raise
        return peers

    def search(self, filters):
        self.acquire()
        try:
            params, ids = self.parameters(filters)
            results = self.request("search", params, parse_ids)
            cars, warnings = [], []
            inspected = 0
            for source_id in results["ids"][:3]:
                try:
                    car = self.car(source_id)
                    inspected += 1
                    if not matches(car, filters, ids):
                        continue
                    rating = estimate(car, [])
                    try:
                        rating = estimate(car, self.comparisons(car))
                    except RiaError as exc:
                        warnings.append(str(exc))
                    car.update(rating)
                    if not filters.onlyDeals or (car["market"] and car["price_usd"] <= car["market"] * .85):
                        cars.append(car)
                except RiaError as exc:
                    warnings.append(str(exc))
                    if str(exc) not in {"listing_unavailable", "invalid_response"}:
                        break
            return {"cars": cars, "source_total": results["total"], "inspected": inspected,
                    "limited": True, "warnings": sorted(set(warnings)), "checked_at": time.time(),
                    "source": "AUTO.RIA", "source_url": "https://auto.ria.com/"}
        finally:
            self.release()


def matches(car, filters, ids):
    for key, value in ids.items():
        if isinstance(value, list):
            if value and car.get(key) not in value:
                return False
        elif car.get(key) != value:
            return False
    for value, span in ((car["price_usd"], filters.price), (car["year"], filters.year),
                        (car["mileage"] / 1000, filters.mileage)):
        if (span.from_ is not None and value < span.from_) or (span.to is not None and value > span.to):
            return False
    return True


def verify_search_once(engine, key):
    """Deployment smoke check for the user's Volkswagen / Khmelnytskyi search.

    Persisted once-only claim; shares the exact same cache and quota as users.
    No public route can initiate or reset this check.
    """
    if not key:
        return
    check_id = "auto-ria-filter-check-v2"
    with Session(engine) as db:
        db.add(SourceProbe(id=check_id, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    status, result = "check_failed", {}
    search = RiaSearch(engine, key)
    try:
        data = search.search(Filters(brand="Volkswagen", region="Хмельницька область", onlyDeals=False))
        status = "verified" if data["inspected"] else "no_verified_details"
        result = {"inspected": data["inspected"], "returned": len(data["cars"]),
                  "source_total": data["source_total"], "warnings": data["warnings"],
                  "valued": sum(car["market"] is not None for car in data["cars"])}
    except RiaError as exc:
        status = str(exc)
    except Exception as exc:
        # Class + fixed stage only: never exception messages or upstream URLs.
        result = {"error_type": type(exc).__name__, "stage": search.stage}
    with Session(engine) as db:
        row = db.get(SourceProbe, check_id)
        row.status, row.result, row.checked_at = status, result, time.time()
        db.commit()
