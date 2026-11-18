"""The actual signed webhook and HTTP checkout, using only synthetic receipts."""
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import billing, bot_commands, manual_checkout as checkout, manual_payments as payments
from backend import manual_receipts, telegram_setup
from backend.billing_models import AccessEvent, BillingCampaign, Entitlement, MarketingConsent
from backend.api_attempt_audit import RiaApiAttempt
from backend.app import create_app
from backend.manual_payment_models import (BankCredit, PaymentAudit, PaymentEvidence, PaymentNotice,
                                           PaymentOwnerConfirmation, PaymentRequest, ReceiptExpectation,
                                           ReceiptSubmission)
from backend.models import BotReply, Search, User
from backend.radar_models import MIGRATION_VERSION, Radar, RadarMigration, RadarSearch
from backend.radar_monitor import sync_radar
from backend.radar_service import RadarCreate, create as create_radar
from backend.tests.test_backend import SECRET, command, headers
from backend.tests.test_manual_checkout import bank
from backend.tests.test_manual_payments import ADMIN, OTHER, UID, count, review


def send_receipt(client, uid=UID, number=301, kind="photo"):
    media = ({"photo": [{"file_id": "SYNTHETIC-RECEIPT-PHOTO", "width": 1200,
                         "height": 1600, "file_size": 12000}]} if kind == "photo" else
             {"document": {"file_id": "SYNTHETIC-RECEIPT-PDF", "mime_type": "application/pdf",
                            "file_name": "квитанція.pdf", "file_size": 12000}})
    return client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": number, "message": {"message_id": number, "date": int(time.time()),
            "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, **media}})


def accept_into(calls):
    def transport(token, method, payload, **kwargs):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 9000 + len(calls)}}
    return transport


def saved_radars(engine):
    now = time.time()
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.add(RadarMigration(version=MIGRATION_VERSION, applied_at=now))
        db.flush()
        ids = []
        for name in ("На паузі", "Минув оплачений період"):
            value = create_radar(db, UID, RadarCreate(name=name, all_brands=True, brands=[],
                price_category=3, region_ids=[], year_from=2000, year_to=2020, min_profit=10))
            ids.append(value["id"])
        expired = db.get(Radar, ids[1])
        expired.status, expired.active_until = "active", now - 60
        sync_radar(db, expired)
        return ids


@pytest.mark.parametrize("kind", ["photo", "pdf"])
def test_welcome_purchase_receipt_admin_confirmation_end_to_end_without_auto_resume(bank, kind):
    engine, settings, client = bank
    radar_ids = saved_radars(engine)
    calls = []
    assert command(client, "/start", update=100).json() == {"ok": True}
    assert bot_commands.deliver_one(engine, settings, accept_into(calls)) == "sent"
    welcome = calls[0][1]
    assert "250 грн / 30 днів" in welcome["text"]
    assert checkout.TERMS in welcome["text"]
    purchase = welcome["reply_markup"]["inline_keyboard"][-1][0]
    assert purchase["url"].endswith("?start=buy_subscription")
    assert count(engine, PaymentRequest) == count(engine, Entitlement) == 0

    details = command(client, "/start buy_subscription", update=101)
    assert details.status_code == 200
    assert "ФОП Тест &lt;&amp;&gt;" in details.json()["text"]
    assert "Я оплатив" not in details.json()["text"]
    assert "квитанці" in details.json()["text"]
    assert not any(b.get("callback_data", "").startswith(checkout.PREFIX + "paid:")
                   for row in details.json()["reply_markup"]["inline_keyboard"] for b in row)
    state = client.get("/api/billing/status", headers=headers()).json()
    assert state["request"]["state"] == "created" and state["request"]["awaiting_receipt"]
    assert not state["access_available"]
    code = state["request"]["code"]
    command(client, "/start buy_subscription", update=101)
    assert count(engine, PaymentRequest) == 1

    # No paid-button click or receipt command is necessary: the next image/PDF
    # is attached to this user's order and shown to the owner for bank review.
    assert send_receipt(client, kind=kind).json()["text"] == manual_receipts.ACK
    assert send_receipt(client, kind=kind).json()["text"] == manual_receipts.ACK
    assert count(engine, PaymentEvidence) == count(engine, ReceiptSubmission) == 1
    assert count(engine, Entitlement) == count(engine, AccessEvent) == 0
    assert payments.deliver_notice(engine, settings, accept_into(calls), now=time.time()) == "sent"
    method, owner_message = calls[-1]
    assert method == ("sendPhoto" if kind == "photo" else "sendDocument")
    assert owner_message["chat_id"] == ADMIN
    assert owner_message["photo" if kind == "photo" else "document"].startswith("SYNTHETIC-RECEIPT-")
    assert code in str(owner_message["reply_markup"])
    assert client.get("/api/manual-payments/admin/" + code, headers=headers(OTHER)).status_code == 403

    detail = client.get("/api/manual-payments/admin/" + code, headers=headers(ADMIN)).json()
    body = {"code": code, "revision": detail["revision"], "bank_verified": False}
    assert client.post("/api/manual-payments/admin/preview", headers=headers(ADMIN), json=body).status_code == 422
    assert count(engine, Entitlement) == 0
    body["bank_verified"] = True
    assert client.post("/api/manual-payments/admin/preview", headers=headers(), json=body).status_code == 403
    preview = client.post("/api/manual-payments/admin/preview", headers=headers(ADMIN), json=body)
    assert preview.status_code == 200, preview.text
    confirmation = {"confirmation": preview.json()["confirmation"]}
    assert client.post("/api/manual-payments/admin/confirm", headers=headers(OTHER), json=confirmation).status_code == 403
    approved = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN), json=confirmation)
    assert approved.status_code == 200, approved.text
    replay = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN), json=confirmation)
    assert replay.json()["replayed"] and replay.json()["expires_at"] == approved.json()["expires_at"]
    assert count(engine, Entitlement) == count(engine, AccessEvent) == count(engine, PaymentOwnerConfirmation) == 1
    assert count(engine, BankCredit) == 0  # No invented bank transaction ID.
    assert count(engine, MarketingConsent) == count(engine, BillingCampaign) == count(engine, RiaApiAttempt) == 0
    with Session(engine) as db:
        assert db.get(User, UID).ready
        assert db.get(Radar, radar_ids[0]).status == "paused"
        assert db.get(Radar, radar_ids[1]).status == "subscription_expired"
        assert all(not search.enabled for search in db.scalars(select(Search).join(
            RadarSearch, RadarSearch.search_id == Search.id).where(RadarSearch.radar_id.in_(radar_ids))))
        assert not db.get(ReceiptExpectation, UID).active
    assert client.get("/api/billing/status", headers=headers()).json()["access_available"]
    # Telegram retries after approval must not silently open a renewal order.
    command(client, "/start buy_subscription", update=101)
    assert count(engine, PaymentRequest) == 1
    assert count(engine, AccessEvent) == 1


