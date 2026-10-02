"""Public bank flow and rollout: synthetic accounts, signed identities, no real sends."""
from dataclasses import replace
import json
import time

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from backend import billing, manual_checkout as c, manual_launch as launch, manual_payments as m, telegram_setup
from backend.billing_models import BillingCampaign, CampaignRecipient, MarketingConsent, Entitlement
from backend.manual_payment_models import PaymentRequest, PaymentNotice, BankCredit
from backend.models import User, Search, MonitorControl, SourceProbe, Delivery, DeliveryTiming
from backend.tests.test_backend import headers, command, SECRET
from backend.tests.test_manual_payments import review, ADMIN, UID, OTHER, NOW, preview, count


@pytest.fixture
def bank(review, monkeypatch):
    engine, settings, client = review
    bban = "0"*25
    iban = f"UA{98-int(bban+'301000')%97:02d}"+bban
    monkeypatch.setenv(c.subscription_preview.RECIPIENT_ENV, json.dumps({"mode":"test_only",
        "recipient_name":"ФОП Тест <&>", "recipient_code":"0000000000", "iban":iban,"bank_name":"Тестовий банк"}))
    monkeypatch.setenv(c.subscription_preview.CARD_ENV, "4242 4242 4242 4242")
    monkeypatch.setenv("MANUAL_PAYMENT_PUBLIC_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAYMENT_REVIEW_ENABLED", "true")
    monkeypatch.setattr(telegram_setup, "call", lambda *a, **kw: {"ok":True})
    with Session(engine) as db, db.begin():
        ctrl=billing.control(db); ctrl.offer=dict(c.OFFER); ctrl.sales=True
    return review


def callback(bank, data, uid=UID, update=100, **changes):
    cb={"id":"FIXTURE-CB", "from":{"id":uid,"first_name":"Тест"},
        "message":{"chat":{"id":uid,"type":"private"}}, "data":data}
    cb.update(changes)
    return bank[2].post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token":SECRET},
                        json={"update_id":update,"callback_query":cb})


def ready_health(engine, monkeypatch):
    monkeypatch.setenv("MANUAL_PAYMENT_BACKUP_REFERENCE","libfile_SYNTHETIC_OFFLINE_FIXTURE")
    monkeypatch.setenv("MANUAL_PAYMENT_STORAGE_READY","true")
    with Session(engine) as db,db.begin():
        db.merge(MonitorControl(id="pilot",heartbeat=NOW,status="running"))
        db.merge(SourceProbe(id=telegram_setup.PROBE_ID,status="configured",checked_at=NOW,requests=0,result={}))
        db.add(Delivery(id=999,user_id=UID,listing_id=999,state="sent"))
        db.add(DeliveryTiming(delivery_id=999,queued_at=NOW-1,accepted_at=NOW))


def test_bot_checkout_persists_terms_copy_controls_and_waits_for_owner(bank):
    engine, settings, client=bank
    intro=command(client,"/subscription").json()
    assert "250 грн / 30 днів" in intro["text"]
    assert count(engine,PaymentRequest)==0
    terms=callback(bank,c.PREFIX+"terms").json()
    assert "Повні умови" not in terms["text"]
    full=callback(bank,c.PREFIX+"full_terms",update=104).json()
    assert "Натискання «Я оплатив»" in full["text"]
    details=callback(bank,c.PREFIX+"accept:"+c.TERMS_VERSION).json()
    assert "ФОП Тест &lt;&amp;&gt;" in details["text"]
    assert details["text"].index("Рахунок · IBAN")<details["text"].index("Номер картки")
    copies=[b["copy_text"]["text"] for row in details["reply_markup"]["inline_keyboard"] for b in row if "copy_text" in b]
    assert "4242424242424242" in copies
    with Session(engine) as db:
        row=db.scalar(select(PaymentRequest)); code=row.id
        assert row.terms_version==c.TERMS_VERSION and row.terms_text==c.TERMS and row.terms_accepted_at
        assert not billing.expiry(db,UID)
    callback(bank,c.PREFIX+"accept:"+c.TERMS_VERSION,update=101)
    assert count(engine,PaymentRequest)==1
    result=callback(bank,c.PREFIX+"paid:"+code,update=102).json()
    assert "Повторно не сплачуй" in result["text"]
    callback(bank,c.PREFIX+"paid:"+code,update=103)
    assert count(engine,PaymentNotice)==1 and count(engine,BankCredit)==0
    assert "4242" not in callback(bank,c.PREFIX+"details:"+code).json()["text"]
    row=m.get_status(engine,settings,UID,code)
    proof=preview(bank,row,now=time.time())
    m.confirm(engine,settings,ADMIN,proof["confirmation"],time.time())
    assert "Доступ до" in callback(bank,c.PREFIX+"status:"+code).json()["text"]
    assert "sendInvoice" not in json.dumps(details)


