import copy
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend import bot_commands, quota_management as qm, active_window
from backend.app import Settings, create_app
from backend.auto_ria import RiaError
from backend.models import (Delivery, MonitorFeed, MonitorJob, MonitorMembership,
                            MonitorWatch, SourceBudget, SourceProbe, User)
from backend.manual_payment_models import ManualBase
from backend.ria_budget import BudgetLimits, TOTAL_OVERRIDE_ID, total_cap
from backend.ria_search import RiaSearch, budget_state, budget_usage, quota_status
from backend.tests.test_backend import TOKEN, SECRET, command


@pytest.fixture
def q(tmp_path, monkeypatch):
    for name, value in [("HOURLY",900), ("DAILY",12000), ("TOTAL",90000)]:
        monkeypatch.setenv("RIA_REQUESTS_"+name+"_CAP", str(value))
    async def idle(engine, settings, stop):
        await stop.wait()
    monkeypatch.setattr(bot_commands, "run", idle)
    engine = create_engine("sqlite:///"+str(tmp_path/"quota.db"), connect_args={"check_same_thread":False})
    # /stats reads purchase history even when this fixture disables sales.
    ManualBase.metadata.create_all(engine)
    settings = Settings("unused", TOKEN, SECRET, True, True, admin_telegram_id=111,
                        ria_quota_management_enabled=True)
    with TestClient(create_app(settings, engine)) as api:
        with Session(engine) as db:
            budget = db.get(SourceBudget,"auto_ria")
            budget.total, budget.calls = 80000, [time.time()-5000]*100
            db.add(User(id=111, ready=True))
            db.commit()
        yield engine, settings, api
    engine.dispose()


def draft(q, remaining, update=10):
    engine, _, api = q
    assert command(api, f"/quota_set {remaining}", update=update).status_code == 200
    with Session(engine) as db:
        rows = list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.DRAFT))
                              .order_by(SourceProbe.checked_at.desc())))
        return rows[0].id.removeprefix(qm.DRAFT)


def confirm(q, nonce, update=20):
    return command(q[2], f"/quota_confirm {nonce}", update=update)


def reply(engine, update):
    with Session(engine) as db:
        return db.get(SourceProbe, qm.OUTBOX+str(update)).result["text"]


def test_confirmation_replaces_remaining_preserves_counters_and_spending_in_flight(q):
    engine, _, api = q
    nonce = draft(q, 1000000)
    with Session(engine) as db:
        budget = db.get(SourceBudget,"auto_ria")
        assert total_cap(db) == 90000
        budget.total += 7
        budget.calls = [*budget.calls, *([time.time()]*7)]
        budget.blocked_until = time.time()+600
        saved_calls, blocked = copy.deepcopy(budget.calls), budget.blocked_until
        db.commit()
    assert confirm(q, nonce).status_code == 200
    with Session(engine) as db:
        row = db.get(SourceBudget,"auto_ria")
        assert row.total == 80007 and row.calls == saved_calls and row.blocked_until == blocked
        assert total_cap(db) == 1080000
        assert qm.snapshot(db)["remaining"] == 999993
        assert qm.snapshot(db)["gate"]["reason"] == "upstream"
        assert "999 993" in bot_commands.stats_text(db,111,111)
    usage = budget_usage(engine)
    assert usage["limits"] == {"hourly":900,"daily":12000,"total":1080000}
    assert usage["remaining"]["total"] == 999993
    # Replayed webhook and a new update using the same code cannot add again.
    confirm(q, nonce)
    confirm(q, nonce, update=21)
    with Session(engine) as db:
        assert total_cap(db) == 1080000
    assert "вже використане" in reply(engine,21)


def test_duplicate_draft_update_creates_only_one_request(q):
    first = draft(q,1000000)
    second = draft(q,1000000)
    assert first == second
    with Session(q[0]) as db:
        assert len(list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.DRAFT))))) == 1


def test_newer_confirmation_invalidates_an_older_draft(q):
    old = draft(q,1000000)
    new = draft(q,2000000,update=11)
    confirm(q,new)
    confirm(q,old,update=21)
    with Session(q[0]) as db:
        assert total_cap(db) == 2080000
    assert "бюджет уже змінено" in reply(q[0],21)


