"""Explicit all-brand configuration compiles to one bounded wildcard search."""
import hashlib
import json
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import radar_monitor
from backend.models import Filters, MonitorWatch, Search
from backend.radar_models import BrandCatalog, Radar, RadarBrand, RadarModel, RadarSearch
from backend.ria_search import matches
from backend.tests.test_radar_backend import radar_db, payload, paid


def all_payload(**changes):
    return payload(all_brands=True, brands=[], **changes)


def add_all(client, **changes):
    response = client.post("/api/radars", json=all_payload(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def branches(db, rid):
    return list(db.scalars(select(Search).join(RadarSearch, RadarSearch.search_id == Search.id)
                          .where(RadarSearch.radar_id == rid)))


def test_all_brands_save_reload_and_resume_has_one_wildcard_binding(radar_db):
    engine, client = radar_db
    row = add_all(client)
    assert row["all_brands"] is True and row["brands"] == [] and row["status"] == "paused"
    assert client.get("/api/radars").json()["radars"] == [row]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(RadarBrand)) == 0
        assert db.scalar(select(func.count()).select_from(RadarModel)) == 0
        compiled = branches(db, row["id"])
        assert len(compiled) == 1 and not compiled[0].enabled
        filters = Filters.model_validate(compiled[0].filters)
        assert filters.brand == filters.model == ""
    assert client.patch(f"/api/radars/{row['id']}/status", json={"active": True, "revision": 1}).status_code == 402
    paid(engine)
    resumed = client.patch(f"/api/radars/{row['id']}/status", json={"active": True, "revision": 1})
    assert resumed.status_code == 200 and resumed.json()["all_brands"] is True
    with Session(engine) as db:
        compiled = branches(db, row["id"])
        assert len(compiled) == 1 and compiled[0].enabled
        params, ids = radar_monitor.parameters(db, Filters.model_validate(compiled[0].filters))
        assert not any(key.startswith(("marka_id", "model_id")) for key in params)
        assert "brand_id" not in ids and "model_id" not in ids
        assert params["price_ot"] == 5000 and params["price_do"] == 10000
        assert params["s_yers[0]"] == 2005 and params["po_yers[0]"] == 2026


@pytest.mark.parametrize("changes", [
    {"brands": []}, {"all_brands": False, "brands": []},
    {"all_brands": True}, {"all_brands": "true", "brands": []},
    {"all_brands": 1, "brands": []}, {"all_brands": None, "brands": []},
    {"all_brands": True, "brands": [{"brand_id": 9, "model_ids": []}]},
])
def test_missing_or_ambiguous_all_brand_selection_is_rejected_atomically(radar_db, changes):
    engine, client = radar_db
    response = client.post("/api/radars", json=payload(**changes))
    assert response.status_code == 422
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar)) == 0
        assert db.scalar(select(func.count()).select_from(Search)) == 0


def test_specific_payload_compatibility_and_historical_idempotency_hash(radar_db):
    engine, client = radar_db
    body = payload(client_request_id="legacy-specific-action-0001")
    row = client.post("/api/radars", json=body).json()
    assert row["all_brands"] is False and row["brands"][0]["model_ids"] == [6]
    old_canonical = {key: value for key, value in body.items() if key != "client_request_id"}
    legacy_hash = hashlib.sha256(json.dumps(old_canonical, sort_keys=True).encode()).hexdigest()
    with Session(engine) as db:
        assert db.get(Radar, row["id"]).create_hash == legacy_hash
    retried = client.post("/api/radars", json={**body, "all_brands": False})
    assert retried.status_code == 201 and retried.json()["id"] == row["id"]
    changed = client.post("/api/radars", json={**body, "all_brands": True, "brands": []})
    assert changed.status_code == 409


def test_all_brand_idempotency_retry_occupies_one_slot(radar_db):
    engine, client = radar_db
    body = all_payload(client_request_id="all-brands-action-0001")
    first = client.post("/api/radars", json=body)
    retry = client.post("/api/radars", json=body)
    assert first.status_code == retry.status_code == 201 and first.json() == retry.json()
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar)) == 1
        assert db.scalar(select(func.count()).select_from(RadarSearch)) == 1


