import copy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend import subscription_preview as preview, telegram_setup
from backend.app import Settings, create_app
from backend.models import (Base, SubscriptionPreview, StarsTestOrder, User, Search,
                            MonitorWatch, MonitorSeen, Delivery, SourceBudget, SourceProbe)
from backend.tests.test_backend import TOKEN, SECRET, command, subscribe, headers


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///"+str(tmp_path/"preview.db"), connect_args={"check_same_thread": False})
    settings = Settings("unused", TOKEN, SECRET, True, True)
    calls = []
    def request(token, method, payload, **kwargs):
        calls.append((method, copy.deepcopy(payload)))
        return {"ok": True, "result": True}
    monkeypatch.setattr(telegram_setup, "call", request)
    with TestClient(create_app(settings, engine)) as api:
        yield engine, settings, api, calls
    engine.dispose()


def button(api, data, update, uid=preview.OWNER, **extras):
    event = {"update_id": update, "callback_query": {"id": "SYNTH-CALLBACK-"+str(update),
        "from": {"id": uid}, "message": {"message_id": 1, "chat": {"id": uid, "type": "private"}},
        "data": data}}
    event["callback_query"].update(extras)
    return api.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, json=event)


def create(api, update=10):
    response = button(api,"subtest:create",update).json()
    assert response["method"] == "sendMessage"
    return response["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[-1]


def test_owner_sees_disabled_tariff_and_private_durable_trial(setup):
    engine, settings, api, calls = setup
    status = command(api,"/subtest",uid=preview.OWNER).json()
    assert "250 грн за 30 днів" in status["text"]
    assert "вимкнені" in status["text"] and "безкоштовний" in status["text"]
    assert "Не переказуй" in status["text"] and status["protect_content"] is True
    assert preview.PAYMENTS_ENABLED is False
    assert preview.view(engine)["orders"] == []
    oid = create(api)
    assert "Спочатку" in button(api,"subtest:approve:"+oid,11).json()["text"]
    assert preview.view(engine)["expires_at"] == 0
    button(api,"subtest:receipt:"+oid,12)
    approved = button(api,"subtest:approve:"+oid,13).json()
    assert "лише тестовий" in approved["text"] and "Реальної оплати немає" in approved["text"]
    state = preview.view(engine)
    assert state["orders"][0]["amount"] == 250 and state["orders"][0]["days"] == 30
    assert state["orders"][0]["status"] == "approved"
    # Independent handler/session and recreated engine see durable state.
    other = create_engine(engine.url)
    assert preview.view(other) == state
    other.dispose()
    assert all(method == "answerCallbackQuery" for method,_ in calls)
    with Session(engine) as db:
        assert not db.scalars(select(StarsTestOrder)).all()
        assert len(db.scalars(select(SubscriptionPreview)).all()) == 1


@pytest.mark.parametrize("cmd",["/subtest","/subscription_test"])
def test_public_users_never_see_preview_and_still_have_free_searches(setup, cmd):
    engine, _, api, calls = setup
    assert command(api,cmd,uid=111).json() == {"ok":True}
    assert button(api,"subtest:create",3,uid=111).json() == {"ok":True}
    assert button(api,"subtest:approve:"+"a"*32,4,uid=111).json() == {"ok":True}
    assert command(api,"/start",uid=111).status_code == 200
    assert subscribe(api,uid=111).status_code == 200
    assert api.get("/api/subscriptions",headers=headers(111)).json()[0]["enabled"] is True
    assert not calls
    with Session(engine) as db:
        assert not db.scalars(select(SubscriptionPreview)).all()


@pytest.mark.parametrize("extras",[
    {"from":{"id":111}}, {"from":{"id":True}},
    {"from":{"id":preview.OWNER,"is_bot":True}},
    {"message":{"chat":{"id":-1,"type":"group"}}},
    {"message":{"chat":{"id":preview.OWNER,"type":"supergroup"}}},
    {"message":{"chat":{"id":111,"type":"private"}}},
    {"message":None}, {"id":""}, {"id":123},
])
def test_forged_or_forwarded_callback_cannot_create_trial(setup, extras):
    engine,_,api,calls = setup
    assert button(api,"subtest:create",1,**extras).json() == {"ok":True}
    assert preview.view(engine)["orders"] == []
    assert not calls


def test_secret_authentication_wrong_bot_mention_and_payload_guards(setup):
    engine,_,api,calls = setup
    event = {"update_id":1,"message":{"from":{"id":preview.OWNER},
        "chat":{"id":preview.OWNER,"type":"private"},"text":"/subtest","date":int(time.time())}}
    assert api.post("/telegram/webhook",json=event).status_code == 403
    assert command(api,"/subtest@other_bot",uid=preview.OWNER).json() == {"ok":True}
    assert command(api,"/subtest@auto_deal_finder1_bot",uid=preview.OWNER).json()["chat_id"] == preview.OWNER
    for data in ["subtest:approve:bad","subtest:create:250","subtest:create:111", "subtest:approve:"+"a"*33,
                 "subtest:enable","subtest:pay", "subtest:receipt:"+"A"*32]:
        assert button(api,data,2).json() == {"ok":True}
    assert preview.view(engine)["orders"] == []
    assert not calls


def test_duplicate_approval_renewal_rejection_expiry_and_clock_rollback(setup):
    engine,_,_,_ = setup
    state,_ = preview.apply(engine,"create","",10,1000)
    first = state["orders"][-1]["id"]
    preview.apply(engine,"receipt",first,11,1001)
    approved,_ = preview.apply(engine,"approve",first,12,1002)
    expiry = approved["expires_at"]
    assert expiry == 1002+30*86400
    for update in [12,13,1]:
        assert preview.apply(engine,"approve",first,update,1003)[0]["expires_at"] == expiry
    renewed,_ = preview.apply(engine,"create","",14,1004)
    second = renewed["orders"][-1]["id"]
    assert second != first
    assert preview.apply(engine,"create","",15,1005)[0]["orders"][-1]["id"] == second
    preview.apply(engine,"receipt",second,16,1006)
    result,_ = preview.apply(engine,"approve",second,17,1007)
    assert result["expires_at"] == expiry+30*86400
    state = preview.view(engine)
    assert "завершений" in preview.render(state,state["expires_at"])["text"]
    assert "активний" in preview.render(state,state["expires_at"]-1)["text"]
    rollback,_ = preview.apply(engine,"create","",18,999)
    assert rollback == state
    rejected,_ = preview.apply(engine,"create","",18,1008)
    third = rejected["orders"][-1]["id"]
    rejected,_ = preview.apply(engine,"reject",third,19,1009)
    assert rejected["expires_at"] == state["expires_at"]
    assert rejected["orders"][-1]["status"] == "rejected"


def test_concurrent_buttons_grant_once_and_keep_one_open_order(setup):
    engine,_,_,_ = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i:preview.apply(engine,"create","",i+10,1000),range(4)))
    state = preview.view(engine)
    assert len(state["orders"]) == 1
    oid = state["orders"][0]["id"]
    preview.apply(engine,"receipt",oid,20,1001)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i:preview.apply(engine,"approve",oid,i+21,1002),range(4)))
    state = preview.view(engine)
    assert state["expires_at"] == 1002+30*86400
    assert len(state["orders"]) == 1