@pytest.mark.parametrize("value", ["-1","1.5","1e6","100_000","100000001","１００００００","1000000 extra"])
def test_invalid_amounts_do_not_modify_budget(q,value):
    assert command(q[2],f"/quota_set {value}",update=10).status_code == 200
    with Session(q[0]) as db:
        assert db.get(SourceProbe,TOTAL_OVERRIDE_ID) is None
        assert not list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.DRAFT))))


@pytest.mark.parametrize("action",["cancel","expired","env_changed"])
def test_cancel_expiry_and_operator_edit_keep_budget_unchanged(q,monkeypatch,action):
    nonce=draft(q,1000000)
    if action=="cancel":
        command(q[2],f"/quota_cancel {nonce}",update=11)
    elif action=="expired":
        with Session(q[0]) as db:
            db.get(SourceProbe,qm.DRAFT+nonce).checked_at-=301
            db.commit()
    else:
        monkeypatch.setenv("RIA_REQUESTS_TOTAL_CAP","100000")
    confirm(q,nonce)
    with Session(q[0]) as db:
        assert db.get(SourceProbe,TOTAL_OVERRIDE_ID) is None


def test_zero_remaining_is_allowed_without_disabling_subscriptions_or_resetting_usage(q):
    nonce=draft(q,0)
    confirm(q,nonce)
    assert quota_status(q[0])["reason"] == "total"
    with Session(q[0]) as db:
        assert db.get(SourceBudget,"auto_ria").total == 80000
        assert db.get(User,111).ready


def test_override_survives_sessions_and_total_env_edit_supersedes_it(q,monkeypatch):
    confirm(q,draft(q,1000000))
    q[0].dispose()
    assert budget_usage(q[0])["remaining"]["total"] == 1000000
    monkeypatch.setenv("RIA_REQUESTS_HOURLY_CAP","1000")
    assert budget_usage(q[0])["remaining"]["total"] == 1000000
    monkeypatch.setenv("RIA_REQUESTS_TOTAL_CAP","200000")
    assert budget_usage(q[0])["remaining"]["total"] == 120000


@pytest.mark.parametrize("kind",["other_user","group","forged_chat","bot","wrong_secret","wrong_mention","stale"])
def test_owner_private_fresh_authenticated_commands_only(q,kind):
    engine,_,api=q
    msg={"from":{"id":111},"chat":{"id":111,"type":"private"},
         "text":"/quota_set 1000000","date":int(time.time())}
    secret=SECRET
    if kind=="other_user":msg["from"]["id"]=msg["chat"]["id"]=222
    if kind=="group":msg["chat"]["type"]="group"
    if kind=="forged_chat":msg["chat"]["id"]=222
    if kind=="bot":msg["from"]["is_bot"]=True
    if kind=="wrong_secret":secret="wrong"
    if kind=="wrong_mention":msg["text"]="/quota_set@another_bot 1000000"
    if kind=="stale":msg["date"]-=301
    r=api.post("/telegram/webhook",headers={"X-Telegram-Bot-Api-Secret-Token":secret},
               json={"update_id":10,"message":msg})
    assert r.status_code == (403 if kind=="wrong_secret" else 200)
    with Session(engine) as db:
        assert not list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.DRAFT))))
        assert db.get(SourceBudget,"auto_ria").total==80000


def test_admin_commands_do_not_mask_an_earlier_stop_arriving_late(q):
    now=int(time.time())
    command(q[2],"/quota",update=11,date=now)
    command(q[2],"/stop",update=10,date=now-1)
    with Session(q[0]) as db:
        assert db.get(User,111).ready is False


def test_status_reads_and_drafts_never_issue_provider_requests(q,monkeypatch):
    monkeypatch.setattr(RiaSearch,"request",lambda *a,**kw:pytest.fail("source call"))
    before=budget_usage(q[0])
    command(q[2],"/quota",update=9)
    draft(q,1000000)
    assert budget_usage(q[0])==before
    assert "не онлайн-баланс" in reply(q[0],9)


def test_actual_requests_stop_at_confirmed_ceiling_even_on_existing_client(q):
    engine,_,_=q
    calls=[]
    source=RiaSearch(engine,"fake",fetch=lambda key,path,params:calls.append(path) or {})
    source.acquire()
    try:
        confirm(q,draft(q,2))
        source.request("test",{},lambda d:d,force=True)
        source.request("test",{},lambda d:d,force=True)
        with pytest.raises(RiaError,match="quota_exceeded"):
            source.request("test",{},lambda d:d,force=True)
    finally:source.release()
    assert len(calls)==2 and quota_status(engine)["reason"]=="total"


