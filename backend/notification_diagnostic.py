"""Operator-only, once-per-listing inspection; never creates or sends alerts."""
import json
import logging
import re
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json
from .models import Filters, MonitorJob, MonitorSeen, SourceProbe
from .monitor import active_members, stamp
from .ria_search import RiaSearch, matches, parse_ids
from .valuation import reasons

CALL_CAP = 3
FLAGS = ("damage", "onRepairParts", "abroad", "custom")


def validate_id(value):
    if value and not re.fullmatch(r"[1-9][0-9]{0,11}", value):
        raise ValueError("Invalid diagnostic listing ID")


def condition_fields(raw):
    """Only known condition primitives; no seller text, VIN or credentials."""
    technical = raw.get("technicalCondition")
    flags = raw.get("autoInfoBar")
    values = {"technical_id": technical.get("id") if isinstance(technical, dict) else None}
    values.update({key: flags.get(key) if isinstance(flags, dict) else None for key in FLAGS})
    return {key: {"type": type(value).__name__,
                  "value": value if type(value) in (bool, int) else None}
            for key, value in values.items()}


def check_once(engine, key, source_id, fetch=fetch_json):
    validate_id(source_id)
    if not source_id or not key:
        return
    probe_id = "notification-diagnostic-v1-" + source_id
    with Session(engine) as db:
        if db.get(SourceProbe, probe_id):
            return
    result = {"source_id": source_id}

    def inspect_fetch(api_key, path, params):
        # Durable count before the call, including failure or process exit.
        with Session(engine) as db:
            db.get(SourceProbe, probe_id).requests += 1
            db.commit()
        raw = fetch(api_key, path, params)
        if path == "info":
            result["condition_fields"] = condition_fields(raw)
        return raw

    source = RiaSearch(engine, key, inspect_fetch)
    source.request_limit = CALL_CAP
    try:
        source.acquire()
    except RiaError:
        # No claim or provider calls: a later startup may try after the lease.
        logging.warning("Notification diagnostic unavailable: source lease")
        return
    try:
        with Session(engine) as db:
            db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return
            job = db.get(MonitorJob, source_id)
            result["monitor_job"] = ({"state": job.state, "first_seen": job.first_seen,
                                      "last_attempt": job.last_attempt}
                                     if job else None)
            result["seen_subscriptions"] = db.scalar(select(func.count()).select_from(MonitorSeen)
                                                      .where(MonitorSeen.source_id == source_id))
            members = [(Filters.model_validate(search.filters), member.started_at)
                       for search, _, member in active_members(db)]
        state = "checked"
        try:
            car = source.car(source_id, force=True)
            result["valuation_blockers"] = reasons(car, time.time())
            result["source_added_at"] = car.get("source_added_at")
            result["subscriptions"] = {"active": len(members), "checked": 0, "matching": 0,
                                       "matching_active_before_listing": 0}
            for filters, started_at in members:
                _, ids = source.parameters(filters)
                result["subscriptions"]["checked"] += 1
                if matches(car, filters, ids):
                    result["subscriptions"]["matching"] += 1
                    if car.get("source_added_at") and started_at <= car["source_added_at"]:
                        result["subscriptions"]["matching_active_before_listing"] += 1
        except RiaError as exc:
            state = "partial"
            result["error"] = str(exc) if str(exc) in {
                "busy", "search_limit", "quota_exceeded", "connection_error", "upstream_error",
                "key_rejected", "access_denied", "listing_unavailable", "invalid_response",
                "unsupported_filter"} else "source_error"
        except Exception as exc:
            state = "failed"
            result["error_type"] = type(exc).__name__
        result["requests_used"] = source.requests_made
        with Session(engine) as db:
            probe = db.get(SourceProbe, probe_id)
            probe.status, probe.result, probe.checked_at = state, result, time.time()
            db.commit()
        logging.warning("Notification diagnostic %s", json.dumps(result, sort_keys=True))
    finally:
        source.release()


