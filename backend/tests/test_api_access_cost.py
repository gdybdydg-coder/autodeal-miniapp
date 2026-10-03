"""Isolated access/cost regressions: fake provider, fake Telegram, SQLite only."""
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import billing, full_scan, purchase_stats, recent_publications as rp
from backend.auto_ria import RiaError
from backend.billing_models import AccessEvent, BillingControl, Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import (Delivery, Filters, FullScan, MonitorJob, MonitorSeen, Search,
                            SourceBudget, StarsTestOrder, User)
from backend.monitor import active_members, access_allowed_clause, reset_watch
from backend.tests.test_monitor import p, add_search, drain, wake, details, searches
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_recent_publications import setup as html_setup, offer, probe
from backend.worker import enqueue, deliver_one


def enforce(p, paid=(111,), seconds=3600):
    ManualBase.metadata.create_all(p.engine)
    with Session(p.engine) as db:
        db.merge(BillingControl(id=billing.CONTROL, sales=True, enforce=True, offer={}))
        for uid in paid:
            db.merge(Entitlement(user_id=uid, expires_at=p.clock[0]+seconds, updated_at=p.clock[0]))
        db.commit()


def expire(p, uid=111):
    with Session(p.engine) as db:
        db.get(Entitlement, uid).expires_at=p.clock[0]
        db.commit()


def arrival(p, sid="124"):
    p.ads[sid]=p.clock[0]+1
    wake(p)
    assert p.runner.tick()


def test_two_paid_one_unpaid_share_one_search_detail_quote_and_keep_filters(p, monkeypatch):
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    enforce(p, paid=(111,222))
    quotes=enable(p, monkeypatch)
    with Session(p.engine) as db:
        originals=[(s.id,s.enabled,s.filters,s.fingerprint) for s in db.scalars(select(Search))]
        assert len(active_members(db))==2
    drain(p)
    assert len(searches(p))==1
    arrival(p)
    drain(p)
    assert sorted((uid,c.source_id) for uid,c in p.sent)==[(111,"124"),(222,"124")]
    assert quotes==["124"] and len(details(p,"124"))==1
    with Session(p.engine) as db:
        assert db.get(MonitorSeen,(3,"124")) is None
        assert [(s.id,s.enabled,s.filters,s.fingerprint) for s in db.scalars(select(Search))]==originals


def test_only_unpaid_enabled_search_spends_zero_calls_and_keeps_saved_search(p):
    enforce(p, paid=())
    before=0
    drain(p)
    assert len(p.calls)==before and not p.sent
    with Session(p.engine) as db:
        assert active_members(db)==[]
        assert db.get(Search,1).enabled is True and db.get(User,111).ready is True
        assert db.scalar(select(SourceBudget.total))==2