@pytest.mark.parametrize("gate",["hourly","daily","upstream"])
def test_total_topup_does_not_bypass_other_gates(q,gate):
    engine,_,_=q
    nonce=draft(q,1000000)
    with Session(engine) as db:
        row=db.get(SourceBudget,"auto_ria")
        if gate=="hourly":row.calls=[time.time()]*900
        if gate=="daily":row.calls=[time.time()-4000]*12000
        if gate=="upstream":row.blocked_until=time.time()+3600
        db.commit()
    confirm(q,nonce)
    assert quota_status(engine)["reason"]==gate
    source=RiaSearch(engine,"fake",fetch=lambda *a:pytest.fail("blocked request"))
    source.acquire()
    try:
        with pytest.raises(RiaError,match="quota_exceeded"):source.request("test",{},lambda d:d)
    finally:source.release()


def test_active_window_reservation_uses_same_remaining_as_primary(q):
    confirm(q,draft(q,31))
    with Session(q[0]) as db:
        assert active_window.budget_available(db,reserve=1)
        assert not active_window.budget_available(db,reserve=32)


def set_remaining(q,remaining,update=10):
    nonce=draft(q,remaining,update=update)
    confirm(q,nonce,update=update+1)
    with Session(q[0]) as db:
        # Ignore user-requested response deliveries in warning-only assertions.
        for row in db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.OUTBOX))):row.status="sent"
        db.commit()


def warning_rows(engine):
    with Session(engine) as db:
        return [row.result for row in db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.OUTBOX+"warning-")))]


