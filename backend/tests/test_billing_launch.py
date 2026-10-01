"""Synthetic fixtures only; no live prices, customer data or network access."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from backend import billing as b, billing_campaign as c, telegram_setup, worker
from backend.app import Settings, create_app
from backend.models import Base, User, SourceProbe, MonitorControl, Delivery, DeliveryTiming, StarsTestOrder, Search
from backend.billing_models import (BillingControl, BillingOrder, Entitlement, AccessEvent,
    MarketingConsent, BillingCampaign, CampaignRecipient, BillingNotice)
from backend.tests.test_backend import TOKEN, SECRET, headers, command, subscribe, car

NOW = c.WHEN-600
ADMIN = 987654321
OFFER = {"stars": 137, "days": 30, "currency": "XTR", "price_approval": "synthetic-price-decision",
         "terms": "Synthetic terms", "terms_version": "fixture-1", "refund_terms": "Synthetic refund policy",
         "transition_review": "synthetic-inventory", "search_review": "synthetic-check",
         "payment_review": "synthetic-check", "support_route": "bot_paysupport"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_LAUNCH_PREPARED", "true")
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(ADMIN))
    monkeypatch.delenv("SUBSCRIPTION_APPROVED_OFFER_JSON", raising=False)
    engine = create_engine("sqlite:///"+str(tmp_path/"billing.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    settings = Settings("unused", TOKEN, SECRET, True, True, admin_telegram_id=ADMIN)
    c.initialize(engine, settings, now=NOW)
    with Session(engine) as db:
        db.add_all([User(id=111, ready=True), User(id=222, ready=True), User(id=ADMIN, ready=True)])
        db.commit()
    yield engine, settings
    engine.dispose()


def event(text="/subscription", uid=111, update_id=1, now=NOW, callback=None, payment=None):
    msg = {"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "date": int(now), "text": text}
    result = {"update_id": update_id, "message": msg}
    if payment:
        msg["successful_payment"] = payment
    if callback:
        result = {"update_id": update_id, "callback_query": {"id": "fixture-callback", "data": callback,
                  "from": {"id": uid}, "message": msg}}
    return result


def handle(setup, **kw):
    engine, settings = setup
    now = kw.get("now", NOW)
    return b.handle(engine, settings, event(**kw), request=lambda *a, **k: {"ok": True}, now=now)


def opened(setup):
    engine, _ = setup
    with Session(engine) as db:
        ctrl = b.control(db); ctrl.offer = dict(OFFER); ctrl.sales = ctrl.enforce = True; db.commit()


def invoice(setup, uid=111, update_id=5, now=NOW):
    terms = handle(setup, text="/terms", uid=uid, now=now)
    action = terms["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    return handle(setup, callback=action, uid=uid, update_id=update_id, now=now)


def payment(inv, charge="fixture-charge"):
    return {"invoice_payload": inv["payload"], "currency": "XTR", "total_amount": 137,
            "telegram_payment_charge_id": charge}


def ready_campaign(setup, now=c.WHEN, consent=True):
    engine, settings = setup
    settings = replace(settings, monitor_enabled=True)
    with Session(engine) as db:
        ctrl = b.control(db); ctrl.offer = dict(OFFER)
        row = db.get(BillingCampaign, c.CAMPAIGN); row.content = c.content(OFFER); row.status = "scheduled"
        db.add(MonitorControl(id="pilot", heartbeat=now))
        db.add(SourceProbe(id=telegram_setup.PROBE_ID, status="configured", checked_at=now, result={}))
        delivery = Delivery(user_id=111, listing_id=1, state="sent"); db.add(delivery); db.flush()
        db.add(DeliveryTiming(delivery_id=delivery.id, queued_at=now, accepted_at=now))
        db.get(BillingNotice, "launch-preview:"+c.VERSION).state = "sent"
        if consent:
            db.add(MarketingConsent(user_id=111, allowed=True, blocked=False, at=now, source="fixture"))
        db.commit()
    return engine, settings


def sender(calls, response=None):
    def send(token, method, payload, **kwargs):
        calls.append((method, payload))
        if method == "getMe":
            return {"ok": True, "result": {"username": telegram_setup.BOT_USERNAME}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": telegram_setup.WEBHOOK_URL}}
        return response or {"ok": True, "result": {"message_id": 999}}
    return send


def test_disabled_offer_free_and_no_invoices(setup):
    engine, settings = setup
    result = handle(setup)
    assert "безкоштовно" in result["text"] and "250" not in result["text"]
    assert handle(setup, callback=b.PREFIX+"buy:forged")["method"] == "sendMessage"
    with Session(engine) as db:
        assert b.allowed(db, 111, NOW)
        state = c.snapshot(db, settings, NOW)
        assert state["eligible_now"] == 0 and state["status"] == "scheduled_blocked"
        assert not state["sales"] and not state["enforce"]
        assert state["admin_matches_protected_config"] is True


def test_invoice_terms_no_autorenew_checkout_never_grants(setup):
    engine, settings = setup; opened(setup)
    inv = invoice(setup)
    assert inv["currency"] == "XTR" and inv["prices"][0]["amount"] == 137
    assert "subscription_period" not in inv
    assert invoice(setup)["ok"] is True  # Same callback has one durable order.
    data = payment(inv)
    result = b.handle(engine, settings, {"pre_checkout_query": {**data, "from": {"id": 111}, "id": "q"}}, now=NOW)
    assert result["ok"] is True
    with Session(engine) as db:
        assert not b.allowed(db, 111, NOW)
        assert db.scalar(select(BillingOrder)).state == "pending"


@pytest.mark.parametrize("field,value", [("currency", "UAH"), ("total_amount", True), ("total_amount", 138),
    ("invoice_payload", b.PREFIX+"not-real"), ("telegram_payment_charge_id", "")])
def test_invalid_success_cannot_grant(setup, field, value):
    engine, _ = setup; opened(setup)
    data = payment(invoice(setup)); data[field] = value
    assert b.apply_payment(engine, 111, data, NOW) is False
    with Session(engine) as db:
        assert db.get(Entitlement, 111) is None


def test_wrong_payer_and_unknown_charge_replay(setup):
    engine, _ = setup; opened(setup)
    one = payment(invoice(setup))
    assert not b.apply_payment(engine, 222, one, NOW)
    assert b.apply_payment(engine, 111, one, NOW)
    other = payment(invoice(setup, uid=222, update_id=6))
    assert not b.apply_payment(engine, 222, other, NOW)


def test_duplicate_concurrent_success_renewal_and_restart(setup):
    engine, settings = setup; opened(setup)
    data = payment(invoice(setup))
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: b.apply_payment(engine, 111, data, NOW), range(4)))
    assert result.count(True) == 1
    second = payment(invoice(setup, update_id=6), charge="fixture-charge-two")
    assert b.apply_payment(engine, 111, second, NOW+100)
    other = create_engine(engine.url)
    c.initialize(other, settings, now=NOW+200)
    with Session(other) as db:
        assert b.expiry(db, 111) == NOW+60*86400
        assert b.allowed(db, 111, NOW+60*86400-1)
        assert not b.allowed(db, 111, NOW+60*86400)
        assert len(db.scalars(select(AccessEvent)).all()) == 2
    other.dispose()


def test_late_success_survives_sales_pause_and_runtime_flag(setup, monkeypatch):
    engine, _ = setup; opened(setup)
    data = payment(invoice(setup))
    with Session(engine) as db:
        b.control(db).sales = False; db.commit()
    monkeypatch.setenv("SUBSCRIPTION_LAUNCH_PREPARED", "false")
    assert "активний" in handle(setup, payment=data, now=NOW+2000)["text"]


def test_previous_gift_and_real_pilot_preserved(setup):
    engine, _ = setup; opened(setup)
    with Session(engine) as db:
        db.add(StarsTestOrder(id="fixture-pilot", user_id=111, command_update=88, created_at=NOW-10,
                              state="paid", paid_until=NOW+500, charge_id="pilot-charge"))
        db.add(Entitlement(user_id=222, expires_at=NOW+9999, updated_at=NOW)); db.commit()
    data = payment(invoice(setup))
    assert b.apply_payment(engine, 111, data, NOW)
    with Session(engine) as db:
        assert b.expiry(db, 111) == NOW+500+30*86400
        assert b.expiry(db, 222) == NOW+9999
        assert db.get(StarsTestOrder, "fixture-pilot").state == "paid"


def test_price_or_terms_change_reject_old_checkout_and_button(setup):
    engine, settings = setup; opened(setup)
    terms = handle(setup, text="/terms")
    old_action = terms["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    data = payment(invoice(setup))
    with Session(engine) as db:
        b.control(db).offer = {**OFFER, "stars": 150}; db.commit()
    assert handle(setup, callback=old_action, update_id=8)["method"] == "sendMessage"
    assert b.handle(engine, settings, {"pre_checkout_query": {**data, "from": {"id": 111}, "id": "q"}}, now=NOW)["ok"] is False


def test_admin_auth_and_config_mismatch_no_preview(setup, monkeypatch):
    engine, settings = setup
    assert handle(setup, text="/billing_admin pause")["ok"] is True
    assert "campaign_id" in handle(setup, text="/billing_admin", uid=ADMIN)["text"]
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "666")
    calls = []
    assert c.deliver_notice(engine, settings, sender(calls), NOW) == "admin_mismatch"
    assert calls == []
    assert handle(setup, text="/billing_admin pause", uid=ADMIN) == {"ok": True}


def test_stop_and_consent_are_independent_of_searches(setup):
    engine, _ = setup
    assert handle(setup, text="/start") is None
    handle(setup, callback=b.PREFIX+"on", update_id=3)
    handle(setup, text="/stop", update_id=4)
    handle(setup, callback=b.PREFIX+"on", update_id=2)
    with Session(engine) as db:
        assert db.get(MarketingConsent, 111).allowed is False
        assert db.get(User, 111).last_update == -1  # Existing /stop handler retains its own ordering.


def test_support_ticket_and_owner_reply_durable(setup):
    engine, settings = setup
    handle(setup, text="/paysupport synthetic question", update_id=12)
    handle(setup, text="/paysupport synthetic question", update_id=12)
    handle(setup, text="/billing_admin reply 12 synthetic answer", update_id=13, uid=ADMIN)
    with Session(engine) as db:
        rows = db.scalars(select(BillingNotice).where(BillingNotice.kind.in_(["support", "support_reply"]))).all()
        assert len(rows) == 2
        assert next(r for r in rows if r.kind == "support_reply").user_id == 111


def test_no_launch_before_time_block_at_time_no_rearm_after_restart(setup):
    engine, settings = setup; calls = []
    assert c.tick(engine, settings, sender(calls), NOW) == "scheduled_blocked"
    assert calls == []
    assert c.tick(engine, settings, sender(calls), c.WHEN) == "blocked"
    c.initialize(engine, settings, now=c.WHEN+1)
    assert c.tick(engine, settings, sender(calls), c.WHEN+2) == "blocked"
    with Session(engine) as db:
        assert not b.control(db).sales and not b.control(db).enforce
        assert "commercial_stars_price_not_approved" in db.get(BillingCampaign, c.CAMPAIGN).blockers
        assert db.get(BillingNotice, "launch-report:"+c.VERSION)


def test_approved_launch_sends_only_consent_then_restart_no_duplicate(setup):
    engine, settings = ready_campaign(setup); calls = []
    assert c.tick(engine, settings, sender(calls), c.WHEN-1) == "scheduled"
    assert calls == []
    assert c.tick(engine, settings, sender(calls), c.WHEN) == "sent"
    assert c.tick(engine, settings, sender(calls), c.WHEN+6) == "complete"
    c.initialize(engine, settings, now=c.WHEN+7)
    c.tick(engine, settings, sender(calls), c.WHEN+8)
    sends = [payload for method, payload in calls if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == 111 and sends[0]["allow_paid_broadcast"] is False
    assert "137 ⭐" in sends[0]["text"] and "250 грн" not in sends[0]["text"]
    with Session(engine) as db:
        assert b.control(db).sales and b.control(db).enforce
        assert db.get(CampaignRecipient, (c.CAMPAIGN, 111)).message_id == 999


def test_paid_blocked_and_opt_out_excluded(setup):
    engine, settings = ready_campaign(setup)
    with Session(engine) as db:
        db.add(Entitlement(user_id=111, expires_at=c.WHEN+100, updated_at=NOW)); db.commit()
    calls=[]
    assert c.tick(engine, settings, sender(calls), c.WHEN) == "complete"
    assert not [x for x in calls if x[0] == "sendMessage"]


@pytest.mark.parametrize("failure", ["heartbeat", "api", "late"])
def test_readiness_failures_keep_access_free(setup, failure):
    engine, settings = ready_campaign(setup)
    if failure == "heartbeat":
        with Session(engine) as db:
            db.get(MonitorControl, "pilot").heartbeat -= 999; db.commit()
    request = (lambda *a, **k: {"ok": False}) if failure == "api" else sender([])
    now = c.WHEN+901 if failure == "late" else c.WHEN
    assert c.tick(engine, settings, request, now) in ("blocked", "expired")
    with Session(engine) as db:
        assert not b.control(db).sales and not b.control(db).enforce


def test_429_retry_and_timeout_quarantine(setup):
    engine, settings = ready_campaign(setup); calls=[]
    limited = sender(calls, {"ok": False, "error_code": 429, "parameters": {"retry_after": 10}})
    assert c.tick(engine, settings, limited, c.WHEN) == "retry"
    c.tick(engine, settings, limited, c.WHEN+6)
    assert len([x for x in calls if x[0] == "sendMessage"]) == 1
    assert c.tick(engine, settings, sender(calls, {"uncertain": True}), c.WHEN+11) == "uncertain"
    c.tick(engine, settings, sender(calls), c.WHEN+18)
    assert len([x for x in calls if x[0] == "sendMessage"]) == 2


def test_send_claim_restart_never_replays_and_lease_excludes_other_process(setup):
    engine, settings = ready_campaign(setup); calls=[]
    with Session(engine) as db:
        row=db.get(BillingCampaign,c.CAMPAIGN); row.status="running"; row.lease_until=c.WHEN+60
        db.add(CampaignRecipient(campaign_id=c.CAMPAIGN,user_id=111,state="sending",attempted_at=NOW))
        db.commit()
    assert c.tick(engine,settings,sender(calls),c.WHEN)=="busy"
    with Session(engine) as db:
        assert db.get(CampaignRecipient,(c.CAMPAIGN,111)).state=="uncertain"
    assert calls==[]


def test_cancel_and_priority(setup):
    engine, settings = ready_campaign(setup)
    with Session(engine) as db:
        db.add(Delivery(user_id=222, listing_id=2, state="pending")); db.commit()
    calls=[]
    assert c.tick(engine, settings, sender(calls), c.WHEN)=="yielding_to_service"
    handle(setup, text="/billing_admin cancel", uid=ADMIN, update_id=90)
    assert c.tick(engine, settings, sender(calls), c.WHEN+6)=="cancelled"
    assert not [x for x in calls if x[0]=="sendMessage"]


def test_preview_exactly_one_and_timeout_no_retry(setup):
    engine, settings = setup; calls=[]
    assert c.deliver_notice(engine, settings, sender(calls), NOW)=="sent"
    c.initialize(engine, settings, now=NOW+1)
    assert c.deliver_notice(engine, settings, sender(calls), NOW+2)=="empty"
    assert len(calls)==1 and calls[0][1]["chat_id"]==ADMIN


def test_api_and_worker_enforce_both_queue_and_send_keep_filters(setup, monkeypatch):
    engine, settings = setup
    async def idle(*args):
        return
    monkeypatch.setattr(c,"run",idle)
    with TestClient(create_app(settings,engine)) as api:
        command(api,"/start",update=99)
        sid=subscribe(api).json()["id"]
        worker.ingest(engine,[car()]); worker.enqueue(engine)
        with Session(engine) as db:
            assert db.scalar(select(Delivery).where(Delivery.state=="pending"))
            b.control(db).enforce=True; db.commit()
        calls=[]
        assert worker.deliver_one(engine,settings,lambda *x:calls.append(x))=="cancelled"
        assert calls==[]
        worker.ingest(engine,[car("2")]); worker.enqueue(engine)
        with Session(engine) as db:
            assert len(db.scalars(select(Delivery)).all())==1
            assert db.get(Search,sid).enabled is True
        assert api.patch(f"/api/subscriptions/{sid}",headers=headers(),json={"enabled":True}).status_code==402
        assert api.post("/api/cars/search",headers=headers(),json={}).status_code==402
        assert api.post("/api/cars/scans",headers=headers(),json={}).status_code==402
        assert api.get("/api/cars/scans/fixture",headers=headers()).status_code==402
        assert api.get("/api/subscriptions",headers=headers()).status_code==200
        assert api.get("/api/billing/status",headers=headers()).json()["access_available"] is False
        assert api.get("/api/billing/admin",headers=headers()).status_code==403
        assert api.get("/api/billing/admin",headers=headers(ADMIN)).status_code==200
        assert api.get("/api/billing/admin").status_code==401
        assert command(api,"/stop",update=100).status_code==200
        assert api.get("/api/subscriptions",headers=headers()).json()[0]["filters"]


def test_delayed_stop_is_never_swallowed(setup):
    engine, settings=setup
    assert b.handle(engine,settings,event(text="/stop",update_id=90,now=NOW-86400),now=NOW) is None
    with Session(engine) as db:
        assert db.get(MarketingConsent,111).allowed is False


def test_refund_claim_uncertainty_never_repeats_charge_call(setup,monkeypatch):
    engine, settings=setup; opened(setup)
    inv=invoice(setup); b.apply_payment(engine,111,payment(inv),NOW)
    calls=[]
    monkeypatch.setattr(telegram_setup,"call",lambda *a,**k: calls.append(a) or {"uncertain":True})
    handle(setup,text="/billing_admin refund "+inv["payload"],uid=ADMIN,update_id=88)
    assert calls==[]
    handle(setup,text="/billing_admin refund_confirm "+inv["payload"],uid=ADMIN,update_id=89)
    handle(setup,text="/billing_admin refund_confirm "+inv["payload"],uid=ADMIN,update_id=90)
    assert len(calls)==1
    with Session(engine) as db:
        assert db.get(BillingOrder,inv["payload"]).state=="refund_uncertain"
        assert b.allowed(db,111,NOW)


def test_snapshot_offer_mismatch_blocks_advertising(setup):
    engine,settings=ready_campaign(setup)
    with Session(engine) as db:
        b.control(db).offer={**OFFER,"stars":150}; db.commit()
    assert c.tick(engine,settings,sender([]),c.WHEN)=="blocked"


def test_prelaunch_crash_recovery_rechecks_recipient_optout(setup):
    engine,settings=ready_campaign(setup)
    with Session(engine) as db:
        row=db.get(BillingCampaign,c.CAMPAIGN); row.status="running"
        ctrl=b.control(db);ctrl.sales=ctrl.enforce=True
        db.add(CampaignRecipient(campaign_id=c.CAMPAIGN,user_id=111,state="pending"))
        db.get(MarketingConsent,111).allowed=False;db.commit()
    calls=[]
    assert c.tick(engine,settings,sender(calls),c.WHEN)=="excluded"
    assert calls==[]


def test_admin_grant_preserves_later_expiry_and_audit(setup):
    engine,_=setup;opened(setup)
    cmd="/billing_admin grant 111 2026-12-01T00:00:00+00:00 synthetic promised access"
    handle(setup,text=cmd,uid=ADMIN,update_id=88)
    handle(setup,text=cmd,uid=ADMIN,update_id=88)
    handle(setup,text="/billing_admin grant 111 2026-11-01T00:00:00+00:00 shorter gift",uid=ADMIN,update_id=89)
    with Session(engine) as db:
        assert b.date_text(b.expiry(db,111)).startswith("01.12.2026")
        assert len(db.scalars(select(AccessEvent)).all())==2


def test_webhook_forgery_and_group_cannot_buy(setup,monkeypatch):
    engine,settings=setup
    async def idle(*args): return
    monkeypatch.setattr(c,"run",idle)
    with TestClient(create_app(settings,engine)) as api:
        e=event(text="/billing_admin pause",uid=ADMIN,now=time.time())
        assert api.post('/telegram/webhook',json=e).status_code==403
        e['message']['chat']['type']='group'
        assert b.handle(engine,settings,e) is None


def test_post_launch_service_failure_disables_new_restrictions_preserves_paid(setup):
    engine,settings=ready_campaign(setup);calls=[]
    c.tick(engine,settings,sender(calls),c.WHEN)
    with Session(engine) as db:
        db.add(Entitlement(user_id=222,expires_at=c.WHEN+300,updated_at=c.WHEN))
        db.get(MonitorControl,'pilot').heartbeat=0;db.commit()
    assert c.tick(engine,settings,sender(calls),c.WHEN+6)=='blocked'
    with Session(engine) as db:
        assert not b.control(db).enforce and not b.control(db).sales
        assert b.expiry(db,222)==c.WHEN+300
