"""Explicit operator-requested, once-only live check. Never enables delivery."""
import re
import math
import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json
from .models import Filters, SourceProbe
from .ria_search import RiaSearch, estimate
from .valuation import comparison_dimensions

PREFIX = "auto-ria-validation-"
MAX_REQUESTS = 32
DIMENSIONS = ("brand_id", "model_id", "generation_id", "modification_id", "body_id", "fuel_id", "gear_id")
PROFILES = {
    "golf": (("Volkswagen", "Golf"),),
    "popular-v1": (("Volkswagen", "Passat"), ("Audi", "A6"), ("Mercedes-Benz", "E-Class")),
    "eligible-v1": (("Volkswagen", "Passat"), ("Audi", "A6"), ("Mercedes-Benz", "E-Class")),
}


def validate_run_id(run_id):
    if run_id and not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", run_id):
        raise ValueError("Invalid AUTO.RIA validation run identifier")


def validate_profile(profile):
    if profile not in PROFILES:
        raise ValueError("Invalid AUTO.RIA validation profile")


def car_summary(car):
    fields = ("id", "title", "url", "year", "price_usd", "mileage", "market",
              "comparables", "valuation", "valuation_reasons", "comparable_condition", "observed_at",
              "modification_name", "modification_source", "modification_resolution", "vehicle_key", "engine_cc", *DIMENSIONS)
    return {field: car.get(field) for field in fields}


def comparison_report(candidate, peers, *, now=None):
    """Independently explain/recalculate the production estimate from sanitized details."""
    now = time.time() if now is None else now
    def detail_errors(car):
        errors = []
        if type(car.get("price_usd")) not in (int, float) or not math.isfinite(car["price_usd"]) or car["price_usd"] <= 0:
            errors.append("invalid_price")
        if type(car.get("year")) is not int or not 1900 <= car["year"] <= 2100:
            errors.append("invalid_year")
        if type(car.get("mileage")) not in (int, float) or not math.isfinite(car["mileage"]) or car["mileage"] < 0:
            errors.append("invalid_mileage")
        stamp = car.get("observed_at")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0 or not -30 <= now - stamp <= 900:
            errors.append("stale_details")
        return errors
    dimensions = comparison_dimensions(candidate)
    missing = [key for key in dimensions if type(candidate.get(key)) is not int or candidate[key] <= 0]
    candidate_reasons = (["unverified_condition"] if candidate["comparable_condition"] is not True else [])
    candidate_reasons += ["missing_" + key for key in missing] + detail_errors(candidate)
    unique = {}
    for peer in peers:
        if peer["id"] != candidate["id"] and (peer["id"] not in unique or
                peer.get("observed_at", 0) >= unique[peer["id"]].get("observed_at", 0)):
            unique[peer["id"]] = peer
    vehicles = {candidate["vehicle_key"]} if candidate.get("vehicle_key") else set()
    accepted, rejected = [], []
    for peer in unique.values():
        reasons = (["candidate_ineligible"] if candidate_reasons else [])
        if peer["comparable_condition"] is not True:
            reasons.append("unverified_condition")
        reasons += ["missing_" + key for key in dimensions if type(peer.get(key)) is not int or peer[key] <= 0]
        reasons += detail_errors(peer)
        reasons += [key for key in dimensions if peer.get(key) != candidate.get(key)]
        if abs(peer["year"] - candidate["year"]) > 1:
            reasons.append("year")
        if abs(peer["mileage"] - candidate["mileage"]) > max(30000, candidate["mileage"] * .2):
            reasons.append("mileage")
        if peer.get("vehicle_key") and peer["vehicle_key"] in vehicles:
            reasons.append("duplicate_vehicle")
        if reasons:
            rejected.append({"id": peer["id"], "reasons": reasons})
        else:
            accepted.append(peer)
            if peer.get("vehicle_key"):
                vehicles.add(peer["vehicle_key"])
    prices = sorted(peer["price_usd"] for peer in accepted)
    mixed = len(prices) >= 5 and max(prices) / min(prices) > 2
    market = None
    if len(prices) >= 5 and not mixed:
        # Independent linear p25 calculation, not the production helper.
        rank, remainder = divmod(len(prices) - 1, 4)
        low, high = Decimal(str(prices[rank])), Decimal(str(prices[min(rank + 1, len(prices) - 1)]))
        market = float(low + (high - low) * Decimal(remainder) / 4)
    threshold = Decimal(str(market)) * Decimal("0.85") if market is not None else None
    actual = estimate(candidate, peers, now=now)
    return {"candidate_id": candidate["id"], "candidate_reasons": candidate_reasons,
            "peers": [car_summary(peer) for peer in peers],
            "accepted_ids": [peer["id"] for peer in accepted], "accepted_prices_usd": prices,
            "rejected": rejected, "duplicate_or_self_entries": len(peers) - len(unique),
            "mixed_sample": mixed, "recalculated_market": market,
            "deal_threshold_usd": float(threshold) if threshold is not None else None,
            "qualifies_as_deal": threshold is not None and Decimal(str(candidate["price_usd"])) <= threshold,
            "calculation_matches": actual["market"] == market and actual["comparables"] == len(accepted)}