def test_specific_to_all_and_back_clears_models_and_replaces_epoch(radar_db):
    engine, client = radar_db
    paid(engine)
    specific = payload(brands=[{"brand_id": 9, "model_ids": [6, 7]}], region_ids=[10])
    created = client.post("/api/radars", json=specific).json()
    active = client.patch(f"/api/radars/{created['id']}/status", json={"active": True, "revision": 1}).json()
    with Session(engine) as db:
        old_epochs = {db.get(MonitorWatch, row.id).epoch for row in branches(db, created["id"])}
        assert len(old_epochs) == 2
    changed = client.put(f"/api/radars/{created['id']}", json=all_payload(region_ids=[10], revision=active["revision"]))
    assert changed.status_code == 200 and changed.json()["status"] == "active"
    with Session(engine) as db:
        compiled = branches(db, created["id"])
        assert len(compiled) == 1 and compiled[0].enabled
        assert db.get(MonitorWatch, compiled[0].id).epoch not in old_epochs
        assert db.scalar(select(func.count()).select_from(RadarBrand)) == 0
        assert db.scalar(select(func.count()).select_from(RadarModel)) == 0
        filters = Filters.model_validate(compiled[0].filters)
        assert filters.brand == filters.model == "" and filters.regions == ["Київська"]
    narrower = client.put(f"/api/radars/{created['id']}", json=payload(
        brands=[{"brand_id": 84, "model_ids": [30]}], all_brands=False,
        revision=changed.json()["revision"], region_ids=[10]))
    assert narrower.status_code == 200 and narrower.json()["all_brands"] is False
    with Session(engine) as db:
        compiled = branches(db, created["id"])
        assert len(compiled) == 1
        filters = Filters.model_validate(compiled[0].filters)
        assert filters.brand == "Volkswagen" and filters.model == "Golf"
        assert db.scalar(select(func.count()).select_from(RadarBrand)) == 1
        assert db.scalar(select(func.count()).select_from(RadarModel)) == 1


def test_wildcard_matches_unrelated_brands_but_retains_all_other_boundaries(radar_db):
    engine, client = radar_db
    row = add_all(client, region_ids=[10])
    with Session(engine) as db:
        filters = Filters.model_validate(branches(db, row["id"])[0].filters)
        _, ids = radar_monitor.parameters(db, filters)
        candidate = {"brand_id": 9, "model_id": 6, "region_id": 10, "price_usd": 7500, "year": 2018}
        assert matches(candidate, filters, ids)
        assert matches({**candidate, "brand_id": 84, "model_id": 30}, filters, ids)
        # The provider wildcard also includes a new brand not yet in our catalog.
        assert matches({**candidate, "brand_id": 99999, "model_id": 99999}, filters, ids)
        for changed in ({"region_id": 4}, {"price_usd": 4999}, {"price_usd": 10000}, {"year": 2004}, {"year": 2027}):
            assert not matches({**candidate, **changed}, filters, ids)


def test_catalog_growth_never_expands_a_wildcard_into_per_brand_searches(radar_db):
    engine, client = radar_db
    row = add_all(client)
    with Session(engine) as db, db.begin():
        db.add_all(BrandCatalog(id=1000 + index, name=f"Future brand {index}") for index in range(398))
    paid(engine)
    resumed = client.patch(f"/api/radars/{row['id']}/status", json={"active": True, "revision": 1})
    assert resumed.status_code == 200
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BrandCatalog)) == 400
        assert len(branches(db, row["id"])) == 1
        assert db.scalar(select(func.count()).select_from(RadarBrand)) == 0


def test_all_brand_ownership_pause_expiry_and_slot_limits_are_unchanged(radar_db):
    engine, client = radar_db
    rows = [add_all(client, name=f"Радар {index}") for index in range(3)]
    assert client.post("/api/radars", json=all_payload()).status_code == 409
    row = rows[0]
    assert client.put(f"/api/radars/{row['id']}", json=all_payload(revision=1),
                      headers={"x-test-user": "222"}).status_code == 404
    paid(engine)
    active = client.patch(f"/api/radars/{row['id']}/status", json={"active": True, "revision": 1}).json()
    paused = client.patch(f"/api/radars/{row['id']}/status", json={"active": False, "revision": active["revision"]}).json()
    assert paused["status"] == "paused" and paused["all_brands"] is True
    resumed = client.patch(f"/api/radars/{row['id']}/status", json={"active": True, "revision": paused["revision"]}).json()
    paid(engine, until=time.time() - 1)
    expired = next(value for value in client.get("/api/radars").json()["radars"] if value["id"] == row["id"])
    assert expired["status"] == "subscription_expired" and expired["all_brands"] is True
    assert expired["revision"] > resumed["revision"]
    with Session(engine) as db:
        assert len(branches(db, row["id"])) == 1 and not branches(db, row["id"])[0].enabled
