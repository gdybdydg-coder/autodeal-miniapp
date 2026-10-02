"""First-purchase advertising policy; isolated synthetic state and no network."""
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import billing, billing_campaign, manual_checkout, manual_launch, manual_repeat_notice
from backend import subscription_promotion as promotion
from backend.billing_models import (AccessEvent, BillingCampaign, BillingNotice, BillingOrder,
                                    CampaignRecipient, Entitlement, MarketingConsent)
from backend.manual_payment_models import BankCredit, PaymentAudit, PaymentNotice, PaymentRequest, ReceiptExpectation
from backend.models import User, StarsTestOrder
from backend.tests.test_manual_checkout import bank, ready_health
from backend.tests.test_manual_payments import review, ADMIN, UID, OTHER, NOW


@pytest.fixture
def audience(bank, monkeypatch):
    engine, settings, client = bank
    settings = replace(settings, manual_payment_notices_enabled=True, monitor_enabled=True)
    ready_health(engine, monkeypatch)
    monkeypatch.setenv(manual_repeat_notice.ARM_ENV, manual_repeat_notice.CAMPAIGN)
    monkeypatch.setenv("SUBSCRIPTION_LAUNCH_PREPARED", "true")
    monkeypatch.setattr(manual_repeat_notice.manual_repeat_preflight, "ready", lambda db: True)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.get(User, UID).ready = True
        db.add(MarketingConsent(user_id=UID, allowed=True, blocked=False, source="fixture", at=NOW))
    return engine, settings, client


def add_history(db, kind, now=NOW):
    if kind in ("active", "expired"):
        db.add(Entitlement(user_id=UID, expires_at=now+(100 if kind == "active" else -100), updated_at=now))
    elif kind in ("gift", "manual_event", "expired_manual_event"):
        db.add(AccessEvent(id="FIXTURE-GRANT", user_id=UID, actor=ADMIN,
            kind="gift" if kind == "gift" else "manual_paid", at=now-1000,
            expires_at=now+(-100 if kind == "expired_manual_event" else 100), reason="Synthetic"))
    elif kind in ("legacy_paid", "legacy_refunded"):
        db.add(StarsTestOrder(id="FIXTURE-PILOT", user_id=UID, command_update=987,
            state="refunded" if kind.endswith("refunded") else "paid", created_at=now-1000,
            paid_until=now-100, charge_id="FIXTURE-CHARGE"))
    elif kind in ("bank_credit", "manual_approved", "approval_audit", "review", "clarification"):
        db.add(PaymentRequest(id="AD-FIXTURE", user_id=UID, active_user_id=UID,
            state={"manual_approved":"approved", "review":"review", "clarification":"clarification"}.get(kind, "rejected"),
            created_at=now-1000, updated_at=now))
        if kind == "bank_credit":
            db.add(BankCredit(bank_key="FIXTURE-KEY", request_id="AD-FIXTURE", user_id=UID,
                amount_minor=25000, actor=ADMIN, at=now-1000, before_expiry=0, after_expiry=now-100))
        if kind == "approval_audit":
            db.add(PaymentAudit(id="FIXTURE-AUDIT", request_id="AD-FIXTURE", actor=ADMIN,
                action="approved", at=now-1000, revision=1, after_expiry=now-100))
    elif kind == "awaiting_receipt":
        db.add(ReceiptExpectation(user_id=UID, request_id=None, active=True, started_at=now, updated_at=now))
    elif kind in ("order_paid", "order_pending", "order_refunded"):
        db.add(BillingOrder(id="FIXTURE-ORDER", user_id=UID, update_id=123, amount=1,
            terms_version="fixture", created_at=now-1000, state=kind.removeprefix("order_"),
            expires_at=now-100))
    else:
        raise AssertionError(kind)


HISTORY = ("active", "expired", "gift", "manual_event", "expired_manual_event", "legacy_paid",
           "legacy_refunded", "bank_credit", "manual_approved", "approval_audit", "review",
           "clarification", "awaiting_receipt", "order_paid", "order_pending", "order_refunded")