def test_api_requires_terms_and_hides_requisites_from_foreign_users(bank):
    engine,settings,client=bank
    assert client.post("/api/manual-payments",headers=headers(),json={}).status_code==422
    assert client.post("/api/manual-payments",headers=headers(),json={"terms_version":"old"}).status_code==409
    assert count(engine,PaymentRequest)==0
    reply=client.post("/api/manual-payments",headers=headers(),json={"terms_version":c.TERMS_VERSION})
    assert reply.status_code==201
    code=reply.json()["code"]
    assert client.get("/api/manual-payments/"+code+"/requisites",headers=headers(OTHER)).status_code==404
    assert "recipient" in client.get("/api/manual-payments/"+code+"/requisites",headers=headers()).json()
    assert callback(bank,c.PREFIX+"details:"+code,uid=OTHER).json()["text"].startswith("Заявку не знайдено")
    assert "user_id" not in client.get("/api/manual-payments/offer",headers=headers()).json()["request"]


@pytest.mark.parametrize("changes",[
    {"from":{"id":UID,"is_bot":True}},
    {"message":{"chat":{"id":-1,"type":"group"}}},
    {"message":{"chat":{"id":OTHER,"type":"private"}}},
])
def test_forged_callbacks_cannot_open_checkout(bank,changes):
    response=callback(bank,c.PREFIX+"accept:"+c.TERMS_VERSION,**changes)
    assert response.json()=={"ok":True}
    assert count(bank[0],PaymentRequest)==0


def test_paused_sales_keep_paid_report_status_and_support(bank,monkeypatch):
    engine,settings,client=bank
    row=c.create(engine,settings,UID,NOW,c.TERMS_VERSION)
    monkeypatch.setenv("MANUAL_PAYMENT_PUBLIC_ENABLED","false")
    assert "закрит" in callback(bank,c.PREFIX+"details:"+row["code"]).json()["text"]
    assert "Очікує перевірки" in callback(bank,c.PREFIX+"paid:"+row["code"]).json()["text"]
    assert client.get("/api/manual-payments/"+row["code"],headers=headers()).status_code==200
    assert "Напиши /paysupport" in command(client,"/paysupport").json()["text"]
    assert command(client,"/stop").status_code==200
    with Session(engine) as db:
        assert db.scalar(select(Search)).filters=={"brand":"BMW"}


def test_missing_profile_never_offers_payment_or_creates_request(bank,monkeypatch):
    monkeypatch.delenv(c.subscription_preview.RECIPIENT_ENV)
    assert not bank[2].get("/api/manual-payments/offer",headers=headers()).json()["sales_enabled"]
    result=callback(bank,c.PREFIX+"accept:"+c.TERMS_VERSION).json()
    assert "Не переказуй" in result["text"] and count(bank[0],PaymentRequest)==0


def test_prepare_and_live_require_backup_storage_and_health(bank,monkeypatch):
    engine,settings,_=bank
    settings=replace(settings,manual_payment_notices_enabled=True,monitor_enabled=True)
    with Session(engine) as db,db.begin():
        billing.control(db).sales=False
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","prepare")
    launch.initialize(engine,settings,NOW)
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","live")
    launch.initialize(engine,settings,NOW+1)
    with Session(engine) as db:
        row=db.get(BillingCampaign,launch.CAMPAIGN)
        assert "verified_private_backup_missing" in row.blockers
        assert "storage_continuity_not_verified" in row.blockers
        assert not billing.control(db).sales and not billing.control(db).enforce
    assert count(engine,CampaignRecipient)==0


