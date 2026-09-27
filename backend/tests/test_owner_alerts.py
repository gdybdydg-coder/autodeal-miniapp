"""No real Telegram requests: exercise incident lifecycle and durable transport."""
import asyncio
import copy
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend import owner_alerts as oa, quota_management as qm
from backend.app import Settings
from backend.models import (Base, Delivery, DeliveryTiming, MonitorControl, MonitorFeed,
    MonitorJob, MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search,
    SourceBudget, SourceProbe, User)


@pytest.fixture
def h(tmp_path, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr(oa.time, "time", lambda: clock[0])
    for k, v in [("HOURLY",4500),("DAILY",90000),("TOTAL",1102160)]:
        monkeypatch.setenv("RIA_REQUESTS_"+k+"_CAP",str(v))
    engine = create_engine("sqlite:///"+str(tmp_path/"health.db"))
    Base.metadata.create_all(engine)
    settings = Settings("unused","fake-test-token","fake-secret-for-tests-at-least-32-chars",True,True,
        monitor_enabled=True, owner_alerts_enabled=True, admin_telegram_id=111,
        ria_quota_management_enabled=True, ria_ai_price_enabled=True)
    with Session(engine) as db:
        db.add_all([User(id=111,ready=True),User(id=222,ready=True),
            SourceBudget(id="auto_ria",total=130000,calls=[]),
            MonitorControl(id="pilot",heartbeat=clock[0]),
            Search(id=1,user_id=222,name="x",fingerprint="f",filters={},enabled=True),
            MonitorWatch(search_id=1,epoch="a"),
            MonitorMembership(search_id=1,epoch="a",feed_id="f",started_at=clock[0]-10000),
            MonitorFeed(id="f",filters={},started_at=clock[0]-10000,cursor=clock[0],checked_at=clock[0],status="watching")])
        db.commit()
    yield engine, settings, clock
    engine.dispose()


def sample(h, seconds=60, *, heartbeat=True, cursor=True):
    engine, settings, clock = h
    clock[0] += seconds
    with Session(engine) as db:
        if heartbeat: db.get(MonitorControl,"pilot").heartbeat=clock[0]
        if cursor: db.get(MonitorFeed,"f").cursor=clock[0]
        db.commit()
    oa.check(engine,settings)


def rows(h):
    with Session(h[0]) as db:
        return list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(oa.OUTBOX))))


def signal(h,key):
    with Session(h[0]) as db:
        return oa.observations(db,h[1],h[2][0])[0][key][0]


def delivery(h, ident=1, state="pending", user=222, *, age=700, sent_age=None):
    with Session(h[0]) as db:
        db.add(Delivery(id=ident,user_id=user,listing_id=ident,state=state))
        db.add(DeliveryTiming(delivery_id=ident,queued_at=h[2][0]-age,
            send_started_at=h[2][0]-sent_age if sent_age is not None else None))
        db.add(MonitorMatch(search_id=1,listing_id=ident,epoch="a",fingerprint="f"))
        db.commit()


def incident(h):
    delivery(h)
    oa.check(h[0],h[1])
    sample(h); sample(h)
    assert len(rows(h))==1


def accepted(*args):
    assert args[1]=="sendMessage" and args[2]["chat_id"]==111
    return {"ok":True,"result":{"message_id":42}}


def test_one_alert_then_recovery_only_after_sustained_health(h):
    incident(h)
    assert oa.deliver_one(h[0],h[1],accepted)=="sent"
    for _ in range(4): sample(h)
    assert len(rows(h))==1  # repeated checks/restarts share durable episode state
    with Session(h[0]) as db:
        db.get(Delivery,1).state="sent"; db.commit()
    sample(h); sample(h)
    assert len(rows(h))==1
    sample(h)
    assert len(rows(h))==2
    assert oa.deliver_one(h[0],h[1],accepted)=="sent"
    assert "стан відновився" in rows(h)[1].result["text"]
    with Session(h[0]) as db:
        db.get(Delivery,1).state="pending"; db.commit()
    sample(h);sample(h);sample(h)
    assert len(rows(h))==3
    assert len({r.id for r in rows(h)})==3


def test_short_issue_and_gap_do_not_trigger(h):
    delivery(h)
    oa.check(h[0],h[1]); sample(h)
    with Session(h[0]) as db:
        db.get(Delivery,1).state="cancelled"; db.commit()
    sample(h)
    assert rows(h)==[]
    with Session(h[0]) as db:
        db.get(Delivery,1).state="pending"; db.commit()
    sample(h); sample(h,seconds=600)
    assert rows(h)==[]  # unobserved time is not evidence of continuous failure
    sample(h);sample(h)
    assert len(rows(h))==1


def test_resolved_unsent_alert_is_cancelled_without_recovery(h):
    incident(h)
    with Session(h[0]) as db:
        db.get(Delivery,1).state="cancelled"; db.commit()
    assert oa.deliver_one(h[0],h[1],accepted)=="cancelled"
    for _ in range(3): sample(h)
    assert len(rows(h))==1


