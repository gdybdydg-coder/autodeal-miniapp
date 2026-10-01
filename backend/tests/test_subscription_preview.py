import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html.parser import HTMLParser

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
    assert "250 грн" in status["text"] and "30 днів" in status["text"]
    assert "Тест без оплати" in status["text"] and "безкоштовний" in status["text"]
    assert status["parse_mode"] == "HTML"
    assert "Не переказуй" in status["text"] and status["protect_content"] is True
    assert preview.PAYMENTS_ENABLED is False
    assert preview.view(engine)["orders"] == []
    oid = create(api)
    assert "Спочатку" in button(api,"subtest:approve:"+oid,11).json()["text"]
    assert preview.view(engine)["expires_at"] == 0
    button(api,"subtest:receipt:"+oid,12)
    approved = button(api,"subtest:approve:"+oid,13).json()
    assert "Тест підтверджено" in approved["text"] and "Тест без оплати" in approved["text"]
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


@pytest.mark.parametrize("cmd",["/subtest","/subscription_test","/subtest_admin"])
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
    assert {"start","stop","subtest","subtest_admin","payment","refundtest"} <= names
    quota_names = {"check", "quota", "quota_set"}
    assert (quota_names <= names) is (quota_enabled and admin_id == preview.OWNER)
    if not (quota_enabled and admin_id == preview.OWNER):
        assert not quota_names & names
    assert len(names) == len(payload["commands"])


