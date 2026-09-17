"""Explicit operator-requested, once-only live check. Never enables delivery."""
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json
from .models import Filters, SourceProbe
from .ria_search import RiaSearch

PREFIX = "auto-ria-validation-"
MAX_REQUESTS = 32
DIMENSIONS = ("brand_id", "model_id", "generation_id", "modification_id", "body_id", "fuel_id", "gear_id")


def validate_run_id(run_id):
    if run_id and not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", run_id):
        raise ValueError("Invalid AUTO.RIA validation run identifier")


def car_summary(car):
    fields = ("id", "title", "url", "year", "price_usd", "mileage", "market",
              "comparables", "valuation", "comparable_condition", *DIMENSIONS)
    return {field: car.get(field) for field in fields}


def validation_status(engine, run_id):
    if not run_id:
        return None
    with Session(engine) as db:
        row = db.get(SourceProbe, PREFIX + run_id)
        if row is None:
            return {"status": "pending"}
        return {"status": row.status, "checked_at": row.checked_at,
                "requests_used": row.requests, "request_cap": MAX_REQUESTS, **row.result}


def validate_once(engine, key, run_id, fetch=None):
    validate_run_id(run_id)
    if not run_id or not key:
        return
    probe_id = PREFIX + run_id
    with Session(engine) as db:
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return

    provider_quota, raw_checks, comparisons = {}, [], []
    def telemetry(data):
        provider_quota.update(data)

    def bounded_fetch(api_key, method, params):
        with Session(engine) as db:
            row = db.scalar(select(SourceProbe).where(SourceProbe.id == probe_id).with_for_update())
            if row.requests >= MAX_REQUESTS:
                raise RiaError("validation_limit")
            row.requests += 1
            db.commit()  # A crash or upstream error still counts as an attempt.
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
                    "missing_auto_fields": [name for name in ("generationId", "modificationId", "bodyId", "fuelId", "gearBoxId")
                                            if type(auto.get(name)) is not int or auto[name] <= 0]})
        return raw

    class AuditSearch(RiaSearch):
        def comparisons(self, candidate):
            peers = super().comparisons(candidate)
            comparisons.append({"candidate_id": candidate["id"], "peers": [car_summary(p) for p in peers]})
            return peers

    search = AuditSearch(engine, key, bounded_fetch)
    filters = Filters(brand="Volkswagen", model="Golf", onlyDeals=False)
    result, status = {}, "check_failed"
    try:
        # The production request/filter/valuation path, without snapshot fallback.
        data = search.search_uncached(filters)
        valued = sum(car["market"] is not None for car in data["cars"])
        status = "valuation_verified" if valued else "insufficient_comparables" if data["cars"] else "no_verified_details"
        result = {"filters": filters.canonical(), "inspected": data["inspected"],
                  "returned": len(data["cars"]), "valued": valued, "warnings": data["warnings"],
                  "cars": [car_summary(car) for car in data["cars"]]}
    except RiaError as exc:
        status = str(exc)
    except Exception as exc:
        result = {"error_type": type(exc).__name__, "stage": search.stage}
    result.update(provider_quota=provider_quota, detail_checks=raw_checks, comparisons=comparisons)
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        row.status, row.result, row.checked_at = status, result, time.time()
        db.commit()