@pytest.mark.parametrize("action",["owner_stop","subscriber_stop","disable_search","epoch_change","filter_change"])
def test_stop_and_current_subscription_guards(h,action):
    incident(h)
    with Session(h[0]) as db:
        if action=="owner_stop": db.get(User,111).ready=False
        if action=="subscriber_stop": db.get(User,222).ready=False
        if action=="disable_search": db.get(Search,1).enabled=False
        if action=="epoch_change": db.get(MonitorWatch,1).epoch="b"
        if action=="filter_change": db.get(Search,1).fingerprint="other"
        db.commit()
    assert oa.deliver_one(h[0],h[1],accepted)=="cancelled"
    sample(h)
    assert len(rows(h))==1


def test_no_activity_is_healthy_not_a_stall(h):
    for _ in range(15): sample(h)
    assert rows(h)==[]
    assert oa.public_status(h[0],h[1])["monitoring"] is True


def test_stale_monitor_and_delayed_discovery_are_separate(h):
    sample(h,seconds=181,heartbeat=False)
    assert signal(h,"monitor") is True
    assert signal(h,"discovery") is None
    sample(h,heartbeat=False);sample(h,heartbeat=False)
    assert [r.result["kind"] for r in rows(h)]==["monitor"]
    sample(h)
    assert signal(h,"monitor") is False
    sample(h,seconds=601,cursor=False)
    assert signal(h,"discovery") is True


def test_missing_feed_only_alerts_after_first_activation_grace(h):
    with Session(h[0]) as db:
        db.delete(db.get(MonitorFeed,"f"));db.get(MonitorMembership,1).started_at=h[2][0];db.commit()
    assert signal(h,"discovery") is False
    h[2][0]+=601
    with Session(h[0]) as db:
        db.get(MonitorControl,"pilot").heartbeat=h[2][0];db.commit()
    assert signal(h,"discovery") is True


def test_supplemental_backlog_is_not_fresh_queue_failure(h):
    with Session(h[0]) as db:
        db.add_all([MonitorJob(source_id="a",state="pending",first_seen=h[2][0]-5000,
            result={"discovery_kind":"active_window"}),
            MonitorSeen(search_id=1,source_id="a",epoch="a",state="pending",first_seen=h[2][0]-5000)])
        db.commit()
    assert signal(h,"valuation_queue") is False
    with Session(h[0]) as db:
        db.get(MonitorJob,"a").result={"discovery_kind":"publications"};db.commit()
    assert signal(h,"valuation_queue") is True
    with Session(h[0]) as db:
        db.get(MonitorSeen,(1,"a")).epoch="old";db.commit()
    assert signal(h,"valuation_queue") is False


@pytest.mark.parametrize("gate",["hourly","daily","upstream","total"])
def test_budget_pauses_do_not_masquerade_as_source_queue_failure(h,gate):
    with Session(h[0]) as db:
        b=db.get(SourceBudget,"auto_ria")
        if gate=="hourly": b.calls=[h[2][0]]*4500
        if gate=="daily": b.calls=[h[2][0]-4000]*90000
        if gate=="upstream": b.blocked_until=h[2][0]+3600
        if gate=="total": b.total=1102160
        db.commit()
    assert signal(h,"source_pause") is (None if gate=="total" else True)
    assert signal(h,"discovery") is None
    assert signal(h,"valuation_queue") is None


def test_telegram_incident_needs_multiple_users_and_real_recovery_evidence(h):
    for i in range(1,4): delivery(h,i,"failed",222,sent_age=1)
    assert signal(h,"telegram") is None  # one client's problem is not a system outage
    with Session(h[0]) as db:
        db.get(Delivery,3).user_id=111;db.commit()
    assert signal(h,"telegram") is True
    h[2][0]+=601
    assert signal(h,"telegram") is None  # silence is not recovery
    for i in range(4,7): delivery(h,i,"sent",222,sent_age=1)
    assert signal(h,"telegram") is False


@pytest.mark.parametrize("response,expected",[
    ({"ok":False,"error_code":403},"failed"),
    ({"ok":True},"uncertain"),
    ({},"uncertain"),
    (RuntimeError("private details"),"uncertain")])
def test_uncertain_and_rejected_sends_are_never_replayed(h,response,expected):
    incident(h)
    calls=[]
    def send(*args):
        calls.append(args)
        if isinstance(response,Exception):raise response
        return response
    assert oa.deliver_one(h[0],h[1],send)==expected
    assert oa.deliver_one(h[0],h[1],send)=="idle"
    assert len(calls)==1


def test_429_uses_bounded_retry_and_claim_is_persisted_before_http(h):
    incident(h)
    def send(*args):
        with Session(h[0]) as db:
            assert db.get(SourceProbe,rows(h)[0].id).status=="sending"
        return {"ok":False,"error_code":429,"parameters":{"retry_after":3}}
    assert oa.deliver_one(h[0],h[1],send)=="pending"
    assert oa.deliver_one(h[0],h[1],accepted)=="idle"
    h[2][0]+=4
    assert oa.deliver_one(h[0],h[1],accepted)=="sent"