class TelegramHTML(HTMLParser):
    """Check balanced Telegram-supported tags and their visible text."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.visible = [], []
        self.feed(source)
        self.close()
        assert not self.tags

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "code"} and not attrs
        self.tags.append(tag)

    def handle_endtag(self, tag):
        assert self.tags and self.tags.pop() == tag

    def handle_data(self, text):
        self.visible.append(text)


@pytest.mark.parametrize("status", [None,"awaiting","review","approved","rejected"])
def test_customer_card_hides_identifiers_and_owner_actions_in_every_state(status):
    oid = "b"*32
    state = preview.initial()
    if status:
        state["orders"] = [{"id":oid,"amount":250,"days":30,"status":status}]
    state["expires_at"] = 2000
    customer = preview.render(state,1000)
    assert customer["parse_mode"] == "HTML"
    visible = "".join(TelegramHTML(customer["text"]).visible)
    assert oid not in visible and "AD-" not in visible and "ID" not in visible
    assert str(preview.OWNER) not in visible and "SYNTH" not in visible
    assert "250 грн" in visible and "30 днів" in visible and "Тест без оплати" in visible
    assert "Не переказуй" in visible
    actions = [b["callback_data"] for row in customer["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(a.startswith(("subtest:approve:","subtest:reject:")) or a=="subtest:admin" for a in actions)
    if status == "rejected":
        assert "Продовження відхилено" in visible and "залишається активним" in visible
    if status:
        admin = preview.render(state,1000,admin=True)
        assert "AD-"+oid.upper() in "".join(TelegramHTML(admin["text"]).visible)


def test_owner_details_are_read_only_and_cannot_mask_a_pending_trial_action(setup):
    engine,_,api,_ = setup
    oid = create(api,10)
    with Session(engine) as db:
        row = db.get(SubscriptionPreview,preview.OWNER)
        before = (copy.deepcopy(row.state),row.version,row.updated_at)
    admin = button(api,"subtest:admin",1000).json()
    assert "AD-"+oid.upper() in admin["text"]
    client = button(api,"subtest:view",1001).json()
    assert oid.upper() not in client["text"]
    assert "AD-"+oid.upper() in command(api,"/subtest_admin",uid=preview.OWNER,update=1002).json()["text"]
    with Session(engine) as db:
        row = db.get(SubscriptionPreview,preview.OWNER)
        assert (row.state,row.version,row.updated_at) == before
    # Read-only views with a later ID must not suppress a queued earlier action.
    button(api,"subtest:receipt:"+oid,11)
    assert preview.view(engine)["orders"][-1]["status"] == "review"
    review = command(api,"/subtest_admin",uid=preview.OWNER,update=1003).json()
    assert any(b["callback_data"]=="subtest:approve:"+oid
               for row in review["reply_markup"]["inline_keyboard"] for b in row)


@pytest.mark.parametrize("data",["subtest:admin","subtest:view"])
def test_public_callbacks_cannot_read_owner_details(setup, data):
    engine,_,api,calls = setup
    create(api,10)
    calls.clear()
    assert button(api,data,1000,uid=111).json() == {"ok":True}
    assert button(api,data,1001,**{"message":{"chat":{"id":-1,"type":"group"}}}).json() == {"ok":True}
    assert not calls


def test_dynamic_text_is_escaped_and_does_not_break_telegram_formatting():
    state = preview.initial()
    state["orders"] = [{"id":"x<&>","amount":"250<&>","days":30,"status":"review"}]
    note = '<script>bad & "test"</script>'
    for admin in (False,True):
        reply = preview.render(state,1000,note,admin=admin)
        assert "<script>" not in reply["text"] and "250&lt;&amp;&gt;" in reply["text"]
        visible = "".join(TelegramHTML(reply["text"]).visible)
        assert note in visible
        if admin:
            assert "AD-X<&>" in visible


@pytest.fixture
def recipient_profile(monkeypatch):
    # Fictional offline account: an all-zero bank routing code, never a live IBAN.
    bban = "0"*25
    checksum = 98-int(bban+"301000") % 97
    profile = {"mode":"test_only", "recipient_name":"ФОП Тест <&>",
               "recipient_code":"0000000000", "iban":f"UA{checksum:02d}"+bban,
               "bank_name":"Тестовий банк <&>"}
    monkeypatch.setenv(preview.RECIPIENT_ENV,json.dumps(profile))
    return profile


def test_requisites_are_private_inert_escaped_and_do_not_consume_updates(setup,recipient_profile):
    engine,_,api,calls = setup
    command(api,"/start",uid=preview.OWNER,update=1)
    sid = subscribe(api,uid=preview.OWNER).json()["id"]
    oid = create(api,10)
    with Session(engine) as db:
        before = {table.name:list(db.execute(table.select()).mappings())
                  for table in Base.metadata.sorted_tables}
    calls.clear()
    card = button(api,"subtest:requisites",1000).json()
    visible = "".join(TelegramHTML(card["text"]).visible)
    assert recipient_profile["recipient_name"] in visible and recipient_profile["bank_name"] in visible
    assert recipient_profile["iban"] in visible.replace(" ", "")
    assert "250 грн" in visible and "30 днів" in visible and "Не переказуй" in visible
    assert "ID" not in visible and "AD-" not in visible and oid.upper() not in visible
    keys = [b for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert next(b["copy_text"]["text"] for b in keys if "copy_text" in b) == recipient_profile["iban"]
    assert not any("pay" in b or "url" in b for b in keys)
    assert card["protect_content"] is True
    assert "<code>" in card["text"] and "&lt;&amp;&gt;" in card["text"]
    receipt = button(api,"subtest:receipt_preview",1001).json()
    assert "не надсилай справжню квитанцію" in receipt["text"]
    assert next(b["callback_data"] for row in receipt["reply_markup"]["inline_keyboard"]
                for b in row if b["text"] == "📎 Тестова квитанція") == "subtest:receipt:"+oid
    with Session(engine) as db:
        after = {table.name:list(db.execute(table.select()).mappings())
                 for table in Base.metadata.sorted_tables}
    assert before == after
    assert all(method == "answerCallbackQuery" for method,_ in calls)
    # The high-ID preview cannot suppress either an earlier receipt or /stop.
    button(api,"subtest:receipt:"+oid,11)
    assert preview.view(engine)["orders"][-1]["status"] == "review"
    command(api,"/stop",uid=preview.OWNER,update=2)
    with Session(engine) as db:
        assert db.get(Search,sid).enabled is False
    assert preview.PAYMENTS_ENABLED is False


@pytest.mark.parametrize("data",["subtest:requisites","subtest:receipt_preview"])
@pytest.mark.parametrize("extras",[
    {"from":{"id":111}}, {"from":{"id":preview.OWNER,"is_bot":True}},
    {"message":{"chat":{"id":-1,"type":"group"}}},
    {"message":{"chat":{"id":111,"type":"private"}}},
])
def test_receiving_profile_is_not_read_for_forged_or_other_users(setup,monkeypatch,data,extras):
    engine,_,api,calls = setup
    def forbidden():raise AssertionError("Private profile read before authentication")
    monkeypatch.setattr(preview,"receiving_profile",forbidden)
    assert button(api,data,1000,**extras).json() == {"ok":True}
    assert not calls and preview.view(engine) == preview.initial()


@pytest.mark.parametrize("bad",[
    "", "not-json", "[]", "null", "x"*4097,
    {"mode":"production"}, {"mode":"test_only"},
    {"recipient_code":"1"}, {"iban":"UA00"+"0"*25},
    {"recipient_name":""}, {"bank_name":"x"*161}, {"bank_name":"test\nname"},
    {"iban":None}, {"recipient_code":123},
])
def test_invalid_or_incomplete_profile_is_not_shown(recipient_profile,monkeypatch,bad):
    if isinstance(bad,dict):
        value=json.dumps({**recipient_profile,**bad})
        if bad == {"mode":"test_only"}:value=json.dumps(bad)
    else:value=bad
    monkeypatch.setenv(preview.RECIPIENT_ENV,value)
    assert preview.receiving_profile() is None
    card=preview.render_requisites(preview.initial())
    assert "ще не налаштовані" in card["text"] and recipient_profile["iban"] not in card["text"]
    assert not any("copy_text" in b for row in card["reply_markup"]["inline_keyboard"] for b in row)


def test_requisites_keep_saved_quote_and_ignore_real_receipt(setup,recipient_profile):
    engine,_,api,calls=setup
    state=preview.initial()
    state["orders"]=[{"id":"a"*32,"amount":120,"days":7,"status":"awaiting"}]
    card=preview.render_requisites(state)
    assert "120 грн" in card["text"] and "7 днів" in card["text"] and "250 грн" not in card["text"]
    oid=create(api,10)
    before=preview.view(engine)
    event={"update_id":1001,"message":{"from":{"id":preview.OWNER},
           "chat":{"id":preview.OWNER,"type":"private"},"date":int(time.time()),
           "document":{"file_id":"SYNTH-REAL-RECEIPT","file_name":"receipt.pdf","mime_type":"application/pdf"}}}
    response=api.post("/telegram/webhook",headers={"X-Telegram-Bot-Api-Secret-Token":SECRET},json=event)
    assert response.json()=={"ok":True}
    assert preview.view(engine)==before and preview.view(engine)["orders"][-1]["id"]==oid
