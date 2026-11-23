"""Offline contract, normalized-schema, lifecycle and slot-race regressions."""
import time
import json
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app import Settings  # registers existing billing/Telegram schemas
from backend.billing_models import Entitlement
from backend.models import Base, Search, User
from backend.radar_api import install
from backend.radar_catalog import import_catalog, resolve, view
from backend.radar_migration import migrate
from backend.radar_models import (MIGRATION_VERSION, Radar, RadarBrand, RadarMigration,
                                 RadarModel, RadarRegion, RadarSearch, RadarLegacySearchState,
                                 BrandCatalog,ModelCatalog,RegionCatalog,FuelCatalog)
from backend.radar_service import PRICE_BOUNDS, RadarCreate, RadarError, create


CATALOG={"brands":[{"id":9,"name":"BMW","models_available":True,
                     "models":[{"id":6,"name":"3 Series"},{"id":7,"name":"5 Series"}]},
                    {"id":84,"name":"Volkswagen","models_available":True,
                     "models":[{"id":30,"name":"Golf"},{"id":31,"name":"Passat"}]}],
         "regions":[{"id":10,"name":"Київська"},{"id":4,"name":"Хмельницька"}]}


@pytest.fixture
def radar_db(tmp_path,monkeypatch):
    engine=create_engine("sqlite:///"+str(tmp_path/"radars.db"),
                        connect_args={"check_same_thread":False,"timeout":20})
    @event.listens_for(engine,"connect")
    def enable_fk(connection,_):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    from backend import radar_catalog
    monkeypatch.setattr(radar_catalog,"SEED_PATH",tmp_path/"no-seed.json")
    with Session(engine) as db,db.begin():
        db.add_all([User(id=111,ready=True),User(id=222,ready=True)])
        import_catalog(db,CATALOG)
    migrate(engine)
    application=FastAPI()
    def identity(x_test_user:int=Header(default=111)):
        return x_test_user
    install(application,engine,identity)
    with TestClient(application) as client:
        yield engine,client
    engine.dispose()


def payload(**overrides):
    return {"name":"Мій радар","brands":[{"brand_id":9,"model_ids":[6]}],
            "price_category":3,"region_ids":[],"year_from":2005,"year_to":2026,
            "min_profit":10,**overrides}


def paid(engine,uid=111,until=None):
    with Session(engine) as db,db.begin():
        db.merge(Entitlement(user_id=uid,expires_at=until or time.time()+3600,updated_at=time.time()))


def add(client,**values):
    result=client.post("/api/radars",json=payload(**values))
    assert result.status_code==201,result.text
    return result.json()


def test_empty_slots_and_first_paused_radar_without_subscription(radar_db):
    engine,client=radar_db
    listing=client.get("/api/radars").json()
    assert listing=={"radars":[],"limit":3,"access":{"active":False,"expires_at":None}}
    result=add(client)
    assert result["slot"]==1 and result["status"]=="paused"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(RadarBrand))==1
        assert db.scalar(select(func.count()).select_from(RadarModel))==1
        assert not db.scalar(select(func.count()).select_from(Search).where(Search.enabled))


def test_three_slots_fourth_denied_delete_frees_exact_slot(radar_db):
    engine,client=radar_db
    radars=[add(client,name=f"Радар {i}") for i in range(3)]
    assert [r["slot"] for r in radars]==[1,2,3]
    assert client.post("/api/radars",json=payload()).status_code==409
    assert client.delete(f"/api/radars/{radars[1]['id']}?revision=1").status_code==204
    assert add(client,name="Новий")["slot"]==2
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar))==3
        assert db.scalar(select(func.count()).select_from(RadarSearch))==3


def test_edit_multiple_brands_models_regions_and_independent_config(radar_db):
    _,client=radar_db
    one,two=add(client),add(client,name="Другий")
    changed=payload(name="Дві марки",brands=[{"brand_id":9,"model_ids":[6,7]},
        {"brand_id":84,"model_ids":[30,31]}],region_ids=[10,4],min_profit=30,revision=1)
    response=client.put(f"/api/radars/{one['id']}",json=changed)
    assert response.status_code==200,response.text
    result=response.json()
    assert {b["brand_id"]:b["model_ids"] for b in result["brands"]}=={9:[6,7],84:[30,31]}
    assert set(result["region_ids"])=={10,4} and result["min_profit"]==30
    unchanged=next(r for r in client.get("/api/radars").json()["radars"] if r["id"]==two["id"])
    assert unchanged["name"]=="Другий" and unchanged["min_profit"]==10


