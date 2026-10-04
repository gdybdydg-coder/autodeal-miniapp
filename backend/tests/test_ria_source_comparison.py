"""Bounded production diagnostic, using offline fixture transports only."""
import copy
import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import paid_source_access, ria_ai_price as ai, ria_source_comparison as comparison
from backend.auto_ria import RiaError
from backend.billing_models import Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import (Delivery, Listing, MonitorJob, MonitorMatch, MonitorMembership,
    MonitorSeen, MonitorWatch, Search, SourceBudget, SourceCache, SourceProbe, User, ValuationPeer)
from backend.ria_search import parse_car
from backend.tests.test_monitor import p
from backend.tests.test_paid_sources_production import approve
from backend.tests.test_ria_search import raw

SID = "40517476"


@pytest.fixture
def incident(p, monkeypatch):
    ManualBase.metadata.create_all(p.engine)
    p.settings = replace(p.settings, admin_telegram_id=111, auto_ria_user_id="42",
        ria_ai_price_enabled=True, ria_source_comparison_listing_id=SID)
    data = raw(SID, VIN="PRIVATE_VIN", sellerName="PRIVATE_SELLER")
    data["autoData"].update(categoryId=1, fuelName="Дизель, 2 л.")
    p.data = data
    p.candidate = parse_car(data, SID)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id=SID, state="done", first_seen=p.clock[0]-10,
            result={"candidate": p.candidate, "private_raw": "PRIVATE_JOB"}))
        watch = db.get(MonitorWatch, 1)
        db.add(MonitorSeen(search_id=1, source_id=SID, epoch=watch.epoch,
            state="done", first_seen=p.clock[0]-10))
        db.add(ValuationPeer(source_id=SID, group_key="fixture", car={"sentinel": "PRIVATE_PEER"},
            observed_at=p.clock[0], available=True))
        for path, items in (("categories/1/marks", [{"name": "Volkswagen", "value": 84}]),
                ("categories/1/marks/84/models", [{"name": "Golf", "value": 30}]),
                ("states", [{"name": "Хмельницька", "value": 4}]),
                ("type", [{"name": "Дизель", "value": 2}])):
            digest = hashlib.sha256(json.dumps([path, {}], sort_keys=True).encode()).hexdigest()
            db.add(SourceCache(id=digest, expires_at=p.clock[0]+3600, payload={"items": items}))
        db.commit()
    p.source_calls, p.quote_calls, p.sources = [], [], []
    def factory(engine, key):
        source = p.runner.search_factory(engine, key)
        def fetch(key, path, params):
            p.source_calls.append((path, copy.deepcopy(params)))
            assert path == "info" and params == {"auto_id": SID}
            if getattr(p, "detail_action", None):
                p.detail_action()
            if getattr(p, "detail_error", None):
                raise RiaError(p.detail_error)
            return copy.deepcopy(p.data)
        source.fetch = fetch
        p.sources.append(source)
        return source
    def quote(key, uid, sid, *, period_parameter, params, attempt_telemetry):
        p.quote_calls.append((period_parameter, copy.deepcopy(params)))
        assert key == "test-only" and uid == "42" and sid == SID
        if getattr(p, "quote_action", None):
            p.quote_action()
        if getattr(p, "quote_error", None):
            raise RiaError(p.quote_error)
        return {"source_id": SID, "basis": ai.API_BASIS, "currency": "USD",
            "lower_usd": 4707, "upper_usd": 5202, "average_usd": 4955,
            "range_fraction": .05, "quantity": 170, "period_parameter": period_parameter,
            "observed_at": p.clock[0], "sellerName": "PRIVATE_SELLER", "VIN": "PRIVATE_VIN"}
    monkeypatch.setattr(ai, "fetch_observation", quote)
    p.comparison_factory = factory
    return p


def run(p):
    return comparison.check_once(p.engine, p.settings, source_factory=p.comparison_factory)


def probe(p):
    with Session(p.engine) as db:
        row = db.get(SourceProbe, comparison.VERSION + "-" + SID)
        return row.status, row.requests, copy.deepcopy(row.result)


def protected_tables(p):
    models = (MonitorJob, MonitorSeen, MonitorMatch, Listing, Delivery, ValuationPeer,
              Search, MonitorWatch, MonitorMembership)
    with Session(p.engine) as db:
        return {model.__tablename__: sorted(json.dumps(
            {column.name: getattr(row, column.name) for column in model.__table__.columns},
            sort_keys=True) for row in db.scalars(select(model))) for model in models}


