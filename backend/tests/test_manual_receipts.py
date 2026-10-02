"""Real workflow shape with synthetic identities/media/transports only."""
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend import billing, manual_checkout as checkout, manual_payments as m, manual_receipts as receipts
from backend.manual_payment_models import (PaymentRequest, PaymentNotice, PaymentEvidence, ReceiptExpectation,
    ReceiptSubmission, ReceiptNotice, PaymentOwnerConfirmation, BankCredit)
from backend.billing_models import Entitlement, AccessEvent
from backend.tests.test_manual_checkout import bank
from backend.tests.test_manual_payments import review, UID, OTHER, ADMIN, NOW, count, headers


def order(bank):
    return checkout.create(bank[0], bank[1], UID, NOW, checkout.TERMS_VERSION, name="Клієнт")


def message(number=100, uid=UID, **media):
    return {"update_id": number, "message": {"message_id": number, "date": NOW,
        "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, **media}}


def photo(number=100, **media):
    return message(number, photo=[{"file_id": "LARGE-IMAGE", "width": 1600, "height": 1200, "file_size": 10000},
        {"file_id": "SMALL-IMAGE", "width": 80, "height": 60, "file_size": 100}], **media)


def expect(bank, row):
    assert receipts.await_receipt(*bank[:2], UID, row["code"], NOW)["text"] == receipts.PROMPT


def test_full_photo_without_caption_store_owner_card_then_explicit_approval(bank, monkeypatch):
    engine, settings, client = bank
    row = order(bank)
    expect(bank, row)
    expect(bank, row)
    assert count(engine, PaymentRequest) == 1
    assert m.get_status(engine, settings, UID, row["code"])["awaiting_receipt"]
    assert receipts.handle(engine, settings, photo(), NOW+1)["text"] == receipts.ACK
    detail = m.card(engine, settings, ADMIN, row["code"])
    assert detail["state"] == "review" and detail["receipt_file_id"] == "LARGE-IMAGE"
    assert detail["username"] is None and detail["received_at"] == NOW+1
    assert detail["receipt_url"].endswith(row["code"]+"/receipt")
    assert count(engine, Entitlement) == count(engine, BankCredit) == 0
    observed = []
    def sender(token, method, payload, **kwargs):
        with Session(engine) as db:
            assert db.get(PaymentRequest, row["code"]).state == "review"
            assert db.scalar(select(PaymentEvidence)).receipt_file_id == "LARGE-IMAGE"
        observed.append((method, payload))
        return {"ok": True, "result": {"message_id": 901}}
    assert m.deliver_notice(engine, settings, sender, NOW+2) == "sent"
    assert observed[0][0] == "sendPhoto" and observed[0][1]["photo"] == "LARGE-IMAGE"
    assert observed[0][1]["chat_id"] == ADMIN
    assert "До сплати за тарифом: 250 грн" in observed[0][1]["caption"]
    assert "code="+row["code"] in str(observed[0][1]["reply_markup"])
    body = {"code": row["code"], "revision": detail["revision"], "bank_verified": True}
    assert client.post("/api/manual-payments/admin/preview", headers=headers(UID), json=body).status_code == 403
    preview = client.post("/api/manual-payments/admin/preview", headers=headers(ADMIN), json=body)
    assert preview.status_code == 200, preview.text
    token = preview.json()["confirmation"]
    assert client.post("/api/manual-payments/admin/confirm", headers=headers(OTHER), json={"confirmation":token}).status_code == 403
    result = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN), json={"confirmation":token})
    assert result.status_code == 200
    replay = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN), json={"confirmation":token})
    assert replay.json()["replayed"] is True and replay.json()["expires_at"] == result.json()["expires_at"]
    assert count(engine, PaymentOwnerConfirmation) == count(engine, Entitlement) == count(engine, AccessEvent) == 1
    assert count(engine, BankCredit) == 0  # No invented bank operation.
    with Session(engine) as db:
        assert not db.get(ReceiptExpectation, UID).active


@pytest.mark.parametrize("caption", [None, "Дякую", "/довільний текст"])
def test_document_image_optional_caption_and_no_automatic_access(bank, caption):
    row = order(bank); expect(bank, row)
    payload = message(document={"file_id":"DOCUMENT-IMAGE", "mime_type":"image/png", "file_size":12000},
                      **({"caption":caption} if caption is not None else {}))
    assert receipts.handle(*bank[:2], payload, NOW+1)["text"] == receipts.ACK
    assert m.card(*bank[:2], ADMIN, row["code"])["receipt_kind"] == "document"
    calls = []
    def send(token, method, payload, **kwargs):
        calls.append((method, payload)); return {"ok":True,"result":{"message_id":12}}
    assert m.deliver_notice(*bank[:2], send, NOW+2) == "sent"
    assert calls[0][0] == "sendDocument" and calls[0][1]["document"] == "DOCUMENT-IMAGE"
    assert count(bank[0], Entitlement) == count(bank[0], BankCredit) == 0