def test_warning_thresholds_are_once_per_level_across_restarts(q):
    engine,settings,_=q
    set_remaining(q,10001)
    qm.check_warning(engine,settings)
    assert not warning_rows(engine)
    for spent,expected in [(1,1),(9001,2),(10001,3)]:
        with Session(engine) as db:
            db.get(SourceBudget,"auto_ria").total=80000+spent
            db.get(SourceProbe,qm.ALERT_STATE).checked_at-=61
            db.commit()
        qm.check_warning(engine,settings)
        engine.dispose()
        qm.check_warning(engine,settings)
        assert len(warning_rows(engine))==expected
    with Session(engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==90001


def test_warnings_use_three_day_and_one_day_pace(q):
    for remaining,level in [(60001,0),(60000,1),(20000,2),(0,3)]:
        assert qm.warning_level({"remaining":remaining,"used_day":20000})==level


@pytest.mark.parametrize("change",["stop","topup","owner_changed"])
def test_pending_warning_is_cancelled_after_relevant_state_changes(q,change):
    engine,settings,_=q
    set_remaining(q,1000)
    qm.check_warning(engine,settings)
    if change=="stop":
        with Session(engine) as db:
            db.get(User,111).ready=False;db.commit()
    elif change=="topup":confirm(q,draft(q,1000000,update=30),update=31)
    else:settings=replace(settings,admin_telegram_id=222)
    assert qm.deliver_one(engine,settings,lambda *a:pytest.fail("obsolete alert"))=="cancelled"


@pytest.mark.parametrize("response,expected",[({"ok":True,"result":{"message_id":5}},"sent"),
    ({"uncertain":True},"uncertain"),({"ok":False,"error_code":403},"failed")])
def test_replies_are_claimed_once_and_target_only_owner(q,response,expected):
    engine,settings,api=q
    command(api,"/quota",update=10)
    calls=[]
    def send(token,method,payload):
        assert payload["chat_id"]==111 and method=="sendMessage"
        calls.append(payload)
        return response
    assert qm.deliver_one(engine,settings,send)==expected
    assert qm.deliver_one(engine,settings,send)=="idle"
    assert len(calls)==1


def test_explicit_telegram_rate_limit_is_retried_but_never_unknown_delivery(q):
    engine,settings,api=q
    command(api,"/quota",update=10)
    assert qm.deliver_one(engine,settings,lambda *a:{"ok":False,"error_code":429,
        "parameters":{"retry_after":2}})=="pending"
    assert qm.deliver_one(engine,settings,lambda *a:pytest.fail("too soon"))=="idle"
    with Session(engine) as db:
        db.get(SourceProbe,qm.OUTBOX+"10").checked_at-=3;db.commit()
    assert qm.deliver_one(engine,settings,lambda *a:(_ for _ in ()).throw(TimeoutError()))=="uncertain"
    assert qm.deliver_one(engine,settings,lambda *a:pytest.fail("unknown retry"))=="idle"


def test_admin_menu_is_scoped_and_idempotent(q):
    engine,settings,_=q
    settings=replace(settings,configure_webhook=True)
    with Session(engine) as db:
        db.merge(SourceProbe(id="telegram-webhook-v1",status="configured",checked_at=time.time(),result={}))
        db.commit()
    calls=[]
    def request(token,method,payload):
        calls.append((method,payload));return {"ok":True,"result":True}
    qm.configure(engine,settings,request)
    qm.configure(engine,settings,request)
    assert len(calls)==1 and calls[0][0]=="setMyCommands"
    assert calls[0][1]["scope"]=={"type":"chat","chat_id":111}
    assert any(c["command"]=="quota" for c in calls[0][1]["commands"])


def test_public_status_contains_no_admin_identity_or_confirmation_code(q):
    nonce=draft(q,1000000)
    confirm(q,nonce)
    public=qm.public_status(q[0],q[1])
    assert public["enabled"] and public["balance_source"]=="owner_confirmed_snapshot"
    assert not public["provider_balance_read_automatically"]
    assert nonce not in str(public) and "111" not in str(public)


def test_commands_when_feature_disabled_or_owner_unconfigured_do_nothing(q):
    with Session(q[0]) as db:
        for settings in [replace(q[1],ria_quota_management_enabled=False),replace(q[1],admin_telegram_id=0)]:
            qm.handle(db,settings,111,"/quota_set","/quota_set 1000000",100,int(time.time()))
        db.commit()
        assert not list(db.scalars(select(SourceProbe).where(SourceProbe.id.startswith(qm.DRAFT))))


def test_next_package_can_resume_waiting_work_without_resets(q):
    from backend.monitor import Monitor, initialize
    engine,settings,_=q
    initialize(engine)
    set_remaining(q,0)
    with Session(engine) as db:
        db.add(MonitorFeed(id="group",filters={},started_at=1,cursor=50,context={"saved":"page"},
                           status="quota_exceeded",next_poll=time.time()+3600))
        db.add(MonitorJob(source_id="123",first_seen=1,state="pending",reason="quota_exceeded",next_run=time.time()+3600))
        db.add(MonitorJob(source_id="124",first_seen=1,state="pending",reason="connection_error",next_run=time.time()+3600))
        db.add(MonitorWatch(search_id=1,epoch="untouched"))
        db.add(Delivery(user_id=111,listing_id=1,state="uncertain"))
        db.get(User,111).ready=False
        db.commit()
    before=budget_usage(engine)["used"]
    confirm(q,draft(q,1000000,update=30),update=31)
    runner=Monitor(engine,settings)
    assert runner.claim()
    try:
        with Session(engine) as db:runner.resume_available_quota(db,{"group"})
    finally:runner.release("idle")
    with Session(engine) as db:
        feed=db.get(MonitorFeed,"group")
        assert feed.next_poll<=time.time() and feed.cursor==50 and feed.context=={"saved":"page"}
        assert db.get(MonitorJob,"123").next_run<=time.time()
        assert db.get(MonitorJob,"124").next_run>time.time()
        assert db.get(MonitorWatch,1).epoch=="untouched"
        assert db.scalar(select(Delivery)).state=="uncertain"
        assert not db.get(User,111).ready
    assert budget_usage(engine)["used"]==before


def test_failed_menu_configuration_does_not_block_startup(q):
    engine,settings,_=q
    with Session(engine) as db:
        db.merge(SourceProbe(id="telegram-webhook-v1",status="configured",checked_at=time.time(),result={}))
        db.commit()
    qm.configure(engine,replace(settings,configure_webhook=True),lambda *a:(_ for _ in ()).throw(TimeoutError()))
    assert not qm.public_status(engine,settings)["menu_configured"]