def test_default_off_does_not_touch_database_or_construct_sources():
    settings = type("Settings", (), {"ria_source_comparison_listing_id": ""})()
    assert comparison.check_once(object(), settings,
        source_factory=lambda *a: pytest.fail("default-off source construction")) is None


@pytest.mark.parametrize("change", [
    {"ria_source_comparison_listing_id": "bad"}, {"delivery_enabled": False},
    {"source_ready": False}, {"monitor_enabled": False}, {"ria_ai_price_enabled": False},
    {"auto_ria_api_key": ""}, {"auto_ria_user_id": "bad"}, {"admin_telegram_id": 0}])
def test_invalid_or_disabled_configuration_makes_no_claim_or_calls(incident, change):
    p = incident
    p.settings = replace(p.settings, **change)
    assert run(p) is None and not p.sources
    with Session(p.engine) as db:
        assert db.get(SourceProbe, comparison.VERSION + "-" + SID) is None


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("access", ["none", "gift", "pending", "expired", "revoked", "paid"])
def test_requires_current_real_purchase_even_when_strict_engine_off(incident, strict, access):
    p = incident
    p.engine.update_execution_options(**{paid_source_access.OPTION: strict})
    if access == "gift":
        with Session(p.engine) as db:
            db.add(Entitlement(user_id=111, expires_at=p.clock[0]+3600, updated_at=p.clock[0]))
            db.commit()
    elif access != "none":
        approve(p, state="review" if access == "pending" else "approved",
            purchase_until=p.clock[0] if access == "expired" else None,
            access_until=p.clock[0] if access == "revoked" else None)
    report = run(p)
    assert report["requests"] == (4 if access == "paid" else 0)
    assert len(p.quote_calls) == (3 if access == "paid" else 0)
    if access != "paid":
        assert not p.sources and report["status"] == "no_eligible_subscription"


def test_four_bounded_attempts_once_only_redacted_and_protected_tables_unchanged(incident, caplog, monkeypatch):
    p = incident
    approve(p)
    monkeypatch.setattr(comparison.log, "handlers", [caplog.handler])
    before = protected_tables(p)
    report = run(p)
    assert report["status"] == "observed_unverified_native" and report["requests"] == 4
    assert len(p.sources) == 4 and all(source.requests_made == source.request_limit == 1 for source in p.sources)
    assert p.source_calls == [("info", {"auto_id": SID})]
    assert p.quote_calls == [(168, {"omniId": SID}), (90, {"omniId": SID}),
        (168, {"categoryId": "1", "brandId": "84", "modelId": "30", "bodyId": "4",
               "fuelId": "2", "gearBoxId": "2", "year": {"gte": "2017", "lte": "2017"},
               "mileage": {"gte": "100", "lte": "100"}, "engineVolume": {"gte": "2", "lte": "2"},
               "generationId": "10", "modificationId": "20"})]
    assert protected_tables(p) == before and not p.sent
    assert report["result"]["native_parity_verified"] is False
    assert report["result"]["detail_availability"] == {
        "auto_data_present": True, "requested_id_matches": True, "isSold": False, "active": True, "statusId": 0}
    assert "PRIVATE" not in json.dumps(report) and "PRIVATE" not in caplog.text
    assert "AUTO.RIA source comparison" in caplog.text
    assert run(p) == report
    assert len(p.sources) == 4
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == 6


@pytest.mark.parametrize("mutation", ["stop", "revoke", "expire", "disable", "filters", "epoch", "feed"])
@pytest.mark.parametrize("after", ["detail", "first_quote"])
def test_rechecks_access_and_frozen_search_between_every_call(incident, mutation, after):
    p = incident
    approve(p)
    def action():
        with Session(p.engine) as db:
            if mutation == "stop": db.get(User, 111).ready = False
            elif mutation == "revoke": db.get(Entitlement, 111).expires_at = p.clock[0]
            elif mutation == "expire": db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0]
            elif mutation == "disable": db.get(Search, 1).enabled = False
            elif mutation == "filters":
                search = db.get(Search, 1)
                search.filters = {**search.filters, "price": {"to": 1}}
            elif mutation == "epoch": db.get(MonitorWatch, 1).epoch = "changed"
            elif mutation == "feed": db.get(MonitorMembership, 1).feed_id = "changed"
            db.commit()
    if after == "detail": p.detail_action = action
    else: p.quote_action = action
    report = run(p)
    assert report["status"] == "no_eligible_subscription"
    assert report["requests"] == (1 if after == "detail" else 2)
    assert len(p.quote_calls) == (0 if after == "detail" else 1)
    assert run(p) == report


