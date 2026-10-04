"""Once-only, owner-scoped paid source comparison; never valuation admission.

The configured incident must already have retained detail evidence matching a
current permitted search. Every request rechecks a real current purchase and
that same search activation. Results remain API observations, never proof of
the AUTO.RIA application's native price range.
"""
import copy
import hashlib
import json
import logging
import math
import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import api_attempt_audit, paid_source_access, ria_ai_price as ai
from .auto_ria import RiaError, fetch_json
from .models import (Filters, MonitorJob, MonitorMembership, MonitorWatch, Search,
                     SourceCache, SourceProbe, User)
from .monitor import member_query, source_filters
from .ria_search import RiaSearch, matches, parse_car
from .valuation import notification_condition_allowed, number

VERSION = "auto-ria-source-comparison-v1"
DETAIL_PATH = "source-comparison-info"
MAX_REQUESTS = 4
DIMENSIONS = ("category_id", "brand_id", "model_id", "region_id", "generation_id", "modification_id",
              "body_id", "fuel_id", "gear_id", "year", "engine_cc", "mileage")
OBSERVATION_FIELDS = ("source_id", "basis", "currency", "lower_usd", "upper_usd",
                      "average_usd", "range_fraction", "quantity", "period_parameter", "observed_at")
ERRORS = frozenset({"busy", "not_configured", "search_limit", "quota_exceeded", "key_rejected",
    "access_denied", "upstream_error", "connection_error", "invalid_response", "listing_unavailable",
    "info_endpoint_unavailable", "ai_not_configured", "ai_access_denied", "ai_upstream_error",
    "ai_connection_error", "ai_invalid_response", "catalog_not_cached", "unsupported_filter",
    "no_eligible_subscription", "paid_access_required", "comparison_details_incomplete",
    "comparison_candidate_not_permitted"})
log = logging.getLogger("autodeal.source_comparison")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    log.addHandler(logging.StreamHandler())


def explicit_parameters(candidate):
    """Exact official criteria; km become thousands of km, cc become litres."""
    fields = {"category_id": "categoryId", "brand_id": "brandId", "model_id": "modelId",
              "body_id": "bodyId", "fuel_id": "fuelId", "gear_id": "gearBoxId"}
    if not isinstance(candidate, dict):
        raise RiaError("comparison_details_incomplete")
    params = {}
    for key, target in fields.items():
        value = candidate.get(key)
        if type(value) is not int or value <= 0 or not ai.valid_id(str(value)):
            raise RiaError("comparison_details_incomplete")
        params[target] = str(value)
    year = candidate.get("year")
    if type(year) is not int or not 1900 <= year <= 2100:
        raise RiaError("comparison_details_incomplete")
    params["year"] = {"gte": str(year), "lte": str(year)}
    for key, target in (("mileage", "mileage"), ("engine_cc", "engineVolume")):
        value = candidate.get(key)
        if not number(value, positive=True):
            raise RiaError("comparison_details_incomplete")
        value = format((Decimal(str(value)) / 1000).normalize(), "f")
        params[target] = {"gte": value, "lte": value}
    for key, target in (("generation_id", "generationId"), ("modification_id", "modificationId")):
        value = candidate.get(key)
        if value is not None:
            if type(value) is not int or value <= 0 or not ai.valid_id(str(value)):
                raise RiaError("comparison_details_incomplete")
            params[target] = str(value)
    return params


def _member(db, owner, candidate, frozen=None):
    """Local validation only: never resolve dictionaries through paid requests."""
    query = member_query(time.time(), paid_only=True,
        excluded=paid_source_access.exclusions(db.get_bind())).where(Search.user_id == owner)
    for search, watch, member in db.execute(query.order_by(Search.id)):
        filters = Filters.model_validate(search.filters)
        fingerprint = filters.fingerprint()
        source_fingerprint = source_filters(search.filters).fingerprint()
        identity = (search.id, watch.epoch, fingerprint, source_fingerprint)
        if (search.fingerprint != fingerprint or member.feed_id != source_fingerprint
                or (frozen is not None and identity != frozen)):
            continue
        _, ids = RiaSearch.cached_parameters(db, source_filters(search.filters))
        if (candidate.get("category_id") == 1
                and notification_condition_allowed(candidate) and matches(candidate, filters, ids)):
            return identity
    raise RiaError("no_eligible_subscription")