def test_duplicate_update_and_message_and_corrected_receipt_keep_history(bank):
    engine, settings, _ = bank
    row = order(bank); expect(bank, row)
    for payload in (photo(), photo(), {**photo(), "update_id": 101}):
        assert receipts.handle(engine, settings, payload, NOW+1)["text"] == receipts.ACK
    assert count(engine, ReceiptSubmission) == count(engine, PaymentEvidence) == count(engine, ReceiptNotice) == 1
    correction = photo(102)
    correction["message"]["photo"][0]["file_id"] = "CORRECTED-IMAGE"
    assert receipts.handle(engine, settings, correction, NOW+2)["text"] == receipts.ACK
    assert count(engine, PaymentRequest) == 1 and count(engine, PaymentEvidence) == 2
    with Session(engine) as db:
        assert [r.receipt_file_id for r in db.scalars(select(PaymentEvidence).order_by(PaymentEvidence.revision))] == ["LARGE-IMAGE", "CORRECTED-IMAGE"]
        assert db.get(PaymentRequest,row["code"]).receipt_file_id == "CORRECTED-IMAGE"


def test_waiting_survives_restart_text_does_not_clear_and_commands_keep_working(bank):
    engine, settings, _ = bank
    row = order(bank); expect(bank, row)
    # A fresh engine is sufficient to model process-memory loss (SQLite only).
    fresh = create_engine(engine.url) if engine.dialect.name == "sqlite" else engine
    try:
        assert receipts.handle(fresh, settings, message(text="Я оплатив"), NOW+1)["text"] == receipts.PROMPT
        for command in ("/start", "/stop", "/paysupport допомога"):
            assert receipts.handle(fresh, settings, message(text=command), NOW+1) is None
        assert receipts.handle(fresh, settings, photo(), NOW+2)["text"] == receipts.ACK
    finally:
        if fresh is not engine: fresh.dispose()


def test_unrelated_photo_is_ignored_and_multiple_requests_require_choice(bank):
    engine, settings, _ = bank
    row = order(bank)
    assert receipts.handle(engine, settings, photo(), NOW) is None
    with Session(engine) as db, db.begin():
        db.add(PaymentRequest(id="AD-SYNTH-SECOND", user_id=UID, name="Клієнт", state="created", created_at=NOW+1, updated_at=NOW+1))
        db.add(ReceiptExpectation(user_id=UID, request_id=None, active=True, awaiting_image=True, started_at=NOW, updated_at=NOW))
    result = receipts.handle(engine, settings, photo(), NOW+2)
    assert "Оберіть заявку" in result["text"] and count(engine, PaymentEvidence) == 0
    buttons = result["reply_markup"]["inline_keyboard"]
    assert len(buttons) == 2 and "AD-" not in buttons[0][0]["text"]
    callback = {"update_id":102,"callback_query":{"id":"SYNTH", "from":{"id":UID},
        "message":{"chat":{"id":UID,"type":"private"}},"data":buttons[1][0]["callback_data"]}}
    acknowledgements = []
    def acknowledge(token, method, payload, **kwargs):
        acknowledgements.append(method)
        raise TimeoutError("synthetic callback acknowledgement timeout")
    assert receipts.handle(engine,settings,callback,NOW+3,request=acknowledge)["text"] == receipts.ACK
    assert acknowledgements == ["answerCallbackQuery"]
    with Session(engine) as db:
        assert db.get(PaymentRequest,"AD-SYNTH-SECOND").receipt_file_id == "LARGE-IMAGE"
        assert not db.get(PaymentRequest,row["code"]).receipt_file_id


def test_owner_delivery_failure_retains_queue_and_controlled_retry(bank):
    engine, settings, _ = bank
    row = order(bank); expect(bank,row)
    receipts.handle(engine,settings,photo(),NOW+1)
    assert m.deliver_notice(engine,settings,lambda *a,**k:{"ok":False,"error_code":403},NOW+2) == "failed"
    with Session(engine) as db:
        notice = db.scalar(select(PaymentNotice)); notice_id=notice.id
        assert db.get(PaymentRequest,row["code"]).state == "review"
        assert db.get(ReceiptNotice,notice_id).file_id == "LARGE-IMAGE"
    m.retry_notice(engine,settings,ADMIN,notice_id)
    assert m.deliver_notice(engine,settings,lambda *a,**k:{"ok":True,"result":{"message_id":55}},NOW+3) == "sent"
    assert count(engine, Entitlement) == 0