@pytest.mark.parametrize("mutation", ["candidate_missing", "candidate_mismatch", "not_ready", "disabled",
    "fingerprint", "membership_missing", "epoch", "feed", "catalog_missing", "price_not_matching"])
def test_retained_details_and_permitted_current_search_are_required_before_spend(incident, mutation):
    p = incident
    approve(p)
    with Session(p.engine) as db:
        if mutation.startswith("candidate"):
            job = db.get(MonitorJob, SID)
            candidate = {} if mutation == "candidate_missing" else {**p.candidate, "id": "123"}
            job.result = {"candidate": candidate}
        elif mutation == "not_ready": db.get(User, 111).ready = False
        elif mutation == "disabled": db.get(Search, 1).enabled = False
        elif mutation == "fingerprint": db.get(Search, 1).fingerprint = "changed"
        elif mutation == "membership_missing": db.delete(db.get(MonitorMembership, 1))
        elif mutation == "epoch": db.get(MonitorWatch, 1).epoch = "changed"
        elif mutation == "feed": db.get(MonitorMembership, 1).feed_id = "changed"
        elif mutation == "catalog_missing":
            for row in db.scalars(select(SourceCache)): db.delete(row)
        elif mutation == "price_not_matching":
            job = db.get(MonitorJob, SID)
            job.result = {"candidate": {**p.candidate, "brand_id": 1}}
        db.commit()
    report = run(p)
    assert report["requests"] == 0 and not p.sources


@pytest.mark.parametrize("change,expected", [({"isSold": True}, "listing_unavailable"),
    ({"active": False}, "listing_unavailable"), ({"statusId": 2}, "listing_unavailable"),
    ({"autoId": 123}, "invalid_response"), ({"fuelName": "Дизель"}, "comparison_details_incomplete")])
def test_fresh_unavailable_or_incomplete_details_stop_quotes_without_peer_writes(incident, change, expected):
    p = incident
    approve(p)
    before = protected_tables(p)
    p.data["autoData"].update(change)
    report = run(p)
    assert report["status"] == expected and report["requests"] == 1 and not p.quote_calls
    assert protected_tables(p) == before
    assert report["result"]["detail_availability"]["requested_id_matches"] == (change.get("autoId", int(SID)) == int(SID))
    assert run(p) == report and len(p.source_calls) == 1


@pytest.mark.parametrize("error", ["invalid_response", "connection_error", "info_endpoint_unavailable"])
def test_failed_detail_transport_never_invalidates_peer_or_retries(incident, error):
    p = incident
    approve(p)
    before = protected_tables(p)
    p.detail_error = error
    report = run(p)
    assert report["status"] == error and report["requests"] == 1
    assert protected_tables(p) == before and not p.quote_calls
    assert run(p) == report and len(p.source_calls) == 1


def test_failed_quote_retains_completed_detail_and_never_issues_next_variant(incident):
    p = incident
    approve(p)
    p.quote_error = "ai_connection_error"
    report = run(p)
    assert report["status"] == "ai_connection_error" and report["requests"] == 2
    assert len(p.quote_calls) == 1 and report["result"]["observations"] == {}
    assert run(p) == report


def test_durable_crash_claim_is_never_resumed(incident):
    p = incident
    approve(p)
    with Session(p.engine) as db:
        db.add(SourceProbe(id=comparison.VERSION + "-" + SID, status="checking", checked_at=p.clock[0],
            requests=1, result={"step": {"name": "omni_168", "state": "started"}, "native_parity_verified": False}))
        db.commit()
    report = run(p)
    assert report["status"] == "checking" and report["requests"] == 1 and not p.sources


def test_busy_claim_has_no_paid_reservations_and_never_retries(incident):
    p = incident
    approve(p)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").busy_until = p.clock[0]+90
        db.commit()
    report = run(p)
    assert report["status"] == "busy" and report["requests"] == 0
    assert not p.source_calls and not p.quote_calls
    assert run(p) == report and len(p.sources) == 1


@pytest.mark.parametrize("change", [{"engine_cc": None}, {"mileage": 0}, {"body_id": None},
    {"year": True}, {"generation_id": "10"}, {"fuel_id": -1}])
def test_explicit_parameters_fail_closed_for_missing_or_invalid_dimensions(change):
    candidate = {"category_id": 1, "brand_id": 62, "model_id": 60014, "body_id": 8,
        "fuel_id": 2, "gear_id": 1, "year": 2005, "mileage": 345000, "engine_cc": 1900,
        "generation_id": 9811, "modification_id": None, **change}
    with pytest.raises(RiaError, match="comparison_details_incomplete"):
        comparison.explicit_parameters(candidate)