def test_unpaid_is_eligible_and_username_or_created_order_proves_no_payment(audience):
    engine, _, _ = audience
    with Session(engine) as db, db.begin():
        db.add(PaymentRequest(id="AD-UNPAID", user_id=UID, active_user_id=UID, username="fixture",
                              state="created", created_at=NOW, updated_at=NOW))
        assert promotion.eligible(db, UID, NOW)


@pytest.mark.parametrize("history", HISTORY)
def test_all_audiences_exclude_persisted_access_and_pending_payment(audience, history):
    engine, _, _ = audience
    with Session(engine) as db, db.begin():
        add_history(db, history)
    with Session(engine) as db:
        assert not promotion.eligible(db, UID, NOW)
        assert not manual_launch.eligible(db, UID, NOW)
        assert not manual_repeat_notice.eligible(db, UID, NOW)
        assert UID not in billing_campaign.count_audience(db, NOW)[1]


def setup_queue(audience, monkeypatch, campaign, state="pending"):
    engine, settings, _ = audience
    with Session(engine) as db, db.begin():
        db.add(BillingCampaign(id=campaign.CAMPAIGN, not_before=NOW-100, deadline=NOW+1000,
            timezone="Europe/Kyiv", content={"text":"Synthetic first purchase", "button":"Buy",
                "callback_data":manual_checkout.PREFIX+"view"},
            audience={"selected":1}, status="running", blockers=[]))
        db.add(CampaignRecipient(campaign_id=campaign.CAMPAIGN, user_id=UID, state=state,
            attempted_at=NOW-10 if state == "retry" else 0, retry_at=NOW))
    if campaign is billing_campaign:
        # These tests exercise the established scheduler queue and send path,
        # after its separately tested readiness check, without a real API call.
        monkeypatch.setattr(campaign, "readiness", lambda *a: [])
    return engine, settings


@pytest.mark.parametrize("campaign", [billing_campaign, manual_launch, manual_repeat_notice])
@pytest.mark.parametrize("state", ["pending", "retry"])
@pytest.mark.parametrize("history", ["active", "manual_event", "review", "awaiting_receipt", "expired"])
def test_latest_payment_excludes_existing_backlog_and_retries(audience, monkeypatch, campaign, state, history):
    engine, settings = setup_queue(audience, monkeypatch, campaign, state)
    with Session(engine) as db, db.begin():
        add_history(db, history)
    assert campaign.tick(engine, settings, lambda *a, **k: pytest.fail("ineligible ad must not send"), NOW+1) == "excluded"
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == "excluded"


@pytest.mark.parametrize("campaign", [billing_campaign, manual_launch, manual_repeat_notice])
@pytest.mark.parametrize("state", ["pending", "retry"])
def test_payment_read_error_defers_without_send_or_consuming_queue(audience, monkeypatch, campaign, state):
    engine, settings = setup_queue(audience, monkeypatch, campaign, state)
    def unavailable(conn, cursor, statement, parameters, context, executemany):
        if "manual_payment_requests" in statement:
            raise OperationalError("FIXTURE", {}, Exception("synthetic unavailable history"))
    event.listen(engine, "before_cursor_execute", unavailable)
    try:
        with pytest.raises(promotion.EligibilityUnavailable):
            campaign.tick(engine, settings, lambda *a, **k: pytest.fail("unknown is never unpaid"), NOW+1)
    finally:
        event.remove(engine, "before_cursor_execute", unavailable)
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == state


def test_audit_is_read_only_aggregate_and_current(audience, monkeypatch):
    engine, settings = setup_queue(audience, monkeypatch, manual_repeat_notice)
    with Session(engine) as db, db.begin():
        add_history(db, "review")
    result = promotion.audit_queue(engine, settings)
    assert result["queued_eligible"] == 0 and result["queued_excluded"] == 1
    assert result["first_purchase_eligible"] == 0
    assert result["first_purchase_excluded"] == 1
    assert "user_id" not in str(result) and "AD-FIXTURE" not in str(result)
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (manual_repeat_notice.CAMPAIGN, UID)).state == "pending"


def test_audit_missing_payment_table_reports_deferred(audience):
    engine, settings, _ = audience
    ReceiptExpectation.__table__.drop(engine)
    assert promotion.audit_queue(engine, settings) == {
        "status":"deferred", "reason":"subscription_promotion_state_unavailable"}


