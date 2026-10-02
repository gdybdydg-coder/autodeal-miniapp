"""Synthetic access audit with SELECT-only SQL and private-field assertions."""
import json

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import billing, manual_access_audit as audit
from backend.billing_models import AccessEvent, CampaignRecipient, Entitlement
from backend.manual_payment_models import PaymentNotice, PaymentRequest
from backend.models import Delivery, DeliveryTiming, Search, User
from backend.tests.test_manual_payments import review, UID, ADMIN, NOW


def test_snapshot_is_bounded_read_only_and_omits_customer_identifiers(review):
    engine, settings, _ = review
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.get(User, UID).ready = True
        db.add(Entitlement(user_id=UID, expires_at=NOW+1000, updated_at=NOW))
        db.scalar(select(Search).where(Search.user_id == UID)).enabled = True
        for i in range(12):
            code = "PRIVATE-ORDER-"+str(i)
            db.add(PaymentRequest(id=code, user_id=UID, name="PRIVATE-NAME", state="approved",
                created_at=NOW+i, updated_at=NOW+i, revision=i, expires_at=NOW+1000,
                receipt_file_id="PRIVATE-RECEIPT", transfer_note="PRIVATE-NOTE"))
            db.add(AccessEvent(id="PRIVATE-EVENT-"+str(i), user_id=UID, actor=ADMIN, kind="manual_paid",
                at=NOW+i, expires_at=NOW+1000, reason="PRIVATE-REASON"))
            db.add(PaymentNotice(id="PRIVATE-NOTICE-"+str(i), request_id=code, user_id=UID, kind="client",
                text="PRIVATE-TEXT", claim="PRIVATE-TOKEN", revision=i, state="sent", attempted_at=NOW+i,
                message_id=12345678))
        db.add(Delivery(id=345, user_id=UID, listing_id=654, state="sent"))
        db.add(DeliveryTiming(delivery_id=345, queued_at=NOW, accepted_at=NOW+1))
        db.add(CampaignRecipient(campaign_id="PRIVATE-CAMPAIGN", user_id=UID, state="failed",
                                 error="403", attempted_at=NOW+2))
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as db:
            result = audit.snapshot(db, UID, NOW)
            assert not db.new and not db.dirty and not db.deleted
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert result["exists"] and result["ready"] and result["allowed"]
    assert result["expires_at"] == NOW+1000
    assert len(result["requests"]) == len(result["access_events"]) == 5
    assert len(result["client_notices"]) == 10
    assert result["client_notices"][0]["message_id_present"] is True
    assert result["searches"] == {"total": 1, "enabled": 1}
    assert result["deliveries"] == {"states": {"sent": 1}, "last_accepted_at": NOW+1}
    assert result["recent_campaign"]["error_code"] == 403
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized and "12345678" not in serialized
    assert "user_id" not in serialized and "request_id" not in serialized and "actor" not in serialized


def test_audit_expiry_guard_requires_verified_owner_and_no_database_work(review, monkeypatch):
    engine, settings, _ = review
    monkeypatch.setenv("MANUAL_PAYMENT_AUDIT_USER_ID", str(UID))
    monkeypatch.setattr(audit.time, "time", lambda: NOW)
    monkeypatch.setattr(audit, "snapshot", lambda *args: (_ for _ in ()).throw(AssertionError("must not read")))
    for expiry in (NOW-1, NOW, NOW+3601, "nan", "inf", "invalid"):
        monkeypatch.setenv("MANUAL_PAYMENT_AUDIT_UNTIL", str(expiry))
        assert audit.log_once(engine, settings) == "disabled"
    monkeypatch.setenv("MANUAL_PAYMENT_AUDIT_UNTIL", str(NOW+60))
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "1")
    assert audit.log_once(engine, settings) == "disabled"


def test_log_once_does_not_expose_target_and_never_mutates(review, monkeypatch):
    engine, settings, _ = review
    monkeypatch.setenv("MANUAL_PAYMENT_AUDIT_USER_ID", str(UID))
    monkeypatch.setenv("MANUAL_PAYMENT_AUDIT_UNTIL", str(NOW+60))
    monkeypatch.setattr(audit.time, "time", lambda: NOW)
    monkeypatch.setattr(audit, "_done", set())
    entries, statements = [], []
    monkeypatch.setattr(audit.LOG, "info", lambda template, value: entries.append(value))
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert audit.log_once(engine, settings) == "logged"
        assert audit.log_once(engine, settings) == "already_logged"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(entries) == 1 and str(UID) not in entries[0]
    assert all(s.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY", "SET LOCAL STATEMENT_TIMEOUT"))
               for s in statements)