def _capture_candidate(result, candidate):
    """Keep observed numeric detail inputs even if the next guard rejects them."""
    def numeric(value):
        return value if type(value) in (int, float) and math.isfinite(value) else None
    result["vehicle_dimensions"] = {key: numeric(candidate.get(key)) for key in DIMENSIONS}
    result["listing_price_usd"] = numeric(candidate.get("price_usd"))
    result["detail_observed_at"] = numeric(candidate.get("observed_at"))


def _filter_failures(candidate, filters, ids):
    """Mirror the existing matches rule, exposing only fixed field names."""
    failures = []
    for key, expected in ids.items():
        if key not in {"brand_id", "model_id", "region_id", "body_id", "fuel_id", "gear_id"}:
            continue
        actual = candidate.get(key)
        if isinstance(expected, list):
            mismatch = expected and actual not in expected and (actual is not None
                or key not in {"body_id", "fuel_id", "gear_id"})
        else:
            mismatch = actual != expected
        if mismatch:
            failures.append({"field": key, "boundary": "permitted_values"})
    for key, span in (("price_usd", filters.price), ("year", filters.year),
                      ("mileage", filters.mileage)):
        value = candidate.get(key)
        if key == "mileage" and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            failures.append({"field": key, "boundary": "valid_numeric_value"})
            continue
        if key == "mileage":
            value /= 1000
        if span.from_ is not None and value < span.from_:
            failures.append({"field": key, "boundary": "minimum"})
        if span.to is not None and value > span.to:
            failures.append({"field": key, "boundary": "maximum"})
    return failures


def _explanation(db, owner, candidate, frozen=None):
    """Read-only current guard evidence; never an alternate eligibility path."""
    now = time.time()
    user = db.get(User, owner)
    explanation = {"scope": "current_state", "checked_at": now,
        "ready": bool(user and user.ready),
        "current_confirmed_purchase": bool(db.scalar(select(paid_source_access.confirmed_clause(
            owner, now, excluded=paid_source_access.exclusions(db.get_bind()))))),
        "enabled_search_present": False, "consistent_membership_present": False,
        "filters_match": False, "category_allowed": candidate.get("category_id") == 1,
        "condition_allowed": notification_condition_allowed(candidate), "filter_failures": []}
    failures, identity_failures = set(), set()
    query = select(Search).where(Search.user_id == owner, Search.enabled.is_(True))
    if frozen is not None:
        query = query.where(Search.id == frozen[0])
    for search in db.scalars(query):
        explanation["enabled_search_present"] = True
        watch, member = db.get(MonitorWatch, search.id), db.get(MonitorMembership, search.id)
        if not watch or not member or watch.epoch != member.epoch:
            identity_failures.add("membership_epoch")
            continue
        try:
            filters = Filters.model_validate(search.filters)
            fingerprint, source_fingerprint = filters.fingerprint(), source_filters(search.filters).fingerprint()
        except (TypeError, ValueError):
            identity_failures.add("invalid_filter_definition")
            continue
        identity = (search.id, watch.epoch, fingerprint, source_fingerprint)
        if (search.fingerprint != fingerprint or member.feed_id != source_fingerprint
                or (frozen is not None and identity != frozen)):
            identity_failures.add("filter_or_activation_identity")
            continue
        explanation["consistent_membership_present"] = True
        try:
            _, ids = RiaSearch.cached_parameters(db, source_filters(search.filters))
        except RiaError:
            identity_failures.add("cached_filter_ids_unavailable")
            continue
        rejected = _filter_failures(candidate, filters, ids)
        if not rejected:
            explanation["filters_match"] = True
        failures.update((item["field"], item["boundary"]) for item in rejected)
    explanation["filter_failures"] = [{"field": field, "boundary": boundary}
        for field, boundary in sorted(failures)]
    explanation["identity_failures"] = sorted(identity_failures)
    explanation["failed_boundaries"] = [key for key in ("ready", "current_confirmed_purchase",
        "enabled_search_present", "consistent_membership_present", "filters_match", "category_allowed",
        "condition_allowed") if not explanation[key]]
    return explanation