def test_stale_and_previously_claimed_events_never_replay(h):
    incident(h)
    h[2][0]+=301
    assert oa.deliver_one(h[0],h[1],accepted)=="cancelled"
    with Session(h[0]) as db:
        db.get(SourceProbe,rows(h)[0].id).status="sending";db.commit()
    assert oa.deliver_one(h[0],h[1],accepted)=="idle"


def test_feature_gates_and_outboxes_are_independent(h):
    incident(h)
    with Session(h[0]) as db:
        qm.respond(db,111,"123","quota",h[2][0]);db.commit()
    off=replace(h[1],owner_alerts_enabled=False)
    assert oa.deliver_one(h[0],off,accepted)=="disabled"
    assert qm.deliver_one(h[0],h[1],accepted)=="sent"
    assert rows(h)[0].status=="pending"
    quota_off=replace(h[1],ria_quota_management_enabled=False)
    assert oa.deliver_one(h[0],quota_off,accepted)=="sent"


def test_observation_never_changes_budget_subscriptions_or_claims(h):
    incident(h)
    with Session(h[0]) as db:
        budget=copy.deepcopy(db.get(SourceBudget,"auto_ria").__dict__)
    oa.deliver_one(h[0],h[1],accepted)
    with Session(h[0]) as db:
        assert db.get(SourceBudget,"auto_ria").total==budget["total"]
        assert db.get(SourceBudget,"auto_ria").calls==budget["calls"]
        assert db.get(User,222).ready and db.get(Search,1).enabled
        assert db.get(MonitorWatch,1).epoch=="a"
        assert db.get(Delivery,1).state=="pending"
    public=oa.public_status(h[0],h[1])
    assert set(public)=={"enabled","owner_configured","checked_at","monitoring","active_incidents","external_uptime_monitor"}
    assert public["active_incidents"]==["delivery_queue"]


def test_run_grace_and_error_do_not_stop_other_work(h,monkeypatch):
    async def scenario():
        stop=asyncio.Event()
        calls=[]
        monkeypatch.setattr(oa,"STARTUP_GRACE",0)
        def fail(*a):calls.append("check");raise RuntimeError("db unavailable")
        monkeypatch.setattr(oa,"check",fail)
        task=asyncio.create_task(oa.run(h[0],h[1],stop))
        for _ in range(100):
            if calls:break
            await asyncio.sleep(.001)
        assert calls==["check"]
        stop.set();await asyncio.wait_for(task,1)
    asyncio.run(scenario())


def test_pending_recovery_is_cancelled_if_problem_returns(h):
    incident(h); oa.deliver_one(h[0],h[1],accepted)
    with Session(h[0]) as db:
        db.get(Delivery,1).state="sent";db.commit()
    for _ in range(3):sample(h)
    assert len(rows(h))==2
    with Session(h[0]) as db:
        db.get(Delivery,1).state="pending";db.commit()
    assert oa.deliver_one(h[0],h[1],accepted)=="cancelled"


def test_budget_pause_does_not_falsely_recover_discovery(h):
    for _ in range(3):sample(h,seconds=60 if _ else 601,cursor=False)
    assert [r.result["kind"] for r in rows(h)]==["discovery"]
    assert oa.deliver_one(h[0],h[1],accepted)=="sent"
    with Session(h[0]) as db:
        db.get(SourceBudget,"auto_ria").blocked_until=h[2][0]+1000;db.commit()
    for _ in range(4):sample(h,cursor=False)
    with Session(h[0]) as db:
        assert db.get(SourceProbe,oa.STATE).result["items"]["discovery"]["active"] is True
    assert not any(r.result.get("recovered") for r in rows(h))


def test_no_owner_or_not_live_never_sends(h):
    incident(h)
    for settings in [replace(h[1],admin_telegram_id=0),replace(h[1],delivery_enabled=False)]:
        assert oa.deliver_one(h[0],settings,accepted)=="disabled"
    with Session(h[0]) as db:
        db.delete(db.get(User,111));db.commit()
    assert oa.deliver_one(h[0],h[1],accepted)=="cancelled"


def test_application_starts_independent_alert_task_and_exposes_safe_state(h,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    calls=[]
    async def run(engine,settings,stop):
        calls.append("started");await stop.wait();calls.append("stopped")
    monkeypatch.setattr(oa,"run",run)
    settings=replace(h[1],monitor_enabled=False,ria_ai_price_enabled=False,ria_quota_management_enabled=False)
    with TestClient(create_app(settings,h[0])) as client:
        status=client.get('/api/source-status').json()['budget']['owner_alerts']
        assert status['enabled'] is True and status['owner_configured'] is True
        assert calls==['started']
    assert calls==['started','stopped']


def test_env_flag_is_explicit_opt_in(monkeypatch):
    for key in ['DATABASE_URL','TELEGRAM_BOT_TOKEN','TELEGRAM_WEBHOOK_SECRET']:
        monkeypatch.setenv(key,'test-only')
    monkeypatch.delenv('OWNER_ALERTS_ENABLED',raising=False)
    assert Settings.env().owner_alerts_enabled is False
    monkeypatch.setenv('OWNER_ALERTS_ENABLED','true')
    assert Settings.env().owner_alerts_enabled is True