def test_entire_ukraine_switch_and_stale_editor(radar_db):
    _,client=radar_db
    row=add(client,region_ids=[4,10])
    result=client.put(f"/api/radars/{row['id']}",json=payload(revision=1))
    assert result.status_code==200 and result.json()["region_ids"]==[]
    assert client.put(f"/api/radars/{row['id']}",json=payload(revision=1)).status_code==409
    assert client.delete(f"/api/radars/{row['id']}?revision=1").status_code==409
    assert client.put(f"/api/radars/{row['id']}",json=payload(revision=2,region_ids=[10])).json()["region_ids"]==[10]


@pytest.mark.parametrize("changes",[
    {"brands":[]},{"brands":[{"brand_id":9,"model_ids":[6,6]}]},
    {"brands":[{"brand_id":9,"model_ids":[]},{"brand_id":9,"model_ids":[]}]},
    {"brands":[{"brand_id":9,"model_ids":[30]}]},
    {"brands":[{"brand_id":77777,"model_ids":[]}]},
    {"region_ids":[4,4]},{"region_ids":[99999]},{"fuel_ids":[1,1]},{"fuel_ids":[99999]},{"year_from":2020,"year_to":2010},
    {"year_to":2100},{"year_from":1899},{"min_profit":12},{"price_category":0},
    {"price_category":7},{"min_profit":True},{"year_from":"2005"},{"name":"  "},
    {"user_id":222},{"status":"active"},{"brands":[{"brand_id":True,"model_ids":[]}]}])
def test_invalid_inputs_never_create_partial_radar(radar_db,changes):
    engine,client=radar_db
    assert client.post("/api/radars",json=payload(**changes)).status_code==422
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar))==0


@pytest.mark.parametrize("category",range(1,7))
@pytest.mark.parametrize("profit",[5,10,15,20,30,40,50])
def test_all_predefined_prices_and_profit_values(radar_db,category,profit):
    _,client=radar_db
    result=add(client,price_category=category,min_profit=profit)
    assert result["price_category"]==category and result["min_profit"]==profit


def test_half_open_price_boundaries_cover_exactly_one_category():
    for value in [0,1999.99,2000,4999.99,5000,9999.99,10000,14999.99,15000,19999.99,20000,1000000]:
        assert sum(low<=value and (high is None or value<high) for low,high in PRICE_BOUNDS.values())==1


def test_pause_resume_expire_renew_manual_resume(radar_db):
    engine,client=radar_db
    row=add(client)
    url=f"/api/radars/{row['id']}/status"
    assert client.patch(url,json={"active":True,"revision":1}).status_code==402
    paid(engine)
    active=client.patch(url,json={"active":True,"revision":1}).json()
    assert active["status"]=="active"
    paused=client.patch(url,json={"active":False,"revision":active["revision"]}).json()
    assert paused["status"]=="paused" and paused["brands"]==row["brands"]
    active=client.patch(url,json={"active":True,"revision":paused["revision"]}).json()
    paid(engine,until=time.time()-1)
    expired=client.get("/api/radars").json()["radars"][0]
    assert expired["status"]=="subscription_expired"
    paid(engine)
    assert client.get("/api/radars").json()["radars"][0]["status"]=="subscription_expired"
    resumed=client.patch(url,json={"active":True,"revision":expired["revision"]})
    assert resumed.status_code==200 and resumed.json()["status"]=="active"


def test_expiry_gap_cannot_auto_resume_between_worker_ticks(radar_db):
    engine,client=radar_db
    paid(engine)
    row=add(client)
    client.patch(f"/api/radars/{row['id']}/status",json={"active":True,"revision":1})
    with Session(engine) as db,db.begin():
        db.get(Radar,row["id"]).active_until=time.time()-1
    paid(engine)
    assert client.get("/api/radars").json()["radars"][0]["status"]=="subscription_expired"


def test_unpaid_edit_delete_pause_and_user_isolation(radar_db):
    _,client=radar_db
    row=add(client)
    other={"X-Test-User":"222"}
    assert client.get("/api/radars",headers=other).json()["radars"]==[]
    assert client.put(f"/api/radars/{row['id']}",headers=other,json=payload(revision=1)).status_code==404
    assert client.patch(f"/api/radars/{row['id']}/status",headers=other,json={"active":False,"revision":1}).status_code==404
    assert client.delete(f"/api/radars/{row['id']}?revision=1",headers=other).status_code==404
    edited=client.put(f"/api/radars/{row['id']}",json=payload(revision=1,name="Без оплати"))
    assert edited.status_code==200
    assert client.patch(f"/api/radars/{row['id']}/status",json={"active":False,"revision":2}).status_code==200
    assert client.delete(f"/api/radars/{row['id']}?revision=3").status_code==204


