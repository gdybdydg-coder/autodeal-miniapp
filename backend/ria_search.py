"""Bounded, authenticated search. Catalog IDs are resolved from official APIs.

No paid valuation API: estimates use at least five independently retrieved peers.
No search/valuation result is ingested into the notification pipeline.
"""
import copy
import hashlib
import json
import math
import re
import time
import uuid
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json, listing_preview
from .models import Filters, SourceBudget, SourceCache, SourceProbe
from .ria_budget import BudgetLimits, peer_scan_limit
from . import peer_cache
from .valuation import DIMENSIONS, VERSION, PeerBatch, comparison_dimensions, estimate, is_deal, reasons, vehicle_key
from .launch import source_added_at

FRESH_SECONDS = 900
SNAPSHOT_SECONDS = 86400
PAGE_SIZE = 8
PAGE_REQUEST_LIMIT = 32
COMPARABLE_DIMENSIONS = DIMENSIONS
CATALOG_PATHS = {"brands": "categories/1/marks", "regions": "states",
                 "body": "categories/1/bodystyles", "fuel": "type",
                 "transmission": "categories/1/gearboxes"}
TRANSIENT_ERRORS = {"quota_exceeded", "busy", "search_limit", "connection_error", "upstream_error"}


def snapshot_key(filters, cursor=None):
    criteria = filters.canonical()
    # Switching the deals toggle only filters the already checked candidate cards.
    criteria["onlyDeals"] = False
    return hashlib.sha256(json.dumps(["search-snapshot-v4", VERSION, PAGE_SIZE, criteria, cursor], sort_keys=True).encode()).hexdigest()


def snapshot_view(payload, filters, quota, cached=False, reason=None):
    result = copy.deepcopy(payload)
    result["cached"] = cached
    if cached:
        result["requests_used"] = 0
    result["stale"] = time.time() - result["checked_at"] >= FRESH_SECONDS
    result["quota"] = quota
    if reason:
        result["warnings"] = sorted(set(result["warnings"] + [reason]))
    if result["stale"]:
        result["next_cursor"] = None
        # Historical prices are useful; an old discount must not look current.
        for car in result["cars"]:
            car.update(market=None, discount=None, comparables=0, valuation="stale")
    if filters.onlyDeals:
        result["cars"] = [car for car in result["cars"]
                          if is_deal(car["price_usd"], car["market"])]
    return result


def normalize(value):
    return " ".join(value.casefold().replace("i", "і").split())


def modification_name(value):
    # Never match truncated labels, seller descriptions or a fuzzy engine token.
    if not isinstance(value, str) or not 1 <= len(value) <= 150:
        return ""
    return " ".join(value.split())


def normalize_modification(value):
    return modification_name(value).casefold()


def engine_capacity(fuel_name):
    """Only a labelled litre value from the source field, never seller prose."""
    if not isinstance(fuel_name, str) or len(fuel_name) > 150:
        return None
    value = re.fullmatch(r"[^0-9]*?([0-9]{1,2}(?:[.,][0-9]{1,2})?)\s*(?:л|l)\.?\s*", fuel_name, re.I)
    if not value:
        return None
    litres = float(value[1].replace(",", "."))
    return round(litres * 1000) if .6 <= litres <= 12 else None


def comparable_condition(data):
    flags, technical = data.get("autoInfoBar"), data.get("technicalCondition")
    if not isinstance(flags, dict) or not all(flags.get(key) is False
            for key in ("damage", "onRepairParts", "abroad", "custom")):
        return False
    # Missing optional technicalCondition is not evidence of damage. Still
    # reject any explicitly repaired/damaged/non-running or malformed value.
    if technical is None or (isinstance(technical, dict) and technical.get("id") is None):
        return True
    return isinstance(technical, dict) and type(technical.get("id")) is int and technical["id"] == 1


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


