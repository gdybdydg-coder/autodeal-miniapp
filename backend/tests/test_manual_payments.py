"""Synthetic money, isolated databases and signed fixture Telegram identities only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from backend import billing, manual_payments as m, manual_checkout
from backend.app import Settings, create_app
from backend.billing_models import BillingControl, Entitlement, AccessEvent
from backend.manual_payment_models import (ManualBase, PaymentRequest, BankCredit,
                                          PaymentAudit, PaymentNotice)
from backend.models import Base, User, Search, StarsTestOrder
from backend.tests.test_backend import TOKEN, SECRET, headers, command

ADMIN, UID, OTHER = 987654321, 111, 222
NOW = 1790922000


@pytest.fixture(params=["sqlite"] + (["postgresql"] if os.getenv("AUTODEAL_TEST_POSTGRES_URL") else []))
def review(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(ADMIN))
    monkeypatch.delenv("SUBSCRIPTION_LAUNCH_PREPARED", raising=False)
    monkeypatch.delenv("MANUAL_PAYMENT_PUBLIC_ENABLED", raising=False)
    monkeypatch.delenv("MANUAL_PAYMENT_LAUNCH_STAGE", raising=False)
    admin_engine = schema = None
    if request.param == "postgresql":
        url = make_url(os.environ["AUTODEAL_TEST_POSTGRES_URL"])
        # Never accept DATABASE_URL or a production host/account. Each test owns
        # a fresh schema inside an explicitly named, disposable localhost DB.
        if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1"
                or url.database != "autodeal_manual_fixture" or url.username != "autodeal_fixture"
                or url.password != "fixture-only" or url.query or not url.port):
            pytest.fail("Expected the isolated localhost manual-payment fixture database")
        admin_engine = create_engine(url, connect_args={"connect_timeout": 5})
        schema = "manual_fixture_" + uuid.uuid4().hex
        with admin_engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA "'+schema+'"'))
        url = url.update_query_dict({"options": "-csearch_path="+schema+" -cstatement_timeout=15000"})
        engine = create_engine(url, connect_args={"connect_timeout": 5})
    else:
        url = "sqlite:///" + str(tmp_path / "review.sqlite")
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    settings = Settings(url, TOKEN, SECRET, True, True, admin_telegram_id=ADMIN,
                        manual_payment_review_enabled=True)
    m.initialize(engine, settings)
    with Session(engine) as db, db.begin():
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=False, offer={}))
        db.add_all([User(id=u, ready=False) for u in (ADMIN, UID, OTHER)])
        db.add(Search(user_id=UID, fingerprint="fixture", name="Saved filter", filters={"brand": "BMW"},
                      enabled=False, after_listing=123))
    client = TestClient(create_app(settings, engine))
    yield engine, settings, client
    client.close()
    engine.dispose()
    if admin_engine is not None:
        with admin_engine.begin() as conn:
            conn.execute(text('DROP SCHEMA "'+schema+'" CASCADE'))
        admin_engine.dispose()


def pending(review, uid=UID, now=NOW):
    engine, settings, _ = review
    row = m.create_request(engine, settings, uid, now)
    return m.report_paid(engine, settings, uid, row["code"], now+1)


def preview(review, row, now=NOW+2, operation="FIXTURE-CREDIT-1", **changes):
    engine, settings, _ = review
    data = dict(account="FIXTURE-ACCOUNT", operation=operation, actual_amount_minor=25000, bank_verified=True)
    data.update(changes)
    return m.preview(engine, settings, ADMIN, row["code"], row["revision"], now, **data)


def count(engine, model):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(model))


def test_public_sales_closed_even_when_review_enabled(review):
    engine, settings, client = review
    r = client.post("/api/manual-payments", headers=headers(), json={"terms_version":manual_checkout.TERMS_VERSION})
    assert r.status_code == 409 and "manual_sales_closed" in r.text
    assert count(engine, PaymentRequest) == 0
    with Session(engine) as db:
        ctrl = billing.control(db)
        assert not ctrl.sales and not ctrl.enforce


def test_full_signed_api_workflow_with_synthetic_bank_offer(review, monkeypatch):
    engine, settings, client = review
    monkeypatch.setenv("MANUAL_PAYMENT_PUBLIC_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAYMENT_REVIEW_ENABLED", "true")
    monkeypatch.setattr(manual_checkout.subscription_preview, "receiving_profile", lambda: {"iban":"SYNTHETIC"})
    with Session(engine) as db, db.begin():
        ctrl=billing.control(db); ctrl.offer=dict(manual_checkout.OFFER); ctrl.sales=True
    body={"terms_version":manual_checkout.TERMS_VERSION}
    row = client.post("/api/manual-payments", headers=headers(), json=body).json()
    code = row["code"]
    assert row["amount_minor"] == 25000 and row["days"] == 30
    assert client.post("/api/manual-payments", headers=headers(), json=body).json()["code"] == code
    path = "/api/manual-payments/" + code
    paid = client.post(path + "/paid", headers=headers(), json={}).json()
    assert paid["state"] == "review" and "user_id" not in paid
    assert client.post(path + "/paid", headers=headers(), json={}).json() == paid
    assert count(engine, PaymentNotice) == 1
    card = client.get("/api/manual-payments/admin/"+code, headers=headers(ADMIN)).json()
    assert card["name"] == "Test" and card["username"] is None
    pre = client.post("/api/manual-payments/admin/preview", headers=headers(ADMIN), json={
        "code": code, "revision": paid["revision"], "account": "FIXTURE-ACCOUNT", "operation": "FIXTURE-1",
        "actual_amount_minor": 25000, "bank_verified": True})
    assert pre.status_code == 200, pre.text
    result = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN),
                         json={"confirmation": pre.json()["confirmation"]})
    assert result.status_code == 200
    until = result.json()["expires_at"]
    with Session(engine) as db:
        assert billing.expiry(db, UID) == until
        ctrl = billing.control(db); ctrl.enforce = True; db.commit()
        assert billing.allowed(db, UID, until-1)
        assert not billing.allowed(db, UID, until)
        assert not db.get(User, UID).ready
        search = db.scalar(select(Search))
        assert not search.enabled and search.after_listing == 123 and search.filters == {"brand": "BMW"}
    assert client.get(path, headers=headers()).json()["state"] == "approved"
    assert count(engine, AccessEvent) == 1


def test_missing_forged_identity_and_foreign_ownership(review):
    _, _, client = review
    code = pending(review)["code"]
    assert client.get("/api/manual-payments/admin").status_code == 401
    forged = headers(); forged["X-Telegram-Init-Data"] += "&user_id=987654321"
    assert client.get("/api/manual-payments/admin", headers=forged).status_code == 401
    assert client.get("/api/manual-payments/admin", headers=headers()).status_code == 403
    assert client.get("/api/manual-payments/"+code, headers=headers(OTHER)).status_code == 404
    assert client.post("/api/manual-payments/"+code+"/paid", headers=headers(), json={"user_id": ADMIN}).status_code == 422
    assert client.post("/api/manual-payments/"+code+"/paid", headers=headers(), json={"receipt_file_id": "foreign"}).status_code == 422


def test_owner_configuration_rechecked_for_old_confirmation(review, monkeypatch):
    engine, settings, client = review
    p = preview(review, pending(review))
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(OTHER))
    result = client.post("/api/manual-payments/admin/confirm", headers=headers(ADMIN), json={"confirmation": p["confirmation"]})
    assert result.status_code == 503
    assert count(engine, BankCredit) == 0


@pytest.mark.parametrize("actual", [24900, 25100])
def test_underpayment_and_overpayment_do_not_grant(review, actual):
    row = pending(review)
    with pytest.raises(m.ReviewError, match="amount_requires_clarification"):
        preview(review, row, actual_amount_minor=actual)
    assert count(review[0], Entitlement) == 0
    assert m.get_status(*review[:2], UID, row["code"])["state"] == "review"


def test_clarify_resubmit_reject_and_exact_retries(review):
    engine, settings, _ = review
    row = pending(review)
    p = preview(review, row)
    changed = m.change_state(engine, settings, ADMIN, row["code"], "clarification", "Уточніть переказ", row["revision"], NOW+3)
    assert changed == m.change_state(engine, settings, ADMIN, row["code"], "clarification", "Уточніть переказ", row["revision"], NOW+4)
    with pytest.raises(m.ReviewError):
        m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+4)
    changed = m.report_paid(engine, settings, UID, row["code"], NOW+5, transfer_note="Синтетичне уточнення")
    rejected = m.change_state(engine, settings, ADMIN, row["code"], "rejected", "Не знайдено", changed["revision"], NOW+6)
    assert rejected["state"] == "rejected"
    assert count(engine, Entitlement) == 0
    with Session(engine) as db:
        notices = db.scalars(select(PaymentNotice).where(PaymentNotice.kind == "client")).all()
        assert "не означає повернення" in notices[-1].text
    assert m.create_request(engine, settings, UID, NOW+7)["code"] != row["code"]


def test_concurrent_create_and_concurrent_confirmation(review):
    engine, settings, _ = review
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: m.create_request(engine, settings, UID, NOW), range(4)))
    assert len({r["code"] for r in rows}) == 1
    row = m.report_paid(engine, settings, UID, rows[0]["code"], NOW+1)
    p = preview(review, row)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3), range(4)))
    assert sum(not r["replayed"] for r in results) == 1
    assert count(engine, BankCredit) == count(engine, Entitlement) == count(engine, AccessEvent) == 1


def test_concurrent_manual_and_existing_owner_grant_preserve_access(review):
    engine, settings, _ = review
    row = pending(review)
    p = preview(review, row)
    gift_until = NOW+40*m.DAY
    iso = datetime.fromtimestamp(gift_until, timezone.utc).isoformat()
    def gift():
        with Session(engine) as db, db.begin():
            billing.admin_command(db, settings, ADMIN,
                f"/billing_admin grant {UID} {iso} synthetic-fixture", 9876, NOW+3)
    def manual():
        try:
            return m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
        except m.ReviewError as exc:
            # A grant winning the shared lock invalidates the old preview;
            # the owner must review the new expiry before confirming again.
            assert exc.code == "request_or_access_changed"
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(gift), pool.submit(manual)
        a.result(); result = b.result()
    if result is None:
        p = preview(review, row, NOW+4)
        result = m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+5)
        assert result["expires_at"] == gift_until+30*m.DAY
    with Session(engine) as db:
        assert billing.expiry(db, UID) >= gift_until
        assert not db.scalar(select(Search)).enabled
    assert count(engine, BankCredit) == 1
    assert count(engine, AccessEvent) == 2


def test_different_tokens_and_same_credit_different_users(review):
    engine, settings, _ = review
    row = pending(review); other = pending(review, OTHER)
    p1, p2 = preview(review, row), preview(review, row)
    p3 = preview(review, other)
    m.confirm(engine, settings, ADMIN, p1["confirmation"], NOW+3)
    for p in (p2, p3):
        with pytest.raises(m.ReviewError):
            m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    assert count(engine, BankCredit) == 1
    with pytest.raises(m.ReviewError, match="bank_credit_already_used"):
        preview(review, other, account="fixture-account", operation="fixture-credit-1")


def test_renewal_preserves_legacy_paid_and_gift_and_restart(review):
    engine, settings, _ = review
    with Session(engine) as db, db.begin():
        db.add(Entitlement(user_id=UID, expires_at=NOW+5000, updated_at=NOW))
        db.add(StarsTestOrder(id="fixture-legacy", user_id=UID, command_update=991,
                             created_at=NOW, state="paid", paid_until=NOW+9000, charge_id="fixture-paid"))
    p = preview(review, pending(review))
    first = m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    assert first["expires_at"] == NOW+9000+30*m.DAY
    second = preview(review, pending(review, now=NOW+5), now=NOW+7, operation="FIXTURE-CREDIT-2")
    m.confirm(engine, settings, ADMIN, second["confirmation"], NOW+8)
    reopened = create_engine(engine.url)
    m.initialize(reopened, settings)
    with Session(reopened) as db:
        assert billing.expiry(db, UID) == NOW+9000+60*m.DAY
        assert db.get(StarsTestOrder, "fixture-legacy").paid_until == NOW+9000
        assert not db.scalar(select(Search)).enabled
    reopened.dispose()


def test_changed_access_evidence_or_expired_preview_requires_new_confirmation(review):
    engine, settings, _ = review
    row = pending(review); p = preview(review, row)
    with pytest.raises(m.ReviewError):
        m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+302)
    m.report_paid(engine, settings, UID, row["code"], NOW+3, transfer_note="Late evidence")
    with pytest.raises(m.ReviewError):
        m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+4)
    row = m.get_status(engine, settings, UID, row["code"]); p = preview(review, row, NOW+5)
    with Session(engine) as db, db.begin():
        db.add(Entitlement(user_id=UID, expires_at=NOW+700, updated_at=NOW+6))
    with pytest.raises(m.ReviewError):
        m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+7)
    assert count(engine, BankCredit) == 0


def test_rollback_if_notice_insert_fails_no_partial_access(review):
    engine, settings, _ = review
    row = pending(review); p = preview(review, row)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO manual_payment_notices"):
            raise OperationalError("fixture failure", {}, None)
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert count(engine, BankCredit) == count(engine, Entitlement) == count(engine, AccessEvent) == 0
    assert m.get_status(engine, settings, UID, row["code"])["state"] == "review"
    assert not m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+4)["replayed"]


def test_outbox_failed_notification_never_undoes_access_and_retry_only_notice(review):
    engine, settings, _ = review
    p = preview(review, pending(review)); m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    with Session(engine) as db, db.begin():
        db.query(PaymentNotice).filter(PaymentNotice.kind == "owner").delete()
    calls = []
    def failed(token, method, payload, **kw):
        calls.append(payload); return {"ok": False, "error_code": 403}
    assert m.deliver_notice(engine, settings, failed, NOW+5) == "failed"
    with Session(engine) as db:
        item = db.scalar(select(PaymentNotice)); key = item.id
        expiry = billing.expiry(db, UID)
    m.retry_notice(engine, settings, ADMIN, key)
    def accepted(token, method, payload, **kw):
        calls.append(payload); return {"ok": True, "result": {"message_id": 1}}
    assert m.deliver_notice(engine, settings, accepted, NOW+6) == "sent"
    assert all(c["chat_id"] == UID and c["allow_paid_broadcast"] is False for c in calls)
    with Session(engine) as db:
        assert billing.expiry(db, UID) == expiry
    assert count(engine, BankCredit) == 1


def test_outbox_timeout_and_crash_are_uncertain_not_replayed(review):
    engine, settings, _ = review
    pending(review)
    def timeout(*args, **kw):
        raise TimeoutError("synthetic")
    assert m.deliver_notice(engine, settings, timeout, NOW+3) == "uncertain"
    assert m.deliver_notice(engine, settings, timeout, NOW+4) == "empty"
    with Session(engine) as db, db.begin():
        row = db.scalar(select(PaymentNotice)); key = row.id
        row.state, row.attempted_at = "sending", NOW
    assert m.deliver_notice(engine, settings, timeout, NOW+65) == "empty"
    with pytest.raises(m.ReviewError):
        m.retry_notice(engine, settings, ADMIN, key)


def test_outbox_429_honors_delay(review):
    engine, settings, _ = review
    pending(review)
    assert m.deliver_notice(engine, settings, lambda *a, **k: {"ok": False, "error_code": 429,
        "parameters": {"retry_after": 10}}, NOW+2) == "retry"
    assert m.deliver_notice(engine, settings, lambda *a, **k: pytest.fail("too early"), NOW+12) == "empty"
    assert m.deliver_notice(engine, settings, lambda *a, **k: {"ok": True,
        "result": {"message_id": 1}}, NOW+13) == "sent"


def test_queued_old_statuses_never_arrive_after_approval(review):
    engine, settings, _ = review
    row = pending(review)
    m.change_state(engine, settings, ADMIN, row["code"], "clarification",
                   "Уточніть переказ", row["revision"], NOW+2)
    row = m.report_paid(engine, settings, UID, row["code"], NOW+3, transfer_note="Уточнення")
    p = preview(review, row, NOW+4)
    m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+5)
    sent = []
    def accepted(token, method, payload, **kw):
        sent.append(payload)
        return {"ok": True, "result": {"message_id": 1}}
    assert m.deliver_notice(engine, settings, accepted, NOW+6) == "sent"
    assert m.deliver_notice(engine, settings, accepted, NOW+7) == "empty"
    assert len(sent) == 1 and sent[0]["chat_id"] == UID
    assert "підтверджено власником" in sent[0]["text"]
    with Session(engine) as db:
        statuses = list(db.scalars(select(PaymentNotice.state)))
        assert statuses.count("superseded") == 3 and statuses.count("sent") == 1


def test_superseding_does_not_hide_an_uncertain_send(review):
    engine, settings, _ = review
    row = pending(review)
    assert m.deliver_notice(engine, settings, lambda *a, **k: {}, NOW+2) == "uncertain"
    p = preview(review, row, NOW+3)
    m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+4)
    assert m.deliver_notice(engine, settings, lambda *a, **k: {
        "ok": True, "result": {"message_id": 2}}, NOW+5) == "sent"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(PaymentNotice).where(
            PaymentNotice.state == "uncertain")) == 1


def test_queue_pages_filters_literal_search_and_missing_username(review, monkeypatch):
    # Numeric search also covers request codes. Keep random hex IDs from
    # accidentally containing another fixture user's numeric ID.
    from itertools import count as sequence
    tokens = sequence(1)
    monkeypatch.setattr(m.secrets, "token_hex", lambda size: f"{next(tokens):0{size*2}x}")
    engine, settings, _ = review
    with Session(engine) as db, db.begin():
        db.add_all([User(id=i, ready=False) for i in range(1000, 1053)])
    for i in range(1000, 1053):
        m.create_request(engine, settings, i, NOW+i, name="Percent%" if i == 1000 else "Клієнт")
    pages = [m.queue(engine, settings, ADMIN, state="all", page=p) for p in range(1, 4)]
    assert pages[0]["total"] == 53 and pages[0]["pages"] == 3
    assert len({r["code"] for q in pages for r in q["items"]}) == 53
    assert m.queue(engine, settings, ADMIN, state="all", search="%")["total"] == 1
    assert m.queue(engine, settings, ADMIN, state="all", search="1002")["total"] == 1
    assert m.queue(engine, settings, ADMIN)["total"] == 0


def test_registered_webhook_owner_command_and_late_receipt(review):
    engine, settings, client = review
    row = pending(review); code = row["code"]
    assert "Заявки" in command(client, "/payments all 1", uid=ADMIN).json()["text"]
    assert "Заявки" not in command(client, "/payments all 1").json()["text"]
    update = {"update_id": 900, "message": {"from": {"id": UID}, "chat": {"id": UID, "type": "private"},
        "caption": "/payment_receipt " + code, "date": NOW,
        "document": {"mime_type": "application/pdf", "file_size": 500, "file_id": "SYNTHETIC-RECEIPT"}}}
    h = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    assert client.post("/telegram/webhook", json=update).status_code == 403
    assert "Квитанцію додано" in client.post("/telegram/webhook", headers=h, json=update).json()["text"]
    assert "Квитанцію додано" in client.post("/telegram/webhook", headers=h, json=update).json()["text"]
    assert m.card(engine, settings, ADMIN, code)["receipt_file_id"] == "SYNTHETIC-RECEIPT"
    assert count(engine, PaymentNotice) == 2  # Initial paid report and one late receipt.
    view = command(client, "/start paymentreceipt_"+code, uid=ADMIN).json()
    assert view["method"] == "sendDocument" and view["document"] == "SYNTHETIC-RECEIPT"
    assert view["protect_content"] is True
    assert "document" not in command(client, "/start paymentreceipt_"+code, uid=OTHER).json()
    update["message"]["from"]["id"] = OTHER
    update["message"]["chat"]["id"] = OTHER
    assert "Квитанцію додано" not in client.post("/telegram/webhook", headers=h, json=update).json()["text"]


def test_only_owner_can_retry_known_failure_over_http(review):
    engine, settings, client = review
    code = pending(review)["code"]
    with Session(engine) as db, db.begin():
        row = db.scalar(select(PaymentNotice)); key = row.id; row.state = "failed"
    body = {"notice_id": key}
    assert client.post("/api/manual-payments/admin/retry-notice", headers=headers(), json=body).status_code == 403
    result = client.get("/api/manual-payments/admin/"+code+"/notices", headers=headers(ADMIN)).json()
    assert result[0]["state"] == "failed" and "text" not in result[0]
    assert client.post("/api/manual-payments/admin/retry-notice", headers=headers(ADMIN), json=body).status_code == 200
    assert client.post("/api/manual-payments/admin/retry-notice", headers=headers(ADMIN), json=body).status_code == 409


def test_customer_without_access_can_check_paid_request_and_stop(review):
    engine, settings, client = review
    code = pending(review)["code"]
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
    assert client.get("/api/manual-payments/"+code, headers=headers()).status_code == 200
    assert client.post("/api/manual-payments/"+code+"/paid", headers=headers(), json={}).status_code == 200
    assert command(client, "/stop", update=1234).status_code == 200
    with Session(engine) as db:
        assert not db.scalar(select(Search)).enabled
        assert db.scalar(select(Search)).filters == {"brand": "BMW"}


def test_confirmation_of_two_distinct_credits_preserves_both_periods(review):
    engine, settings, _ = review
    row = pending(review); p = preview(review, row)
    m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    second = pending(review, now=NOW+4)
    p = preview(review, second, NOW+6, operation="SECOND-CREDIT")
    m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+7)
    with Session(engine) as db:
        assert billing.expiry(db, UID) == NOW+3+60*m.DAY
        history = list(db.scalars(select(AccessEvent)))
        assert len(history) == 2


def test_feature_flags_do_not_accept_user_input_and_default_to_off(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("MANUAL_PAYMENT_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("MANUAL_PAYMENT_NOTICES_ENABLED", raising=False)
    assert not Settings.env().manual_payment_review_enabled
    assert not Settings.env().manual_payment_notices_enabled
    monkeypatch.setenv("MANUAL_PAYMENT_REVIEW_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAYMENT_NOTICES_ENABLED", "true")
    assert Settings.env().manual_payment_review_enabled and Settings.env().manual_payment_notices_enabled
    assert not m.public_creation_allowed()


def test_closed_request_does_not_claim_new_receipt_was_added(review):
    engine, settings, client = review
    row = pending(review); p = preview(review, row)
    m.confirm(engine, settings, ADMIN, p["confirmation"], NOW+3)
    result = client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, json={
        "update_id": 999, "message": {"from": {"id": UID}, "chat": {"id": UID, "type": "private"},
        "date": NOW+4, "caption": "/payment_receipt "+row["code"],
        "document": {"file_size": 100, "mime_type": "application/pdf", "file_id": "LATE-CLOSED-FIXTURE"}}})
    assert "Нову квитанцію не додано" in result.json()["text"]
    assert not m.card(engine, settings, ADMIN, row["code"])["receipt_file_id"]


def test_missing_control_fails_closed_and_schema_initialization_preserves_requests(review):
    engine, settings, _ = review
    row = pending(review)
    m.initialize(engine, settings)
    assert m.get_status(engine, settings, UID, row["code"])["state"] == "review"
    with Session(engine) as db, db.begin():
        db.delete(db.get(BillingControl, billing.CONTROL))
    with pytest.raises(m.ReviewError, match="access_control_unavailable"):
        preview(review, row)
    assert count(engine, BankCredit) == 0


def test_notice_runner_only_starts_under_both_feature_flags(review, monkeypatch):
    engine, settings, _ = review
    calls = []
    async def stub(engine, settings, stop):
        calls.append(True)
        await stop.wait()
    monkeypatch.setattr(m, "run_notices", stub)
    for review_flag, notices_flag in ((False, True), (True, False), (True, True)):
        calls.clear()
        config = replace(settings, manual_payment_review_enabled=review_flag, manual_payment_notices_enabled=notices_flag)
        with TestClient(create_app(config, engine)) as client:
            assert client.get("/health").status_code == 200
        assert bool(calls) == (review_flag and notices_flag)


def test_disabled_feature_creates_no_tables_and_has_no_command_effect(tmp_path):
    engine = create_engine("sqlite:///"+str(tmp_path/"disabled.sqlite"))
    settings = Settings(str(engine.url), TOKEN, SECRET)
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/manual-payments/admin", headers=headers(ADMIN)).status_code == 404
        assert command(client, "/payments", uid=ADMIN).json() == {"ok": True}
    assert not any(t.startswith("manual_") for t in inspect(engine).get_table_names())
    engine.dispose()


def test_database_failure_is_503_not_unpaid_or_raw_sql(review, monkeypatch):
    _, _, client = review
    def unavailable(*args, **kwargs):
        raise OperationalError("PRIVATE SQL", {"receipt": "PRIVATE"}, None)
    monkeypatch.setattr(m, "queue", unavailable)
    result = client.get("/api/manual-payments/admin", headers=headers(ADMIN))
    assert result.status_code == 503 and "do_not_pay_again" in result.text
    assert "PRIVATE" not in result.text


def test_postgres_ddl_compiles_additively_without_existing_table_changes():
    for table in ManualBase.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE manual_" in sql and "DROP" not in sql and "ALTER" not in sql
    assert "UNIQUE (request_id)" in str(CreateTable(BankCredit.__table__).compile(dialect=postgresql.dialect()))