def test_catalog_uses_official_ids_without_api_calls(radar_db):
    _,client=radar_db
    result=client.get("/api/radars/catalog?brand_id=84").json()
    assert {r["id"] for r in result["regions"]}=={4,10}
    assert result["models"]==[{"id":30,"brand_id":84,"name":"Golf"},{"id":31,"brand_id":84,"name":"Passat"}]
    assert client.get("/api/radars/catalog?brand_id=999").status_code==404

def test_fuel_choices_are_catalogued_and_saved_per_radar(radar_db):
    _,client=radar_db
    catalog=client.get("/api/radars/catalog").json()["fuel_types"]
    assert {item["id"] for item in catalog} >= {1,2,3,4,5,6,8,10}
    first=add(client,fuel_ids=[1,6])
    second=add(client,name="Без обмеження")
    assert first["fuel_ids"]==[1,6]
    assert first["fuel_types"]==["Бензин","Електро"]
    assert second["fuel_ids"]==[] and second["fuel_types"]==[]
    edited=client.put(f"/api/radars/{first['id']}",json=payload(fuel_ids=[2],revision=1))
    assert edited.status_code==200,edited.text
    assert edited.json()["fuel_ids"]==[2]
    unchanged=next(r for r in client.get("/api/radars").json()["radars"] if r["id"]==second["id"])
    assert unchanged["fuel_ids"]==[]



def test_create_retry_is_idempotent_and_key_cannot_change_payload(radar_db):
    engine,client=radar_db
    key="radar-request-1234567890"
    first=add(client,client_request_id=key)
    assert add(client,client_request_id=key)["id"]==first["id"]
    assert client.post("/api/radars",json=payload(client_request_id=key,name="Інший")).status_code==409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar))==1


def test_concurrent_creation_never_exceeds_three_slots(radar_db):
    engine,_=radar_db
    def concurrent(i):
        try:
            with Session(engine) as db,db.begin():
                return create(db,111,RadarCreate(**payload(name=f"Радар {i}")))["slot"]
        except RadarError as exc:
            assert exc.code=="radar_slots_full"
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(concurrent,range(8)))
    assert sorted(slot for slot in results if slot)==[1,2,3]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar))==3


def test_foreign_keys_and_database_slot_constraint(radar_db):
    engine,client=radar_db
    row=add(client)
    with Session(engine) as db:
        db.add(RadarModel(radar_id=row["id"],brand_id=9,model_id=30))
        with pytest.raises(IntegrityError):
            db.commit()
    with Session(engine) as db:
        radar=db.get(Radar,row["id"])
        radar.slot=4
        with pytest.raises(IntegrityError):
            db.commit()


def test_migration_repeat_preserves_new_radars_and_existing_accounts(radar_db):
    engine,client=radar_db
    paid(engine)
    row=add(client)
    assert migrate(engine)=={"applied":False,"version":MIGRATION_VERSION}
    assert client.get("/api/radars").json()["radars"][0]["id"]==row["id"]
    with Session(engine) as db:
        assert db.get(User,111).ready and db.get(Entitlement,111).expires_at>time.time()
        assert db.scalar(select(func.count()).select_from(RadarMigration))==1


def test_flat_catalog_seed_imports_models_with_correct_brand(radar_db):
    engine,_=radar_db
    with Session(engine) as db,db.begin():
        import_catalog(db,{"brands":[{"id":8,"name":"Audi"}],
            "models":[{"brand_id":8,"id":1,"name":"A6"}],"model_catalogs":[8],"regions":[]})
        assert resolve(db,"categories/1/marks/8/models",["A6"])==[1]
        assert view(db,8)["models"]==[{"id":1,"brand_id":8,"name":"A6"}]