def _guard_member(db, owner, candidate, result, frozen=None):
    try:
        return _member(db, owner, candidate, frozen)
    except RiaError:
        result["eligibility_explanation"] = _explanation(db, owner, candidate, frozen)
        raise


def _retained_report(db, settings, sid, row):
    """Enrich the old missing-input incident from its own cache without writes."""
    result = copy.deepcopy(row.result)
    flags = result.get("detail_availability", {})
    if (row.status == "no_eligible_subscription" and row.requests == 1
            and "vehicle_dimensions" not in result and flags.get("requested_id_matches") is True
            and flags.get("isSold") is False and flags.get("active") is True and flags.get("statusId") == 0):
        key = [DETAIL_PATH, {"auto_id": sid, "comparison_policy": VERSION}]
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        cached = db.get(SourceCache, digest)
        candidate = cached.payload if cached and isinstance(cached.payload, dict) else None
        if (candidate and candidate.get("id") == sid and number(candidate.get("price_usd"), positive=True)
                and type(candidate.get("year")) is int and 1900 <= candidate["year"] <= 2100):
            _capture_candidate(result, candidate)
            result["detail_recovery"] = {"status": "retained_diagnostic_cache", "provider_requests": 0,
                "cache_expired": cached.expires_at <= time.time(), "valuation_admission": False}
            result["eligibility_explanation"] = _explanation(db, settings.admin_telegram_id, candidate)
        else:
            result["detail_recovery"] = {"status": "not_found", "provider_requests": 0,
                "valuation_admission": False}
    return _report(sid, row.status, row.requests, result)


def _flags(raw, sid):
    auto = raw.get("autoData") if isinstance(raw, dict) else None
    if not isinstance(auto, dict):
        return {"auto_data_present": False}
    result = {"auto_data_present": True, "requested_id_matches": str(auto.get("autoId")) == sid}
    # These are the precise three availability inputs listing_preview checks.
    for key in ("isSold", "active", "statusId"):
        value = auto.get(key)
        result[key] = value if value is None or type(value) in (bool, int) else "invalid_type"
    return result


def _observation(value, sid, period):
    if (not isinstance(value, dict) or value.get("basis") != ai.API_BASIS
            or value.get("source_id") != sid or value.get("currency") != "USD"
            or value.get("period_parameter") != period
            or any(not number(value.get(key), positive=True)
                   for key in ("lower_usd", "upper_usd", "average_usd", "range_fraction", "observed_at"))
            or value["lower_usd"] >= value["upper_usd"] or value["range_fraction"] >= 1
            or type(value.get("quantity")) is not int or value["quantity"] <= 0):
        raise RiaError("ai_invalid_response")
    return {key: value[key] for key in OBSERVATION_FIELDS}


def _save(engine, probe_id, status, requests, result):
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        row.status, row.requests, row.checked_at = status, requests, time.time()
        row.result = copy.deepcopy(result)
        db.commit()


def _report(sid, status, requests, result):
    report = {"source_id": sid, "status": status, "requests": requests, "result": result}
    log.info("AUTO.RIA source comparison %s", json.dumps(report, sort_keys=True, allow_nan=False))
    return report


