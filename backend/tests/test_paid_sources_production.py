"""Production factory: real manual purchase required, fixture transports only."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import app as application, full_scan, paid_source_access
from backend.app import create_app
from backend.auto_ria import RiaError
from backend.billing_models import BillingControl, Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import Filters, FullScan, MonitorJob, Range, Search, SourceBudget, StarsTestOrder, User
from backend.monitor import active_members
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_recent_publications import setup as html_setup, offer
from backend.tests.test_backend import setup, headers
from backend.tests.test_ria_search import fixture_fetch
from backend.ria_search import RiaSearch


def strict(p):
    ManualBase.metadata.create_all(p.engine)
    p.engine.update_execution_options(**{paid_source_access.OPTION: True})


def approve(p, uid=111, *, state="approved", purchase_until=None, access_until=None):
    now = p.clock[0]
    with Session(p.engine) as db:
        db.merge(PaymentRequest(id=f"fixture-{uid}", user_id=uid, state=state,
            amount_minor=25000, currency="UAH", days=30, created_at=now, updated_at=now,
            expires_at=now+3600 if purchase_until is None else purchase_until))
        db.merge(Entitlement(user_id=uid, expires_at=now+3600 if access_until is None else access_until,
                             updated_at=now))
        db.commit()


def test_two_current_paid_clients_one_unpaid_only_paid_source_and_recipients(p, monkeypatch):
    strict(p)
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333, price=Range(to=21000))
    approve(p,111); approve(p,222)
    quotes=enable(p,monkeypatch)
    drain(p)
    assert sum(path=="search" for path,_ in p.calls)==1
    p.ads["124"]=p.clock[0]+1
    wake(p); drain(p)
    assert quotes==["124"]
    assert sum(path=="info" and params["auto_id"]=="124" for path,params in p.calls)==1
    assert sorted((uid,car.source_id) for uid,car in p.sent)==[(111,"124"),(222,"124")]
    with Session(p.engine) as db:
        assert db.get(Search,3).enabled
        assert len(active_members(db))==2


@pytest.mark.parametrize("kind",["none","gift","pending","expired_purchase","revoked","pilot","enforcement_off","service_account"])
def test_non_purchase_access_never_authorizes_api_or_notifications(p,kind):
    strict(p)
    if kind=="pending": approve(p,state="review")
    elif kind=="expired_purchase": approve(p,purchase_until=p.clock[0]-1)
    elif kind=="revoked": approve(p,access_until=p.clock[0]-1)
    elif kind=="gift":
        with Session(p.engine) as db:
            db.add(Entitlement(user_id=111,expires_at=p.clock[0]+3600,updated_at=p.clock[0]));db.commit()
    elif kind=="pilot":
        with Session(p.engine) as db:
            db.add(StarsTestOrder(id="fixture-pilot",user_id=111,command_update=42,created_at=p.clock[0],
                state="paid",paid_until=p.clock[0]+3600));db.commit()
    elif kind=="enforcement_off":
        with Session(p.engine) as db:
            db.add(BillingControl(id="commercial-v1",enforce=False,sales=False,offer={}));db.commit()
    elif kind=="service_account":
        approve(p)
        paid_source_access.configure(p.engine,replace(p.settings,stats_excluded_user_ids="111"))
    drain(p)
    assert not p.calls and not p.sent
    with Session(p.engine) as db:
        assert not paid_source_access.allowed(db,111,p.clock[0])
        assert db.get(Search,1).enabled


@pytest.mark.parametrize("mutation",["purchase_expiry","stop"])
def test_current_purchase_and_stop_rechecked_between_detail_and_quote(p,monkeypatch,mutation):
    strict(p); approve(p)
    quotes=enable(p,monkeypatch)
    drain(p)
    factory=p.runner.search_factory
    def source_factory(engine,key):
        source=factory(engine,key);fetch=source.fetch
        def get(key,path,params):
            result=fetch(key,path,params)
            if path=="info" and params["auto_id"]=="124":
                with Session(engine) as db:
                    if mutation=="purchase_expiry":db.get(PaymentRequest,"fixture-111").expires_at=p.clock[0]
                    else:db.get(User,111).ready=False
                    db.commit()
            return result
        source.fetch=get;return source
    p.runner.search_factory=source_factory
    p.ads["124"]=p.clock[0]+1
    wake(p); drain(p)
    assert not quotes and not p.sent
    with Session(p.engine) as db:assert db.get(MonitorJob,"124").state=="pending"


def test_paid_source_request_fail_closed_without_any_current_purchase(p):
    strict(p)
    source=p.runner.search_factory(p.engine,"fixture")
    source.acquire()
    try:
        with pytest.raises(RiaError,match="paid_access_required"):
            source.request("search",{},lambda value:value,force=True)
    finally:source.release()
    assert not p.calls
    with Session(p.engine) as db:assert db.get(SourceBudget,"auto_ria").total==2


def test_paid_full_scan_honors_stop_and_unpaid_never_constructs_provider(p):
    strict(p);approve(p)
    with Session(p.engine) as db:
        sid=full_scan.start(db,111,p.filters);db.get(User,111).ready=False;db.commit()
    constructed=[]
    scan=full_scan.Scanner(p.engine,"fixture",lambda *args:constructed.append(args))
    scan.tick()
    assert not constructed
    with Session(p.engine) as db:assert db.get(FullScan,sid).status=="queued"


def test_paid_full_scan_can_collect_and_complete_empty_page(p):
    strict(p);approve(p)
    with Session(p.engine) as db:
        sid=full_scan.start(db,111,Filters());db.commit()
    factory=p.runner.search_factory;calls=[]
    def source_factory(engine,key):
        source=factory(engine,key)
        def fetch(key,path,params):
            calls.append(path)
            assert path=="search"
            return {"result":{"search_result":{"ids":[],"count":0}}}
        source.fetch=fetch;return source
    full_scan.Scanner(p.engine,"fixture",source_factory).tick()
    assert calls==["search"]
    with Session(p.engine) as db:
        row=db.get(FullScan,sid)
        assert row.status=="completed" and row.requests==1


def test_html_publication_only_current_paid_clients_share_detail_and_quote(p,monkeypatch):
    strict(p)
    add_search(p,sid=2,uid=222)
    add_search(p,sid=3,uid=333,price=Range(to=21000))
    approve(p,111);approve(p,222)
    quotes=html_setup(p,monkeypatch)
    offer(p,sid="77");drain(p)
    assert quotes==["77"]
    assert sum(path=="info" and params["auto_id"]=="77" for path,params in p.calls)==1
    assert sorted((uid,car.source_id) for uid,car in p.sent)==[(111,"77"),(222,"77")]


def test_strict_catalog_requires_purchase_even_when_another_paid_client_exists(setup,monkeypatch):
    engine,settings,_=setup
    ManualBase.metadata.create_all(engine)
    calls=[]
    monkeypatch.setattr(application,"RiaSearch",lambda engine,key:RiaSearch(engine,key,fixture_fetch(calls)))
    with Session(engine) as db:
        db.add(User(id=222,ready=True));db.add(Entitlement(user_id=222,expires_at=9999999999,updated_at=1))
        db.add(PaymentRequest(id="fixture-other-paid",user_id=222,state="approved",amount_minor=25000,
            currency="UAH",days=30,created_at=1,updated_at=1,expires_at=9999999999))
        db.add(Entitlement(user_id=111,expires_at=9999999999,updated_at=1));db.commit()
    client=TestClient(create_app(replace(settings,auto_ria_api_key="fixture"),engine,paid_source_only=True))
    try:
        response=client.get("/api/catalog",headers=headers())
        assert response.status_code==402 and not calls
        assert client.get("/api/billing/status",headers=headers()).json()["access_available"] is False
    finally:client.close()


def test_production_factory_always_enables_strict_paid_policy(monkeypatch):
    settings=object();result=object();calls=[]
    monkeypatch.setattr(application.Settings,"env",lambda:settings)
    def create(value,**kwargs):calls.append((value,kwargs));return result
    monkeypatch.setattr(application,"create_app",create)
    assert application.factory() is result
    assert calls==[(settings,{"paid_source_only":True})]


def test_strict_startup_never_runs_paid_probes_or_validation(setup,monkeypatch):
    engine,settings,_=setup
    calls=[]
    def forbidden(*args,**kwargs):calls.append("paid startup");raise AssertionError("paid startup")
    for module,name in [(application,"probe_once"),(application,"verify_search_once"),
            (application.ria_rollout,"check_once"),(application.valuation_audit,"check_once"),
            (application.ria_ai_price,"check_once"),(application,"validate_once")]:
        monkeypatch.setattr(module,name,forbidden)
    for name in ["check_once","check_dates_once","check_vin_dates_once","check_vin_presence_once"]:
        monkeypatch.setattr(application.notification_diagnostic,name,forbidden)
    configured=replace(settings,auto_ria_api_key="fixture",delivery_enabled=False,
        ria_ai_price_enabled=True,auto_ria_user_id="42",ria_ai_price_probe_id="124",
        ria_diagnostic_listing_id="124",catalog_rollout_check=True,ria_validation_run_id="fixture")
    with TestClient(create_app(configured,engine,paid_source_only=True)) as client:
        assert client.get("/health").status_code==200
    assert not calls