def test_billing_status_discloses_current_offer_and_only_own_request_with_owner_preflight(bank, monkeypatch, request):
    engine, settings, old_client = bank
    # Configuration without the receipt forwarding worker is not ready for
    # public sales, even if the recipient and approved offer are valid.
    assert not old_client.get("/api/billing/status", headers=headers(ADMIN)).json()["checkout_ready"]
    client = TestClient(create_app(replace(settings, manual_payment_notices_enabled=True), engine))
    request.addfinalizer(client.close)
    state = client.get("/api/billing/status", headers=headers()).json()
    assert state["purchase_url"] == "https://t.me/" + telegram_setup.BOT_USERNAME + "?start=buy_subscription"
    assert state["status_url"].endswith("?start=subscribe")
    assert state["terms"] == checkout.TERMS and state["terms_version"] == checkout.TERMS_VERSION
    assert state["request"] is None and "checkout_ready" not in state
    command(client, "/start buy_subscription", update=80)
    assert client.get("/api/billing/status", headers=headers(OTHER)).json()["request"] is None
    with Session(engine) as db, db.begin():
        billing.control(db).sales = False
    owner = client.get("/api/billing/status", headers=headers(ADMIN)).json()
    assert owner["checkout_ready"] and not owner["sales_enabled"]
    assert "recipient" not in owner and "iban" not in str(owner)
    monkeypatch.setenv("MANUAL_PAYMENT_PUBLIC_ENABLED", "false")
    assert not client.get("/api/billing/status", headers=headers(ADMIN)).json()["checkout_ready"]


@pytest.mark.parametrize("payload", ["subscribe", "buy_subscription"])
def test_checkout_start_connects_new_chat_without_marketing_or_stale_stop_override(bank, payload):
    engine, _, client = bank
    uid = 333
    stamp = int(time.time())
    response = command(client, "/start " + payload, uid=uid, update=20, date=stamp)
    assert response.status_code == 200
    with Session(engine) as db:
        user = db.get(User, uid)
        assert user.ready and user.last_update == 20 and user.last_command_at == stamp
        assert db.get(MarketingConsent, uid) is None
        assert not db.scalar(select(BotReply.id).where(BotReply.user_id == uid))  # No second welcome.
    command(client, "/stop", uid=uid, update=21, date=stamp)
    command(client, "/start " + payload, uid=uid, update=20, date=stamp)
    with Session(engine) as db:
        assert not db.get(User, uid).ready and db.get(User, uid).last_update == 21
        assert not db.get(MarketingConsent, uid).allowed
    command(client, "/start " + payload, uid=uid, update=22, date=stamp)
    with Session(engine) as db:
        assert db.get(User, uid).ready and db.get(User, uid).last_update == 22
        assert not db.get(MarketingConsent, uid).allowed


def test_expired_or_wrong_identity_purchase_does_not_register_chat_or_order(bank):
    engine, _, client = bank
    assert command(client, "/start buy_subscription", uid=333, date=int(time.time()) - 301).json() == {"ok": True}
    assert command(client, "/start@another_bot buy_subscription", uid=333).json() == {"ok": True}
    with Session(engine) as db:
        assert db.get(User, 333) is None
    assert count(engine, PaymentRequest) == 0


@pytest.mark.parametrize("active,pending,sales", [(True, False, True), (False, True, True), (False, False, False)])
def test_welcome_purchase_cta_respects_access_pending_order_and_closed_sales(bank, active, pending, sales):
    engine, settings, client = bank
    if pending:
        command(client, "/start buy_subscription", update=40)
    with Session(engine) as db, db.begin():
        billing.control(db).sales = sales
        if active:
            db.add(Entitlement(user_id=UID, expires_at=time.time() + 86400, updated_at=time.time()))
    command(client, "/start", update=41)
    calls = []
    assert bot_commands.deliver_one(engine, settings, accept_into(calls)) == "sent"
    links = [b.get("url", "") for row in calls[0][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert not any("buy_subscription" in link for link in links)
    assert any("start=subscribe" in link for link in links) is pending