def check_once(engine, settings, *, source_factory=RiaSearch):
    sid = getattr(settings, "ria_source_comparison_listing_id", "")
    if not sid:
        return None  # The default-off path touches neither DB nor provider.
    if (not ai.valid_id(sid) or not settings.live or not settings.monitor_enabled
            or not settings.ria_ai_price_enabled or not settings.auto_ria_api_key
            or not ai.valid_id(settings.auto_ria_user_id)
            or type(settings.admin_telegram_id) is not int or settings.admin_telegram_id <= 0):
        return None
    probe_id = VERSION + "-" + sid
    result = {"native_parity_verified": False, "production_valuation_changed": False,
              "max_source_requests": MAX_REQUESTS, "observations": {}}
    with Session(engine) as db:
        existing = db.get(SourceProbe, probe_id)
        if existing:
            return _retained_report(db, settings, sid, existing)
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result=result))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(SourceProbe, probe_id)
            return _retained_report(db, settings, sid, row)
    requests, status = 0, "operation_error"
    try:
        with Session(engine) as db:
            job = db.get(MonitorJob, sid)
            candidate = copy.deepcopy(job.result.get("candidate")) if job and isinstance(job.result, dict) else None
            if not isinstance(candidate, dict) or candidate.get("id") != sid:
                raise RiaError("comparison_details_incomplete")
            frozen = _guard_member(db, settings.admin_telegram_id, candidate, result)

        def perform(name, request):
            nonlocal requests
            if requests >= MAX_REQUESTS:
                raise RiaError("search_limit")
            result["step"] = {"name": name, "state": "started"}
            _save(engine, probe_id, "checking", requests, result)
            source = source_factory(engine, settings.auto_ria_api_key)
            source.request_limit = 1
            def policy(db, now, limits):
                _guard_member(db, settings.admin_telegram_id, candidate, result, frozen)
                return api_attempt_audit.authorization("owner_source_discrepancy", "auto_ria:" + sid)
            source.request_policy = policy
            acquired = False
            try:
                source.acquire()
                acquired = True
                return request(source)
            finally:
                requests += source.requests_made
                if acquired:
                    source.release()
                result["step"] = {"name": name, "state": "finished"}
                _save(engine, probe_id, "checking", requests, result)

        def detail(source):
            def parser(raw):
                result["detail_availability"] = _flags(raw, sid)
                return parse_car(raw, sid)
            def transport(record):
                if source.fetch is fetch_json:
                    return source.fetch(source.key, "info", {"auto_id": sid}, attempt_telemetry=record)
                return source.fetch(source.key, "info", {"auto_id": sid})
            return source.request(DETAIL_PATH, {"auto_id": sid, "comparison_policy": VERSION},
                parser, ttl=60, force=True, audited_fetcher=transport)

        candidate = perform("fresh_details", detail)
        _capture_candidate(result, candidate)
        with Session(engine) as db:
            _guard_member(db, settings.admin_telegram_id, candidate, result, frozen)
        params = explicit_parameters(candidate)
        variants = (("omni_168", 168, {"omniId": sid}),
                    ("omni_90", 90, {"omniId": sid}), ("explicit_168", 168, params))
        for name, period, variant in variants:
            def quote(source, *, period=period, variant=variant):
                return source.request(ai.METHOD,
                    {"source_id": sid, "period": period, "params": variant, "comparison_policy": VERSION},
                    lambda value: _observation(value, sid, period), ttl=60, force=True,
                    audited_fetcher=lambda record: ai.fetch_observation(source.key,
                        settings.auto_ria_user_id, sid, period_parameter=period,
                        params=variant, attempt_telemetry=record))
            observation = perform(name, quote)
            result["observations"][name] = observation
            _save(engine, probe_id, "checking", requests, result)
        status = "observed_unverified_native"
    except RiaError as exc:
        status = str(exc) if str(exc) in ERRORS else "operation_error"
    except Exception:
        # No raw exception text, credentials, seller details or partial response.
        status = "operation_error"
    _save(engine, probe_id, status, requests, result)
    return _report(sid, status, requests, result)