def check_dates_once(engine, key, source_id, fetch=fetch_json):
    """Record the incident's date window without an undocumented ID search.

    Older probes already contain the two inconclusive results from that search.
    New probes proceed directly to the documented VIN-filter diagnostic.
    """
    if not source_id or not key:
        return
    validate_id(source_id)
    probe_id = "notification-diagnostic-v1-" + source_id
    with Session(engine) as db:
        probe = db.get(SourceProbe, probe_id)
        if (not probe or "date_check" in probe.result
                or probe.result.get("monitor_job") is not None
                or not probe.result.get("subscriptions", {}).get("matching")):
            return
    with Session(engine) as db:
        probe = db.scalar(select(SourceProbe).where(SourceProbe.id == probe_id).with_for_update())
        if "date_check" in probe.result:
            return
        members = active_members(db)
        if not members:
            return
        after, before = min(member.started_at for _, _, member in members), time.time()
        result = dict(probe.result)
        result["date_check"] = {"status": "unverified_id_filter", "after": stamp(after),
                                "before": stamp(before)}
        probe.result, probe.checked_at = result, time.time()
        db.commit()


def check_vin_dates_once(engine, key, source_id, fetch=fetch_json):
    """Correct an inconclusive v1 date check using the documented VIN filter.

    v1 used an undocumented search parameter for the listing ID, so two empty
    responses cannot establish that a freshly displayed listing was absent.
    This separate durable claim preserves v1's spent-call record and allows at
    most three additional calls for an explicitly selected incident. The VIN
    exists only in memory and in the provider request; it is never logged or
    stored in the result/cache payload.
    """
    validate_id(source_id)
    if not source_id or not key:
        return
    old_id = "notification-diagnostic-v1-" + source_id
    probe_id = "notification-diagnostic-v2-" + source_id
    with Session(engine) as db:
        old = db.get(SourceProbe, old_id)
        if not old or db.get(SourceProbe, probe_id):
            return
        prior = old.result if isinstance(old.result, dict) else {}
        dates = prior.get("date_check") or {}
        legacy_inconclusive = (dates.get("status") == "checked" and dates.get("created") is False
                               and dates.get("published") is False)
        if (prior.get("monitor_job") is not None or not prior.get("subscriptions", {}).get("matching")
                or not (legacy_inconclusive or dates.get("status") == "unverified_id_filter")):
            return
        after, before = dates["after"], dates["before"]

    vin = None

    def bounded_fetch(api_key, path, params):
        nonlocal vin
        with Session(engine) as db:
            probe = db.get(SourceProbe, probe_id)
            if probe.requests >= CALL_CAP:
                raise RiaError("search_limit")
            probe.requests += 1
            db.commit()
        raw = fetch(api_key, path, params)
        if path == "info":
            value = raw.get("VIN") if isinstance(raw, dict) else None
            if isinstance(value, str) and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper()):
                vin = value.upper()
        return raw

    source = RiaSearch(engine, key, bounded_fetch)
    source.request_limit = CALL_CAP
    try:
        source.acquire()
    except RiaError:
        return
    try:
        with Session(engine) as db:
            db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return
        result = {"source_id": source_id, "method": "documented_vin_filter",
                  "after": after, "before": before}
        state = "checked"
        try:
            source.car(source_id, force=True)
            if not vin:
                state = "incomplete"
                result["reason"] = "vin_unavailable"
            else:
                for field in ("created", "published"):
                    params = {"category_id": 1, "searchType": 4, "status_id": 0,
                              "VIN[0]": vin, "countpage": 50, "page": 0,
                              field + "_after": after, field + "_before": before}
                    data = source.request("search", params, parse_ids, force=True)
                    if data["total"] > len(data["ids"]):
                        state = "incomplete"
                        result["reason"] = "multiple_pages"
                        break
                    result[field] = source_id in data["ids"]
        except RiaError as exc:
            state = "incomplete"
            result["reason"] = str(exc) if str(exc) in {
                "busy", "search_limit", "quota_exceeded", "connection_error", "upstream_error",
                "key_rejected", "access_denied", "listing_unavailable", "invalid_response"
            } else "source_error"
        except Exception as exc:
            state = "failed"
            result["error_type"] = type(exc).__name__
        with Session(engine) as db:
            probe = db.get(SourceProbe, probe_id)
            result["requests_used"] = probe.requests
            probe.status, probe.result, probe.checked_at = state, result, time.time()
            db.commit()
        logging.warning("Notification VIN date diagnostic %s", json.dumps(result, sort_keys=True))
    finally:
        source.release()