def test_namespace_high_update_cannot_hide_stop_or_change_searches_claims_budget_stars(setup):
    engine,_,api,_ = setup
    command(api,"/start",uid=preview.OWNER,update=1)
    sid = subscribe(api,uid=preview.OWNER).json()["id"]
    with Session(engine) as db:
        db.add(StarsTestOrder(id="SYNTH-EXISTING",user_id=preview.OWNER,command_update=5,
            created_at=1,state="paid",charge_id="SYNTH-CHARGE",paid_until=5000))
        db.commit()
        # Snapshot every pre-existing production table, not just selected columns.
        before = {table.name:list(db.execute(table.select()).mappings())
                  for table in Base.metadata.sorted_tables if table.name != "subscription_previews"}
    oid = create(api,update=1000)
    button(api,"subtest:receipt:"+oid,1001)
    button(api,"subtest:approve:"+oid,1002)
    with Session(engine) as db:
        after = {table.name:list(db.execute(table.select()).mappings())
                 for table in Base.metadata.sorted_tables if table.name != "subscription_previews"}
    assert after == before
    command(api,"/stop",uid=preview.OWNER,update=2)
    with Session(engine) as db:
        assert db.get(Search,sid).enabled is False
        assert db.get(User,preview.OWNER).last_update == 2
        assert db.get(StarsTestOrder,"SYNTH-EXISTING").paid_until == 5000


def test_trial_cap_is_bounded_without_erasing_history(setup):
    engine,_,_,_ = setup
    for i in range(preview.MAX_ORDERS):
        state,_ = preview.apply(engine,"create","",2*i,1000+i)
        preview.apply(engine,"reject",state["orders"][-1]["id"],2*i+1,1000+i)
    saved = preview.view(engine)["orders"]
    state,note = preview.apply(engine,"create","",1000,1021)
    assert state["orders"] == saved and len(saved) == preview.MAX_ORDERS
    assert "Ліміт" in note


def test_acknowledgement_failure_does_not_repeat_grant(setup):
    engine,settings,_,_ = setup
    state,_ = preview.apply(engine,"create","",1,1000)
    oid = state["orders"][0]["id"]
    preview.apply(engine,"receipt",oid,2,1000)
    event = {"update_id":3,"callback_query":{"id":"SYNTH-LOST-ACK","from":{"id":preview.OWNER},
        "message":{"chat":{"id":preview.OWNER,"type":"private"}},"data":"subtest:approve:"+oid}}
    def failed(*args,**kwargs):raise RuntimeError("Synthetic lost acknowledgement")
    assert preview.handle(engine,settings,event,request=failed,now=1001)["method"] == "sendMessage"
    expiry = preview.view(engine)["expires_at"]
    assert preview.handle(engine,settings,event,request=failed,now=1002)["method"] == "sendMessage"
    assert preview.view(engine)["expires_at"] == expiry


@pytest.mark.parametrize("quota_enabled, admin_id", [(False, 0), (True, preview.OWNER), (True, 111)])
def test_menu_is_owner_scoped_idempotent_and_preserves_public_commands(setup, quota_enabled, admin_id):
    engine,settings,_,calls = setup
    with Session(engine) as db:
        db.merge(SourceProbe(id="telegram-webhook-v1",status="configured",checked_at=0,requests=0,result={}))
        db.commit()
    settings = replace(settings,configure_webhook=True,ria_quota_management_enabled=quota_enabled,
                       admin_telegram_id=admin_id)
    preview.configure(engine,settings)
    preview.configure(engine,settings)
    assert len(calls) == 1
    method,payload = calls[0]
    assert method == "setMyCommands"
    assert payload["scope"] == {"type":"chat","chat_id":preview.OWNER}
    names = {c["command"] for c in payload["commands"]}
    assert {"start","stop","subtest","payment","refundtest"} <= names
    quota_names = {"check", "quota", "quota_set"}
    assert (quota_names <= names) is (quota_enabled and admin_id == preview.OWNER)
    if not (quota_enabled and admin_id == preview.OWNER):
        assert not quota_names & names
    assert len(names) == len(payload["commands"])
