"""Direct purchase, durable receipt waiting, and unchanged explicit bank approval.

Synthetic identities, bank fixture and injected transport only; no real sends.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import billing, manual_checkout as c, manual_payments as m, manual_receipts as receipts
from backend.billing_models import Entitlement, AccessEvent
from backend.manual_payment_models import (
    PaymentRequest, PaymentAudit, PaymentNotice, PaymentEvidence, ReceiptExpectation,
    ReceiptSubmission, ReceiptNotice,
)
from backend.models import User
from backend.radar_models import Radar, RadarMigration, MIGRATION_VERSION
from backend.tests.test_manual_checkout import bank
from backend.tests.test_manual_payments import review, ADMIN, UID, OTHER, NOW, count
from backend.tests.test_manual_receipts import photo, message


def purchase(bank, *, uid=UID, update=None, now=NOW):
    return c.create(*bank[:2], uid, now, c.TERMS_VERSION, telegram_update_id=update)


def test_purchase_arms_receipt_without_paid_button_or_access(bank):
    engine, settings, _ = bank
    row = purchase(bank)
    assert row["state"] == "created" and row["awaiting_receipt"]
    assert purchase(bank, now=NOW+1)["code"] == row["code"]
    with Session(engine) as db:
        expectation = db.get(ReceiptExpectation, UID)
        assert expectation.request_id == row["code"] and expectation.awaiting_image
        assert expectation.started_at == expectation.updated_at == NOW
        assert not billing.expiry(db, UID) and not db.get(User, UID).ready
        saved = db.get(PaymentRequest, row["code"])
        assert saved.terms_version == c.TERMS_VERSION and saved.terms_text == c.TERMS
    detail = c.details(engine, settings, UID, row["code"])
    assert "До сплати: 250 грн" in detail["text"] and "PDF" in detail["text"]
    assert "Переказ уже зроблено" not in detail["text"]
    assert "Я оплатив" not in str(detail["reply_markup"])
    assert count(engine, PaymentNotice) == count(engine, Entitlement) == 0
    assert receipts.handle(engine, settings, photo(), NOW+2)["text"] == receipts.ACK
    assert m.get_status(engine, settings, UID, row["code"])["state"] == "review"


def test_purchase_and_expectation_registration_roll_back_together(bank):
    engine, settings, _ = bank
    unknown = 333
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO manual_receipt_expectations"):
            raise OperationalError("synthetic", {}, Exception("fixture write failed"))
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            purchase(bank, uid=unknown)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with Session(engine) as db:
        assert db.get(User, unknown) is None
    assert count(engine, PaymentRequest) == count(engine, ReceiptExpectation) == count(engine, PaymentAudit) == 0


@pytest.mark.parametrize("problem", ["closed", "missing_recipient", "changed_offer", "wrong_terms", "null_terms"])
def test_unavailable_purchase_does_not_register_or_arm(bank, monkeypatch, problem):
    engine, settings, _ = bank
    version = c.TERMS_VERSION
    if problem == "closed":
        monkeypatch.setenv("MANUAL_PAYMENT_PUBLIC_ENABLED", "false")
    elif problem == "missing_recipient":
        monkeypatch.delenv(c.subscription_preview.RECIPIENT_ENV)
    elif problem == "changed_offer":
        with Session(engine) as db, db.begin():
            billing.control(db).offer = {**c.OFFER, "amount_minor": 99999}
    else:
        version = "old" if problem == "wrong_terms" else None
    with pytest.raises(m.ReviewError):
        c.create(engine, settings, 333, NOW, version)
    with Session(engine) as db:
        assert db.get(User, 333) is None
    assert count(engine, PaymentRequest) == count(engine, ReceiptExpectation) == 0


def test_legacy_details_button_arms_existing_request_and_records_explicit_terms(bank):
    engine, settings, _ = bank
    legacy = m.create_request(engine, settings, UID, NOW)
    assert not legacy["awaiting_receipt"]
    c.details(engine, settings, UID, legacy["code"])
    assert m.get_status(engine, settings, UID, legacy["code"])["awaiting_receipt"]
    # An authenticated, explicit new purchase records the current terms on the
    # same unfinished legacy request, preserving amount and recipient behavior.
    current = purchase(bank, now=NOW+2)
    assert current["code"] == legacy["code"]
    with Session(engine) as db:
        assert db.get(PaymentRequest, current["code"]).terms_version == c.TERMS_VERSION


def test_repeated_purchase_preserves_review_evidence_and_receipt_state(bank):
    engine, settings, _ = bank
    row = purchase(bank)
    receipts.handle(engine, settings, photo(), NOW+1)
    before = m.card(engine, settings, ADMIN, row["code"])
    with Session(engine) as db:
        before_time = db.get(ReceiptExpectation, UID).updated_at
    for now in (NOW+2, NOW+3):
        replay = purchase(bank, now=now)
        assert replay["code"] == row["code"] and replay["state"] == "review"
        assert not replay["awaiting_receipt"]
        assert "Очікує перевірки" in c.request_card(engine, settings, UID, row["code"])["text"]
    assert m.card(engine, settings, ADMIN, row["code"]) == before
    with Session(engine) as db:
        assert db.get(ReceiptExpectation, UID).updated_at == before_time
    assert count(engine, ReceiptSubmission) == count(engine, PaymentEvidence) == count(engine, PaymentNotice) == 1


def test_webhook_purchase_retry_after_approval_never_creates_renewal(bank):
    engine, settings, _ = bank
    row = purchase(bank, update=456)
    receipts.handle(engine, settings, photo(), NOW+1)
    reviewed = m.get_status(engine, settings, UID, row["code"])
    token = m.preview(engine, settings, ADMIN, row["code"], reviewed["revision"], NOW+2,
                      bank_verified=True)["confirmation"]
    result = m.confirm(engine, settings, ADMIN, token, NOW+3)
    replay = purchase(bank, update=456, now=NOW+4)
    assert replay["code"] == row["code"] and replay["state"] == "approved"
    assert not replay["awaiting_receipt"] and replay["expires_at"] == result["expires_at"]
    assert count(engine, PaymentRequest) == count(engine, Entitlement) == count(engine, AccessEvent) == 1
    assert m.confirm(engine, settings, ADMIN, token, NOW+5)["replayed"]
    # A different, explicit purchase can renew without modifying the approval.
    new = purchase(bank, update=457, now=NOW+6)
    assert new["code"] != row["code"] and new["awaiting_receipt"]


def test_concurrent_checkout_buttons_share_one_request_and_expectation(bank):
    engine, settings, _ = bank
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda n: purchase(bank, update=1000+n), range(4)))
    assert len({r["code"] for r in rows}) == 1 and all(r["awaiting_receipt"] for r in rows)
    assert count(engine, PaymentRequest) == count(engine, ReceiptExpectation) == 1


def test_direct_pdf_is_stored_once_and_forwarded_without_download(bank):
    engine, settings, _ = bank
    row = purchase(bank)
    pdf = message(document={"file_id": "FIXTURE-PDF", "mime_type": "application/pdf", "file_size": 1024})
    assert receipts.handle(engine, settings, pdf, NOW+1)["text"] == receipts.ACK
    assert receipts.handle(engine, settings, pdf, NOW+1)["text"] == receipts.ACK
    assert count(engine, PaymentEvidence) == count(engine, ReceiptNotice) == 1
    calls = []
    def transport(token, method, body, **kwargs):
        calls.append((method, body))
        return {"ok": True, "result": {"message_id": 123}}
    assert m.deliver_notice(engine, settings, transport, NOW+2) == "sent"
    assert calls[0][0] == "sendDocument" and calls[0][1]["document"] == "FIXTURE-PDF"
    assert calls[0][1]["chat_id"] == ADMIN
    assert m.get_status(engine, settings, UID, row["code"])["state"] == "review"
    assert count(engine, Entitlement) == 0


@pytest.mark.parametrize("mime,size", [("application/pdf", 10*1024*1024+1), ("application/pdf", 0),
                                      ("application/x-msdownload", 1024)])
def test_oversized_or_wrong_document_does_not_change_request(bank, mime, size):
    row = purchase(bank)
    result = receipts.handle(*bank[:2], message(document={"file_id": "BAD", "mime_type": mime,
                                                        "file_size": size}), NOW+1)
    assert receipts.ACK != result["text"]
    assert m.get_status(*bank[:2], UID, row["code"])["state"] == "created"
    assert count(bank[0], PaymentEvidence) == count(bank[0], PaymentNotice) == 0


@pytest.mark.parametrize("command", ["/start", "/stop", "/subscription", "/paysupport допомога"])
def test_armed_receipt_never_swallows_commands(bank, command):
    purchase(bank)
    assert receipts.handle(*bank[:2], message(text=command), NOW+1) is None


@pytest.mark.parametrize("old_until,expected_status", [(NOW-1, "subscription_expired"), (NOW+100, "active")])
def test_bank_approval_preserves_manual_resume_and_continuous_access(bank, old_until, expected_status):
    engine, settings, _ = bank
    with Session(engine) as db, db.begin():
        db.add(RadarMigration(version=MIGRATION_VERSION, applied_at=NOW-100))
        db.add(Entitlement(user_id=UID, expires_at=old_until, updated_at=NOW-100))
        db.add(Radar(id="a"*32, user_id=UID, slot=1, name="Тест", price_category=2,
                     year_from=2000, year_to=2026, min_profit=10, status="active",
                     active_until=old_until, created_at=NOW-100, updated_at=NOW-100))
    row = purchase(bank)
    receipts.handle(engine, settings, photo(), NOW+1)
    current = m.get_status(engine, settings, UID, row["code"])
    token = m.preview(engine, settings, ADMIN, row["code"], current["revision"], NOW+2,
                      bank_verified=True)["confirmation"]
    result = m.confirm(engine, settings, ADMIN, token, NOW+3)
    with Session(engine) as db:
        radar = db.get(Radar, "a"*32)
        assert radar.status == expected_status and radar.name == "Тест"
        assert radar.active_until == (result["expires_at"] if expected_status == "active" else 0)
        assert not db.get(User, UID).ready