def test_one_time_rollout_preserves_access_optouts_and_does_not_rearm(bank,monkeypatch):
    engine,settings,_=bank
    settings=replace(settings,manual_payment_notices_enabled=True,monitor_enabled=True)
    with Session(engine) as db,db.begin():
        billing.control(db).sales=False
        for uid in (UID,OTHER):db.get(User,uid).ready=True
        db.add(MarketingConsent(user_id=OTHER,allowed=False,blocked=False,source="explicit_opt_out",at=NOW))
        db.add(Entitlement(user_id=UID,expires_at=NOW+9000,updated_at=NOW))
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","prepare")
    launch.initialize(engine,settings,NOW)
    ready_health(engine,monkeypatch)
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","live")
    launch.initialize(engine,settings,NOW+1)
    launch.initialize(engine,settings,NOW+2)
    assert count(engine,CampaignRecipient)==1
    calls=[]
    def accepted(token,method,payload,**kw):
        calls.append(payload);return {"ok":True,"result":{"message_id":1}}
    assert launch.tick(engine,settings,accepted,NOW+3)=="sent"
    assert launch.tick(engine,settings,accepted,NOW+6)=="empty"
    launch.initialize(engine,settings,NOW+7)
    assert launch.tick(engine,settings,accepted,NOW+8)=="inactive"
    assert len(calls)==1 and calls[0]["chat_id"]==UID and calls[0]["allow_paid_broadcast"] is False
    assert calls[0]["text"]==launch.COPY
    with Session(engine) as db,db.begin():
        assert billing.expiry(db,UID)==NOW+9000
        assert billing.control(db).enforce and billing.control(db).sales
        billing.control(db).sales=billing.control(db).enforce=False
    launch.initialize(engine,settings,NOW+9)
    with Session(engine) as db:
        assert not billing.control(db).sales and not billing.control(db).enforce


def test_unknown_send_is_not_retried_and_late_optout_is_respected(bank,monkeypatch):
    engine,settings,_=bank
    settings=replace(settings,manual_payment_notices_enabled=True,monitor_enabled=True)
    with Session(engine) as db,db.begin():
        billing.control(db).sales=False
        for uid in (UID,OTHER):db.get(User,uid).ready=True
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","prepare")
    launch.initialize(engine,settings,NOW)
    ready_health(engine,monkeypatch)
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","live")
    launch.initialize(engine,settings,NOW+1)
    assert launch.tick(engine,settings,lambda *a,**kw:{},NOW+2)=="uncertain"
    with Session(engine) as db,db.begin():db.get(User,OTHER).ready=False
    assert launch.tick(engine,settings,lambda *a,**kw:pytest.fail("must not send"),NOW+5)=="excluded"
    assert launch.tick(engine,settings,lambda *a,**kw:pytest.fail("must not retry"),NOW+8)=="empty"
    with Session(engine) as db:
        assert db.get(CampaignRecipient,(launch.CAMPAIGN,UID)).state=="uncertain"


def test_campaign_pauses_when_payment_details_disappear(bank,monkeypatch):
    engine,settings,_=bank
    settings=replace(settings,manual_payment_notices_enabled=True,monitor_enabled=True)
    with Session(engine) as db,db.begin():
        billing.control(db).sales=False
        db.get(User,UID).ready=True
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","prepare")
    launch.initialize(engine,settings,NOW)
    ready_health(engine,monkeypatch)
    monkeypatch.setenv("MANUAL_PAYMENT_LAUNCH_STAGE","live")
    launch.initialize(engine,settings,NOW+1)
    monkeypatch.delenv(c.subscription_preview.RECIPIENT_ENV)
    assert launch.tick(engine,settings,lambda *a,**kw:pytest.fail("checkout unavailable"),NOW+2)=="paused"