def test_access_policy_is_bulk_and_preserves_gift_pilot_disabled_enforcement(p):
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    enforce(p)
    with Session(p.engine) as db:
        db.add(StarsTestOrder(id="synthetic-pilot",user_id=222,command_update=99,
            created_at=p.clock[0],state="paid",paid_until=p.clock[0]+100))
        db.commit()
    statements=[]
    def read(conn,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(p.engine,"before_cursor_execute",read)
    try:
        with Session(p.engine) as db:
            assert [s.user_id for s,_,_ in active_members(db)]==[111,222]
    finally:
        event.remove(p.engine,"before_cursor_execute",read)
    assert len(statements)==1
    with Session(p.engine) as db:
        for uid in (111,222,333):
            assert bool(db.scalar(select(access_allowed_clause(uid,p.clock[0]))))==billing.allowed(db,uid,p.clock[0])
        billing.control(db).enforce=False
        db.commit()
        assert len(active_members(db))==3


def test_screenshot_alone_is_unpaid_first_confirmation_updates_and_renewals_do_not_duplicate(p):
    enforce(p,paid=())
    with Session(p.engine) as db:
        db.add(PaymentRequest(id="synthetic-1",user_id=111,state="review",receipt_file_id="synthetic-image",
            amount_minor=25000,currency="UAH",days=30,created_at=p.clock[0],updated_at=p.clock[0]))
        db.commit()
        assert purchase_stats.counts(db)=={"total":1,"buyers":0,"not_purchased":1}
    drain(p)
    assert not p.calls
    # State written by owner-confirmed payment, isolated fixture only.
    with Session(p.engine) as db:
        request=db.get(PaymentRequest,"synthetic-1")
        request.state="approved"
        request.expires_at=p.clock[0]+3600
        db.add(Entitlement(user_id=111,expires_at=p.clock[0]+3600,updated_at=p.clock[0]))
        for idx in range(2,5):
            db.add(PaymentRequest(id=f"synthetic-{idx}",user_id=111,state="approved",
                amount_minor=25000,currency="UAH",days=30,created_at=p.clock[0],updated_at=p.clock[0]))
        db.commit()
        assert purchase_stats.counts(db)=={"total":1,"buyers":1,"not_purchased":0}
    drain(p)
    assert len(searches(p))==1


def test_gift_access_is_not_purchase_and_expired_purchase_stays_historical(p):
    add_search(p,sid=2,uid=222)
    enforce(p,paid=(111,))
    with Session(p.engine) as db:
        db.add(AccessEvent(id="synthetic-gift",user_id=111,actor=999,kind="gift",
            at=p.clock[0],expires_at=p.clock[0]+3600,reason="synthetic gift"))
        db.add(PaymentRequest(id="synthetic-expired-purchase",user_id=222,state="approved",
            amount_minor=25000,currency="UAH",days=30,created_at=p.clock[0]-86400,
            updated_at=p.clock[0]-86400,expires_at=p.clock[0]-1))
        db.commit()
        assert billing.allowed(db,111,p.clock[0]) and not billing.allowed(db,222,p.clock[0])
        assert purchase_stats.counts(db)=={"total":2,"buyers":1,"not_purchased":1}


def test_expired_pending_job_is_preserved_without_calls_and_can_resume_after_renewal(p,monkeypatch):
    enforce(p)
    quotes=enable(p,monkeypatch)
    drain(p)
    arrival(p)
    expire(p)
    before=len(p.calls)
    assert not p.runner.tick()
    assert len(p.calls)==before and not quotes and not p.sent
    with Session(p.engine) as db:
        job=db.get(MonitorJob,"124")
        assert job.state=="pending" and job.attempts==0
        assert db.get(Search,1).enabled
        db.get(Entitlement,111).expires_at=p.clock[0]+3600
        db.commit()
    drain(p)
    assert quotes==["124"] and [(uid,c.source_id) for uid,c in p.sent]==[(111,"124")]


@pytest.mark.parametrize("mutation",["expiry","stop"])
def test_access_change_after_detail_prevents_further_paid_quote_and_delivery(p,monkeypatch,mutation):
    enforce(p)
    quotes=enable(p,monkeypatch)
    drain(p)
    factory=p.runner.search_factory
    def source_factory(engine,key):
        source=factory(engine,key)
        fetch=source.fetch
        def get(key,path,params):
            result=fetch(key,path,params)
            if path=="info" and params["auto_id"]=="124":
                if mutation=="expiry":
                    expire(p)
                else:
                    with Session(p.engine) as db:
                        db.get(User,111).ready=False
                        db.get(Search,1).enabled=False
                        reset_watch(db,1,False)
                        db.commit()
            return result
        source.fetch=get
        return source
    p.runner.search_factory=source_factory
    arrival(p)
    assert p.runner.tick()
    p.runner.deliver_tick()
    assert len(details(p,"124"))==1 and not quotes and not p.sent
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)) is None


def test_same_client_two_compatible_searches_has_one_delivery(p,monkeypatch):
    add_search(p,sid=2,uid=111,minDiscount=20)
    enforce(p)
    quotes=enable(p,monkeypatch)
    drain(p)
    arrival(p)
    drain(p)
    assert quotes==["124"] and len(details(p,"124"))==1
    assert [(uid,c.source_id) for uid,c in p.sent]==[(111,"124")]