def budget_state(row, now, limits=None):
    limits = limits or BudgetLimits.env()
    if row.total >= limits.total:
        return {"reason": "total", "retry_after_seconds": None}
    calls = sorted(t for t in row.calls if t > now - 86400)
    hourly = [t for t in calls if t > now - 3600]
    waits = []
    if len(hourly) >= limits.hourly:
        waits.append((hourly[-limits.hourly] + 3600, "hourly"))
    if len(calls) >= limits.daily:
        waits.append((calls[-limits.daily] + 86400, "daily"))
    if row.blocked_until > now:
        waits.append((row.blocked_until, "upstream"))
    if waits:
        until, reason = max(waits)
        return {"reason": reason, "retry_after_seconds": max(1, math.ceil(until - now))}
    return {"reason": "available", "retry_after_seconds": 0}


def quota_status(engine, limits=None):
    with Session(engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        return budget_state(row, time.time(), limits) if row else {"reason": "unavailable", "retry_after_seconds": None}


def budget_usage(engine):
    """Read-only local accounting, not the provider's account balance."""
    limits = BudgetLimits.env()
    now = time.time()
    with Session(engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        if row is None:
            return {"status": "unavailable", "limits": limits.public()}
        used = {"hourly": sum(t > now - 3600 for t in row.calls),
                "daily": sum(t > now - 86400 for t in row.calls), "total": row.total}
        return {"status": "local_accounting", "limits": limits.public(), "used": used,
                "remaining": {key: max(0, cap - used[key]) for key, cap in limits.public().items()},
                "total_resets_automatically": False}


def parse_ids(data):
    try:
        result = data["result"]["search_result"]
        ids = result["ids"]
        if not isinstance(ids, list) or type(result["count"]) is not int:
            raise ValueError()
        parsed = list(dict.fromkeys(str(i) for i in ids))
        if any(not re.fullmatch(r"[1-9][0-9]{0,11}", i) for i in parsed):
            raise ValueError()
        return {"ids": parsed[:50], "total": max(0, result["count"])}
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


def parse_modification_catalog(data):
    catalog = parse_catalog(data)
    if any(not 1 <= len(item["name"]) <= 150 for item in data):
        raise RiaError("invalid_response")
    return catalog


def parse_car(data, source_id):
    preview = listing_preview(data, source_id)
    auto = data["autoData"]
    state = data.get("stateData") or {}
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
            "modification_name": modification_name(auto.get("modificationName")),
            "modification_source": "listing" if valid_id(auto.get("modificationId")) else None,
            "engine_cc": engine_capacity(auto.get("fuelName")),
            "mileage": round(mileage * 1000), "image": photo, "vehicle_key": vehicle_key(data.get("VIN")),
            "comparable_condition": comparable_condition(data),
            "source_added_at": source_added_at(data.get("addDate")), "observed_at": time.time()}


class RiaSearch:
    def __init__(self, engine, key, fetch=fetch_json, limits=None):
        self.engine, self.key, self.fetch = engine, key, fetch
        self.limits = limits or BudgetLimits.env()
        self.peer_scan_limit = peer_scan_limit()
        self.owner = uuid.uuid4().hex
        self.deadline = time.monotonic() + 42
        self.stage = "acquire"
        self.observed_at = time.time()
        self.requests_made = 0
        self.request_limit = None

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

    def request(self, path, params, parser, ttl=900, *, force=False):
        self.stage = path
        cache_key = [path, params, "car-v4"] if path == "info" else [path, params]
        digest = hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()
        with Session(self.engine) as db:
            cached = db.get(SourceCache, digest)
            if not force and cached and cached.expires_at > time.time():
                if ttl == FRESH_SECONDS:
                    self.observed_at = min(self.observed_at, cached.expires_at - ttl)
                return cached.payload
            row = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
            now = time.time()
            calls = [t for t in row.calls if t > now - 86400]
            if row.owner != self.owner or time.monotonic() > self.deadline:
                raise RiaError("search_limit")
            if self.request_limit is not None and self.requests_made >= self.request_limit:
                raise RiaError("search_limit")
            if budget_state(row, now, self.limits)["reason"] != "available":
                raise RiaError("quota_exceeded")
            row.calls, row.total = calls + [now], row.total + 1
            db.commit()  # Failed calls consume budget too, even on a process crash.
            self.requests_made += 1
        try:
            payload = parser(self.fetch(self.key, path, params))
        except RiaError as exc:
            if path == "info" and str(exc) in {"listing_unavailable", "invalid_response"}:
                peer_cache.invalidate(self.engine, params["auto_id"])
                with Session(self.engine) as db:
                    db.execute(delete(SourceCache).where(SourceCache.id == digest))
                    db.commit()
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

    def catalog(self, brand=""):
        """Shared official dictionaries. A cached UI read spends no provider calls."""
        cache_id = hashlib.sha256(json.dumps(["ui-catalog-v1", brand]).encode()).hexdigest()
        with Session(self.engine) as db:
            row = db.get(SourceCache, cache_id)
            if row and row.expires_at > time.time():
                return copy.deepcopy(row.payload)
        self.acquire()
        self.request_limit = 6
        try:
            if brand:
                brand_id = self.resolve(CATALOG_PATHS["brands"], [brand])[0]
                result = {"brand": brand, "models": self.request(
                    f"categories/1/marks/{brand_id}/models", {}, parse_catalog, ttl=7 * 86400)["items"]}
            else:
                result = {name: self.request(path, {}, parse_catalog, ttl=7 * 86400)["items"]
                          for name, path in CATALOG_PATHS.items()}
            result["source"] = "AUTO.RIA"
            with Session(self.engine) as db:
                db.merge(SourceCache(id=cache_id, payload=result, expires_at=time.time() + 86400))
                db.commit()
            return result
        finally:
            self.release()

    def continuation(self, filters, state):
        if not state:
            return None
        token = uuid.uuid4().hex
        state = {**state, "filters": snapshot_key(filters), "checked_at": self.observed_at}
        with Session(self.engine) as db:
            db.add(SourceCache(id="cursor-" + token, payload=state,
                               expires_at=self.observed_at + FRESH_SECONDS))
            db.commit()
        return token

    def resume(self, filters, token):
        with Session(self.engine) as db:
            row = db.get(SourceCache, "cursor-" + token)
            if not row or row.expires_at <= time.time() or row.payload.get("filters") != snapshot_key(filters):
                raise RiaError("search_expired")
            return copy.deepcopy(row.payload)

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
                  "order_by": 7, "countpage": PAGE_SIZE, "currency": 1}
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

    def car(self, source_id, *, force=False):
        car = self.request("info", {"auto_id": source_id}, lambda raw: parse_car(raw, source_id), force=force)
        return peer_cache.observe(self.engine, copy.deepcopy(car))

    def resolve_modification(self, car):
        """Recover only a unique full catalog label within the same generation/body."""
        if car.get("modification_id") or not car["comparable_condition"]:
            return
        if not all(valid_id(car.get(key)) for key in COMPARABLE_DIMENSIONS if key != "modification_id"):
            return
        name = normalize_modification(car.get("modification_name"))
        if not name:
            return
        path = (f"modifications/by/generation/{car['generation_id']}/body/"
                f"{car['body_id']}/modifications")
        catalog = self.request(path, {}, parse_modification_catalog, ttl=7 * 86400)["items"]
        ids = {item["value"] for item in catalog if normalize_modification(item["name"]) == name}
        if len(ids) == 1:
            car["modification_id"] = ids.pop()
            car["modification_source"] = "catalog_name"
            car["modification_resolution"] = "resolved"
        else:
            car["modification_resolution"] = "ambiguous" if ids else "not_found"
        peer_cache.observe(self.engine, car)

    def comparisons(self, candidate):
        self.resolve_modification(candidate)
        required = comparison_dimensions(candidate)
        if reasons(candidate, time.time()):
            return PeerBatch()
        start_requests = self.requests_made
        peers = PeerBatch(peer_cache.candidates(self.engine, candidate, self.peer_scan_limit))
        peers.diagnostics = {"cached_peers": len(peers), "details_inspected": 0, "requests_used": 0, "limited": False}
        if estimate(candidate, peers)["valuation"] in {"sample_median", "mixed_sample"}:
            return peers
        # Independent of the user's budget and region, preventing a price-capped median.
        tolerance = max(30000, candidate["mileage"] * .2)
        params = {"category_id": 1, "searchType": 4, "status_id": 0, "page": 0, "countpage": self.peer_scan_limit + 1,
                  "order_by": 7, "marka_id[0]": candidate["brand_id"], "model_id[0]": candidate["model_id"],
                  "generation_id[0][0]": candidate["generation_id"], "bodystyle[0]": candidate["body_id"],
                  "type[0]": candidate["fuel_id"], "gearbox[0]": candidate["gear_id"],
                  "s_yers[0]": candidate["year"] - 1, "po_yers[0]": candidate["year"] + 1,
                  "raceFrom": math.floor(max(0, candidate["mileage"] - tolerance) / 1000),
                  "raceTo": math.ceil((candidate["mileage"] + tolerance) / 1000),
                  "damage": 1, "abroad": 2, "custom": 1}
        if "modification_id" in required:
            params["modifications[0][0][0]"] = candidate["modification_id"]
        else:
            params.update(engineVolumeFrom=candidate["engine_cc"] / 1000,
                          engineVolumeTo=candidate["engine_cc"] / 1000)
        result = self.request("search", params, parse_ids)
        known_ids = {peer["id"] for peer in peers} | {candidate["id"]}
        ids = [sid for sid in result["ids"] if sid not in known_ids]
        inspected = 0
        for source_id in ids[:self.peer_scan_limit]:
            inspected += 1
            try:
                peer = self.car(source_id)
                # Spend catalog calls only on otherwise comparable peers, after
                # loading the candidate cards and checking every known attribute.
                if (all(peer.get(key) == candidate[key] for key in required if key != "modification_id")
                        and abs(peer["year"] - candidate["year"]) <= 1
                        and abs(peer["mileage"] - candidate["mileage"]) <= tolerance):
                    self.resolve_modification(peer)
                # Pool only actual comparison observations, not every monitored
                # low-price candidate. The sample must not follow a user's budget.
                peers.append(peer_cache.observe(self.engine, peer, create=True))
                # Recheck details even if the provider ignores a query parameter.
                # Once five suitable peers establish a result, stop spending calls.
                if estimate(candidate, peers)["valuation"] in {"sample_median", "mixed_sample"}:
                    break
            except RiaError as exc:
                if str(exc) not in {"listing_unavailable", "invalid_response"}:
                    raise
        incomplete = estimate(candidate, peers)["valuation"] == "insufficient_data"
        peers.diagnostics.update(details_inspected=inspected, source_total=result["total"],
            requests_used=self.requests_made - start_requests,
            limited=incomplete and (len(ids) > inspected or result["total"] > len(result["ids"])))
        return peers

    def search(self, filters, cursor=None):
        if not self.key:
            raise RiaError("not_configured")
        context = self.resume(filters, cursor) if cursor else None
        key = snapshot_key(filters, cursor)
        with Session(self.engine) as db:
            row = db.get(SourceCache, key)
            snapshot = copy.deepcopy(row.payload) if row and row.expires_at > time.time() else None
        quota = quota_status(self.engine, self.limits)
        if snapshot:
            if time.time() - snapshot["checked_at"] < FRESH_SECONDS:
                return snapshot_view(snapshot, filters, quota, cached=True)
            if quota["reason"] != "available":
                return snapshot_view(snapshot, filters, quota, cached=True, reason="quota_exceeded")
        try:
            payload = self.search_uncached(filters.model_copy(update={"onlyDeals": False}), context)
        except RiaError as exc:
            if snapshot and str(exc) in TRANSIENT_ERRORS:
                return snapshot_view(snapshot, filters, quota_status(self.engine, self.limits), cached=True, reason=str(exc))
            raise
        # Do not replace a useful snapshot with an interrupted empty response.
        if payload["cars"] or not payload["warnings"]:
            with Session(self.engine) as db:
                row = db.scalar(select(SourceCache).where(SourceCache.id == key).with_for_update())
                if row is None:
                    try:
                        with db.begin_nested():
                            row = SourceCache(id=key, payload=payload,
                                              expires_at=payload["checked_at"] + SNAPSHOT_SECONDS)
                            db.add(row)
                            db.flush()
                    except IntegrityError:
                        row = db.scalar(select(SourceCache).where(SourceCache.id == key).with_for_update())
                if row.payload["checked_at"] <= payload["checked_at"]:
                    row.payload = payload
                    row.expires_at = payload["checked_at"] + SNAPSHOT_SECONDS
                db.commit()
        return snapshot_view(payload, filters, payload["quota"])

    def search_uncached(self, filters, context=None):
        self.acquire()
        self.request_limit = PAGE_REQUEST_LIMIT
        try:
            params, ids = self.parameters(filters)
            context = context or {"page": 0, "offset": 0}
            self.observed_at = min(self.observed_at, context.get("checked_at", self.observed_at))
            page, offset = context["page"], context["offset"]
            params["page"] = page
            results = context.get("results") or self.request("search", params, parse_ids)
            source_ids = results["ids"][:PAGE_SIZE]
            candidates = copy.deepcopy(context.get("pending", []))
            cars, warnings, pending = [], [], []
            inspected = 0
            # Finish interrupted valuations before advancing to new candidate IDs.
            if not candidates:
                for source_id in source_ids[offset:]:
                    try:
                        car = self.car(source_id)
                        inspected += 1
                        offset += 1
                        if matches(car, filters, ids):
                            candidates.append(car)
                    except RiaError as exc:
                        warnings.append(str(exc))
                        if str(exc) not in {"listing_unavailable", "invalid_response"}:
                            break
                        offset += 1
            # Fetch candidate cards first so peer valuation cannot starve results.
            for position, car in enumerate(candidates):
                rating = estimate(car, [])
                try:
                    rating = estimate(car, self.comparisons(car))
                except RiaError as exc:
                    warnings.append(str(exc))
                    if str(exc) in TRANSIENT_ERRORS:
                        pending = candidates[position:]
                        for waiting in pending:
                            waiting.update(market=None, discount=None, comparables=0, valuation="pending",
                                           valuation_reasons=["pending"])
                        cars.extend(pending)
                        break
                car.update(rating)
                cars.append(car)
            next_state = None
            if pending or offset < len(source_ids):
                next_state = {"page": page, "offset": offset, "results": results, "pending": pending}
            elif source_ids and (page + 1) * PAGE_SIZE < results["total"]:
                next_state = {"page": page + 1, "offset": 0}
            if not inspected and not cars and warnings:
                raise RiaError(warnings[0])
            token = self.continuation(filters, next_state)
            if filters.onlyDeals:
                cars = [car for car in cars if is_deal(car["price_usd"], car["market"])]
            return {"cars": cars, "source_total": results["total"], "inspected": inspected,
                    "quota": quota_status(self.engine, self.limits),
                    "next_cursor": token, "page_size": PAGE_SIZE, "requests_used": self.requests_made,
                    "pending_valuations": len(pending),
                    "limited": bool(token or warnings), "warnings": sorted(set(warnings)), "checked_at": self.observed_at,
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