def validation_status(engine, run_id, profile="golf"):
    validate_profile(profile)
    if not run_id:
        return None
    with Session(engine) as db:
        row = db.get(SourceProbe, PREFIX + run_id)
        if row is None:
            return {"status": "pending", "profile": profile, "request_cap": MAX_REQUESTS * len(PROFILES[profile])}
        if row.result.get("profile", "golf") != profile:
            return {"status": "profile_conflict", "profile": profile}
        return {"status": row.status, "checked_at": row.checked_at,
                "requests_used": row.requests, "request_cap": MAX_REQUESTS, **row.result}


def validate_once(engine, key, run_id, fetch=None, *, profile="golf", stop=None):
    validate_run_id(run_id)
    validate_profile(profile)
    if not run_id or not key or (stop is not None and stop.is_set()):
        return
    probe_id = PREFIX + run_id
    request_cap = MAX_REQUESTS * len(PROFILES[profile])
    metadata = {"profile": profile, "request_cap": request_cap,
                "candidate_scope": "undamaged" if profile == "eligible-v1" else "all_conditions",
                "per_query_request_cap": MAX_REQUESTS,
                "models_planned": [brand + " " + model for brand, model in PROFILES[profile]]}
    with Session(engine) as db:
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result=metadata))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return

    provider_quota, raw_checks, comparisons, queries, catalog_checks = {}, [], [], [], []
    query_calls = 0
    def telemetry(data):
        provider_quota.update(data)

    def bounded_fetch(api_key, method, params):
        nonlocal query_calls
        if stop is not None and stop.is_set():
            raise RiaError("validation_stopped")
        with Session(engine) as db:
            row = db.scalar(select(SourceProbe).where(SourceProbe.id == probe_id).with_for_update())
            if row.requests >= request_cap or query_calls >= MAX_REQUESTS:
                raise RiaError("validation_limit")
            row.requests += 1
            db.commit()  # A crash or upstream error still counts as an attempt.
        query_calls += 1
        raw = fetch(api_key, method, params) if fetch else fetch_json(api_key, method, params, telemetry=telemetry)
        if method == "info" and isinstance(raw, dict):
            # Diagnostic booleans/IDs only, never raw seller data, VIN or description.
            auto = raw.get("autoData") or {}
            condition = raw.get("technicalCondition") or {}
            flags = raw.get("autoInfoBar") or {}
            if isinstance(auto, dict) and isinstance(condition, dict) and isinstance(flags, dict):
                raw_checks.append({"id": str(params["auto_id"]),
                    "technical_condition_id": condition.get("id") if type(condition.get("id")) is int else None,
                    "flags": {name: flags.get(name) if type(flags.get(name)) is bool else None
                              for name in ("damage", "onRepairParts", "abroad", "custom")},
                    "has_modification_name": bool(isinstance(auto.get("modificationName"), str)
                                                  and auto["modificationName"].strip()),
                    "missing_auto_fields": [name for name in ("generationId", "modificationId", "bodyId", "fuelId", "gearBoxId")
                                            if type(auto.get(name)) is not int or auto[name] <= 0]})
        return raw

    class AuditSearch(RiaSearch):
        def parameters(self, filters):
            params, ids = super().parameters(filters)
            if profile == "eligible-v1":
                # Target the estimator's supported condition; this is not a
                # representative sample of every listing for these models.
                params.update({"technicalCondition[0]": 1, "damage": 1, "abroad": 2, "custom": 1})
            return params, ids

        def request(self, *args, **kwargs):
            if stop is not None and stop.is_set():
                raise RiaError("validation_stopped")
            return super().request(*args, **kwargs)

        def comparisons(self, candidate):
            if (profile == "eligible-v1" and not catalog_checks and candidate["comparable_condition"]
                    and candidate.get("modification_id") and candidate.get("modification_name")
                    and all(candidate.get(key) for key in DIMENSIONS)):
                # Check the new lookup against a real, independently supplied ID.
                # Only this copy loses its ID; production candidates stay intact.
                probe = {**candidate, "modification_id": None, "modification_source": None}
                self.resolve_modification(probe)
                catalog_checks.append({"candidate_id": candidate["id"],
                    "listing_modification_id": candidate["modification_id"],
                    "catalog_modification_id": probe.get("modification_id"),
                    "status": ("matched" if probe.get("modification_id") == candidate["modification_id"]
                               else "conflict" if probe.get("modification_id") else probe.get("modification_resolution", "unavailable"))})
            peers = super().comparisons(candidate)
            comparisons.append(comparison_report(candidate, peers))
            return peers

    def payload():
        if profile == "golf":
            return {**metadata, **(queries[0] if queries else {}), "provider_quota": dict(provider_quota)}
        return {**metadata, "queries": list(queries), "provider_quota": dict(provider_quota),
                "valued": sum(query.get("valued", 0) for query in queries),
                "returned": sum(query.get("returned", 0) for query in queries)}

    stop_reasons = {"quota_exceeded", "key_rejected", "access_denied", "connection_error", "validation_stopped", "check_failed", "catalog_conflict"}
    for brand, model in PROFILES[profile]:
        if stop is not None and stop.is_set():
            break
        query_calls = 0
        raw_checks, comparisons, catalog_checks = [], [], []
        search = AuditSearch(engine, key, bounded_fetch)
        filters = Filters(brand=brand, model=model, onlyDeals=False)
        result, status = {"filters": filters.canonical()}, "check_failed"
        try:
            # The production path, including post-filtering and strict valuation;
            # no UI snapshot fallback. Fresh source-detail caches remain reusable.
            data = search.search_uncached(filters)
            valued = sum(car["market"] is not None for car in data["cars"])
            status = "valuation_verified" if valued else "insufficient_comparables" if data["cars"] else "no_verified_details"
            if any(not report["calculation_matches"] for report in comparisons):
                status = "calculation_mismatch"
            if any(check["status"] == "conflict" for check in catalog_checks):
                status = "catalog_conflict"
            result.update(inspected=data["inspected"], returned=len(data["cars"]), valued=valued,
                          warnings=data["warnings"], observation_at=data["checked_at"],
                          pending_valuations=data["pending_valuations"],
                          cars=[car_summary(car) for car in data["cars"]])
        except RiaError as exc:
            status = str(exc)
        except Exception as exc:
            status = "check_failed"
            result.update(error_type=type(exc).__name__, stage=search.stage)
        result.update(status=status, requests_used=query_calls, detail_checks=raw_checks,
                      comparisons=comparisons, catalog_resolution_checks=catalog_checks)
        queries.append(result)
        with Session(engine) as db:
            row = db.get(SourceProbe, probe_id)
            row.result, row.checked_at = payload(), time.time()
            db.commit()
        if stop_reasons.intersection([status, *result.get("warnings", [])]):
            break

    if profile == "golf":
        state = queries[0]["status"] if queries else "validation_stopped"
    elif any(query["status"] == "calculation_mismatch" for query in queries):
        state = "calculation_mismatch"
    elif any(query["status"] == "catalog_conflict" for query in queries):
        state = "catalog_conflict"
    else:
        complete = len(queries) == len(PROFILES[profile]) and all(
            query["status"] == "valuation_verified" and not query.get("warnings") for query in queries)
        state = "samples_checked" if complete else "partial"
    final = payload()
    # The outer record owns aggregate status/counts; per-model fields stay in queries.
    final.pop("status", None)
    final.pop("requests_used", None)
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        row.status, row.result, row.checked_at = state, final, time.time()
        db.commit()