def test_expired_owned_full_scan_never_constructs_source_or_loses_progress(p):
    enforce(p,paid=())
    with Session(p.engine) as db:
        sid=full_scan.start(db,111,Filters())
        db.commit()
    created=[]
    scan=full_scan.Scanner(p.engine,"synthetic",lambda *args:created.append(args))
    scan.tick()
    assert created==[]
    with Session(p.engine) as db:
        row=db.get(FullScan,sid)
        assert row.status=="queued" and row.context=={} and row.requests==0


def test_full_scan_expiry_after_first_page_blocks_next_info_request(p):
    enforce(p)
    with Session(p.engine) as db:
        sid=full_scan.start(db,111,Filters())
        db.commit()
    factory=p.runner.search_factory
    calls=[]
    def source_factory(engine,key):
        source=factory(engine,key)
        def fetch(key,path,params):
            calls.append(path)
            if path=="search":
                expire(p)
                return {"result":{"search_result":{"ids":["124"],"count":1}}}
            raise AssertionError("expired scan spent an info/valuation call")
        source.fetch=fetch
        return source
    full_scan.Scanner(p.engine,"synthetic",source_factory).tick()
    assert calls==["search"]
    with Session(p.engine) as db:
        row=db.get(FullScan,sid)
        assert row.requests==1 and row.context["page"]==1 and row.checked==0


def test_html_candidate_with_only_unpaid_interests_spends_no_api_detail(p,monkeypatch):
    quotes=html_setup(p,monkeypatch)
    enforce(p,paid=())
    offer(p)
    before=len(p.calls)
    drain(p)
    assert len(p.calls)==before and not quotes and not p.sent and not details(p,"77")


def test_info_endpoint_404_hold_survives_sixty_ticks_and_new_subscriber(p,monkeypatch):
    enforce(p)
    quotes=enable(p,monkeypatch)
    drain(p)
    factory=p.runner.search_factory
    attempts=[]
    def source_factory(engine,key):
        source=factory(engine,key)
        fetch=source.fetch
        def get(key,path,params):
            if path=="info" and params["auto_id"]=="124":
                attempts.append(params["auto_id"])
                raise RiaError("info_endpoint_unavailable")
            return fetch(key,path,params)
        source.fetch=get
        return source
    p.runner.search_factory=source_factory
    arrival(p)
    assert p.runner.tick()
    with Session(p.engine) as db:
        job=db.get(MonitorJob,"124")
        original=dict(job.result)
        assert job.state=="manual_review" and job.reason=="info_endpoint_unavailable"
    add_search(p,sid=2,uid=222)
    enforce(p,paid=(111,222))
    for _ in range(60):
        wake(p)
        drain(p)
    assert attempts==["124"] and not quotes and not p.sent
    with Session(p.engine) as db:
        job=db.get(MonitorJob,"124")
        assert job.state=="manual_review" and job.reason=="info_endpoint_unavailable"
        assert job.result["discovery_kind"]==original["discovery_kind"]
        assert job.attempts==1


def test_stale_delivery_refresh_cannot_reopen_manual_review_or_drop_saved_evidence(p,monkeypatch):
    enforce(p)
    enable(p,monkeypatch)
    drain(p)
    arrival(p)
    assert p.runner.tick()  # Evaluate but deliberately do not run delivery.
    enqueue(p.engine,now=p.clock[0],require_provider_range=True)
    preserved={"discovery_kind":"new_publication","saved_card_marker":"synthetic"}
    with Session(p.engine) as db:
        job=db.get(MonitorJob,"124")
        job.state,job.reason,job.result="manual_review","info_endpoint_unavailable",preserved
        db.commit()
    p.clock[0]+=400
    calls=[]
    assert deliver_one(p.engine,p.settings,lambda *args:calls.append(args),now=p.clock[0])=="pending"
    assert calls==[]
    with Session(p.engine) as db:
        job=db.get(MonitorJob,"124")
        delivery=db.scalar(select(Delivery))
        assert job.state=="manual_review" and job.reason=="info_endpoint_unavailable"
        assert job.result==preserved and delivery.retry_at==p.clock[0]+60