def test_initial_migration_disables_only_legacy_configuration_and_preserves_billing(radar_db):
    engine,_=radar_db
    paid(engine)
    with Session(engine) as db,db.begin():
        db.delete(db.get(RadarMigration,MIGRATION_VERSION))
        old=Search(user_id=111,name="Старий пошук",fingerprint="legacy-fingerprint",
                   filters={"brand":"BMW"},enabled=True,after_listing=77)
        db.add(old)
        db.flush()
        old_id=old.id
        expiry=db.get(Entitlement,111).expires_at
    result=migrate(engine)
    assert result["applied"] is True
    with Session(engine) as db:
        old=db.get(Search,old_id)
        assert not old.enabled and old.filters=={"brand":"BMW"} and old.after_listing==77
        assert db.get(RadarLegacySearchState,old_id).enabled is True
        assert db.get(User,111).ready and db.get(Entitlement,111).expires_at==expiry
        assert db.scalar(select(func.count()).select_from(Radar))==0


def test_failed_migration_rolls_back_legacy_pause_and_marker(radar_db):
    engine,_=radar_db
    with Session(engine) as db,db.begin():
        db.delete(db.get(RadarMigration,MIGRATION_VERSION))
        row=Search(user_id=111,name="Збережений",fingerprint="migration-rollback",
                   filters={"brand":"BMW"},enabled=True,after_listing=1)
        db.add(row)
        db.flush()
        old_id=row.id
    def reject_marker(*args):
        raise RuntimeError("fixture migration failure")
    event.listen(RadarMigration,"before_insert",reject_marker)
    try:
        with pytest.raises(RuntimeError,match="fixture migration failure"):
            migrate(engine)
    finally:
        event.remove(RadarMigration,"before_insert",reject_marker)
    with Session(engine) as db:
        assert db.get(Search,old_id).enabled is True
        assert db.get(RadarMigration,MIGRATION_VERSION) is None
        assert db.get(RadarLegacySearchState,old_id) is None


def test_invalid_edit_preserves_all_previous_selections(radar_db):
    _,client=radar_db
    row=add(client,region_ids=[4,10])
    response=client.put(f"/api/radars/{row['id']}",json=payload(revision=1,
        brands=[{"brand_id":84,"model_ids":[6]}]))
    assert response.status_code==422
    saved=client.get("/api/radars").json()["radars"][0]
    assert saved==row


def test_catalog_resolution_reuses_transaction_and_invalidates_on_import(radar_db):
    engine,_=radar_db
    queries=[]
    def record(conn,cursor,statement,*args):
        if statement.split("\n")[0].strip()=="SELECT radar_catalog_brands.id, radar_catalog_brands.name":
            queries.append(statement)
    event.listen(engine,"before_cursor_execute",record)
    try:
        with Session(engine) as db:
            for _ in range(20):
                assert resolve(db,"categories/1/marks",["BMW"])==[9]
            assert len(queries)==1
            db.commit()
            assert resolve(db,"categories/1/marks",["BMW"])==[9]
            assert len(queries)==2
            import_catalog(db,{"brands":[{"id":8,"name":"Audi"}],"regions":[]})
            assert resolve(db,"categories/1/marks",["Audi"])==[8]
            assert len(queries)==3
    finally:
        event.remove(engine,"before_cursor_execute",record)


def test_complete_official_seed_import_is_repeatable_and_unambiguous(tmp_path):
    from backend.ria_search import normalize
    seed=json.loads((Path(__file__).parents[1]/"data"/"radar_catalog_seed.json").read_text(encoding="utf-8"))
    assert len(seed["brands"])>=390 and len(seed["models"])>=5000
    assert len(seed["regions"])==25 and {17,21}<={r["id"] for r in seed["regions"]}
    assert set(seed["model_catalogs"])=={b["id"] for b in seed["brands"]}
    assert max(Counter(normalize(b["name"]) for b in seed["brands"]).values())==1
    assert max(Counter((m["brand_id"],normalize(m["name"])) for m in seed["models"]).values())==1
    engine=create_engine("sqlite:///"+str(tmp_path/"real-catalog.db"))
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db,db.begin():
            import_catalog(db,seed)
            import_catalog(db,seed)
            assert db.scalar(select(func.count()).select_from(BrandCatalog))==len(seed["brands"])
            assert db.scalar(select(func.count()).select_from(ModelCatalog))==len(seed["models"])
            assert db.scalar(select(func.count()).select_from(RegionCatalog))==len(seed["regions"])
            for item in seed["brands"]:
                assert resolve(db,"categories/1/marks",[item["name"]])==[item["id"]]
            for item in seed["models"]:
                assert resolve(db,f"categories/1/marks/{item['brand_id']}/models",[item["name"]])==[item["id"]]
    finally:
        engine.dispose()