def test_receipt_proxy_in_admin_list_requires_owner_and_never_returns_token_url(bank, monkeypatch):
    from backend import manual_receipt_proxy
    engine, settings, client = bank
    row=order(bank); expect(bank,row); receipts.handle(engine,settings,photo(),NOW+1)
    calls=[]
    def fetch(settings,file_id,kind):
        calls.append((file_id,kind));return b"\x89PNG\r\n\x1a\nSYNTHETIC", "image/png"
    monkeypatch.setattr(manual_receipt_proxy,"fetch_receipt",fetch)
    path="/api/manual-payments/admin/"+row["code"]+"/receipt"
    assert client.get(path).status_code == 401
    assert client.get(path,headers=headers(UID)).status_code == 403 and calls == []
    result=client.get(path,headers=headers(ADMIN))
    assert result.status_code == 200 and result.headers["content-type"] == "image/png"
    assert "no-store" in result.headers["cache-control"]
    assert calls == [("LARGE-IMAGE","photo")]
    assert settings.bot_token not in result.text and "api.telegram.org" not in result.text


def test_simplified_confirmation_preserves_days_legacy_orders_and_explicit_check(bank):
    engine,settings,_=bank
    row=order(bank)
    legacy=m.report_paid(engine,settings,UID,row["code"],NOW)
    with Session(engine) as db, db.begin():
        db.add(Entitlement(user_id=UID,expires_at=NOW+10000,updated_at=NOW))
    with pytest.raises(m.ReviewError,match="explicit_owner_verification_required"):
        m.preview(engine,settings,ADMIN,row["code"],legacy["revision"],NOW+1)
    p=m.preview(engine,settings,ADMIN,row["code"],legacy["revision"],NOW+1,bank_verified=True)
    result=m.confirm(engine,settings,ADMIN,p["confirmation"],NOW+2)
    assert result["expires_at"] == NOW+10000+30*86400
    assert count(engine,BankCredit) == 0


def test_storage_failure_never_acknowledges_receipt_or_sends_owner_notice(bank):
    from sqlalchemy.exc import OperationalError
    engine,settings,_=bank
    row=order(bank); expect(bank,row)
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith("INSERT INTO manual_receipt_notices"):
            raise OperationalError("synthetic",{},Exception("synthetic"))
    event.listen(engine,"before_cursor_execute",fail)
    try:
        result=receipts.handle(engine,settings,photo(),NOW+1)
    finally:
        event.remove(engine,"before_cursor_execute",fail)
    assert "поки не збережено" in result["text"]
    assert count(engine,PaymentEvidence) == count(engine,ReceiptSubmission) == count(engine,PaymentNotice) == 0
    assert m.get_status(engine,settings,UID,row["code"])["awaiting_receipt"]


def test_legacy_caption_keeps_binding_history_and_owner_media_outbox(bank):
    from backend import manual_payment_routes
    engine,settings,_=bank
    row=order(bank)
    payload=photo(caption="/payment_receipt "+row["code"])
    assert manual_payment_routes.handle(engine,settings,payload)["text"] == receipts.ACK
    assert manual_payment_routes.handle(engine,settings,payload)["text"] == receipts.ACK
    with Session(engine) as db:
        assert db.scalar(select(ReceiptNotice)).file_id == "LARGE-IMAGE"
        assert db.get(PaymentRequest,row["code"]).receipt_file_id == "LARGE-IMAGE"
    assert count(engine,ReceiptSubmission) == count(engine,PaymentNotice) == count(engine,PaymentEvidence) == 1
    payload["message"]["from"]["id"] = OTHER
    payload["message"]["chat"]["id"] = OTHER
    assert "Скриншот отримано" not in manual_payment_routes.handle(engine,settings,payload)["text"]
    assert count(engine,PaymentEvidence) == 1


def test_clarification_reopens_waiting_same_order_and_capabilities(bank):
    engine,settings,client=bank
    row=order(bank); expect(bank,row); receipts.handle(engine,settings,photo(),NOW+1)
    current=m.get_status(engine,settings,UID,row["code"])
    m.change_state(engine,settings,ADMIN,row["code"],"clarification","Надішліть інший скриншот",current["revision"],NOW+2)
    assert m.get_status(engine,settings,UID,row["code"])["awaiting_receipt"]
    assert receipts.handle(engine,settings,message(101,text="Добре"),NOW+3)["text"] == receipts.PROMPT
    assert receipts.handle(engine,settings,photo(102),NOW+4)["text"] == receipts.ACK
    assert count(engine,PaymentRequest) == 1 and count(engine,PaymentEvidence) == 2
    capabilities=receipts.capabilities(engine,client.app)
    assert capabilities["receipt_tables_ready"] and capabilities["owner_receipt_route_registered"]
    assert capabilities["owner_confirmation_route_registered"] and not capabilities["caption_required"]