def test_paid_user_still_receives_transactional_support_reply(audience):
    engine, settings, _ = audience
    with Session(engine) as db, db.begin():
        add_history(db, "active")
        db.add(BillingNotice(id="FIXTURE-SUPPORT", kind="support_reply", user_id=UID,
                             text="Synthetic requested support reply", state="pending"))
    calls = []
    assert billing_campaign.deliver_notice(engine, settings, lambda *a, **k: calls.append(a) or {
        "ok":True,"result":{"message_id":1}}, NOW+1) == "sent"
    assert len(calls) == 1 and calls[0][2]["chat_id"] == UID


@pytest.mark.parametrize("campaign", [billing_campaign, manual_launch, manual_repeat_notice])
@pytest.mark.parametrize("state", ["pending", "retry"])
def test_payment_committed_between_claim_and_dispatch_prevents_transport(audience, monkeypatch, campaign, state):
    engine, settings = setup_queue(audience, monkeypatch, campaign, state)
    guarded = promotion.dispatch_claim
    def approve_after_claim(*args, **kwargs):
        # Deterministic interleaving: original send claim committed, then the
        # owner transaction commits, then guarded dispatch runs. Never mutate
        # the DB from inside a fake sender while the send lock is held.
        with Session(engine) as db, db.begin():
            billing.control(db, lock=True)
            assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == "sending"
            add_history(db, "manual_event")
        return guarded(*args, **kwargs)
    monkeypatch.setattr(promotion, "dispatch_claim", approve_after_claim)
    assert campaign.tick(engine, settings, lambda *a, **k: pytest.fail("paid before transport"), NOW+1) == "excluded"
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == "excluded"


@pytest.mark.parametrize("campaign", [billing_campaign, manual_launch, manual_repeat_notice])
@pytest.mark.parametrize("state", ["pending", "retry"])
def test_dispatch_read_failure_restores_only_a_claim_that_never_started_transport(audience, monkeypatch, campaign, state):
    engine, settings = setup_queue(audience, monkeypatch, campaign, state)
    guarded = promotion.dispatch_claim
    def unavailable(conn, cursor, statement, parameters, context, executemany):
        if "manual_payment_requests" in statement:
            raise OperationalError("FIXTURE", {}, Exception("synthetic dispatch read failure"))
    def fail_after_claim(*args, **kwargs):
        event.listen(engine, "before_cursor_execute", unavailable)
        try:
            return guarded(*args, **kwargs)
        finally:
            event.remove(engine, "before_cursor_execute", unavailable)
    monkeypatch.setattr(promotion, "dispatch_claim", fail_after_claim)
    assert campaign.tick(engine, settings, lambda *a, **k: pytest.fail("unverified must not send"), NOW+1) == "deferred"
    with Session(engine) as db:
        row = db.get(CampaignRecipient, (campaign.CAMPAIGN, UID))
        assert row.state == state
        assert row.attempted_at == (NOW-10 if state == "retry" else 0)
        assert row.retry_at >= NOW+31


@pytest.mark.parametrize("campaign", [billing_campaign, manual_launch, manual_repeat_notice])
def test_persistence_failure_after_transport_never_requeues_unknown_send(audience, monkeypatch, campaign):
    engine, settings = setup_queue(audience, monkeypatch, campaign)
    calls = []
    def unavailable_after_send(conn, cursor, statement, parameters, context, executemany):
        if calls and statement.startswith("UPDATE billing_campaign_recipients"):
            raise OperationalError("FIXTURE", {}, Exception("synthetic post-send write failure"))
    event.listen(engine, "before_cursor_execute", unavailable_after_send)
    try:
        assert campaign.tick(engine, settings, lambda *a, **k: calls.append(1) or {
            "ok":True,"result":{"message_id":1}}, NOW+1) == "uncertain"
    finally:
        event.remove(engine, "before_cursor_execute", unavailable_after_send)
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == "sending"
    campaign.tick(engine, settings, lambda *a, **k: pytest.fail("unknown send must never replay"), NOW+80)
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (campaign.CAMPAIGN, UID)).state == "uncertain"
    assert len(calls) == 1
