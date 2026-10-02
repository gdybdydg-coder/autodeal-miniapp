"""Synthetic isolated reminders: no real tokens, accounts, or Telegram sends."""
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import billing, billing_campaign, manual_checkout, manual_launch
from backend import manual_repeat_notice as reminder
from backend.billing_models import BillingCampaign, BillingNotice, CampaignRecipient, Entitlement, MarketingConsent
from backend.models import User
from backend.tests.test_manual_checkout import bank, ready_health
from backend.tests.test_manual_payments import review, ADMIN, UID, OTHER, NOW


@pytest.fixture
def armed(bank, monkeypatch):
    engine, settings, client = bank
    settings = replace(settings, manual_payment_notices_enabled=True, monitor_enabled=True)
    ready_health(engine, monkeypatch)
    monkeypatch.setenv(reminder.ARM_ENV, reminder.CAMPAIGN)
    monkeypatch.setattr(reminder.manual_repeat_preflight, "ready", lambda db: True)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.get(User, UID).ready = True
    return engine, settings, client


def record(engine):
    with Session(engine) as db:
        row = db.get(BillingCampaign, reminder.CAMPAIGN)
        counts = dict(db.execute(select(CampaignRecipient.state, func.count()).where(
            CampaignRecipient.campaign_id == reminder.CAMPAIGN).group_by(CampaignRecipient.state)).all())
        return (row.status, row.blockers, row.audience, counts) if row else None


def accepted(calls):
    def send(token, method, payload, **kwargs):
        calls.append(payload)
        assert method == "sendMessage" and kwargs == {"timeout": 5}
        return {"ok": True, "result": {"message_id": len(calls)}}
    return send


@pytest.mark.parametrize("value", [None, "true", manual_launch.CAMPAIGN])
def test_exact_arm_is_required(armed, monkeypatch, value):
    engine, settings, _ = armed
    if value is None:
        monkeypatch.delenv(reminder.ARM_ENV)
    else:
        monkeypatch.setenv(reminder.ARM_ENV, value)
    assert reminder.initialize(engine, settings, NOW) == "disabled"
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("not armed"), NOW) == "disabled"
    assert record(engine) is None


@pytest.mark.parametrize("blocker", ["backup", "sales", "enforce", "owner_proof"])
def test_prerequisite_failure_never_sends_or_rearms(armed, monkeypatch, blocker):
    engine, settings, _ = armed
    if blocker == "backup":
        monkeypatch.delenv("MANUAL_PAYMENT_BACKUP_REFERENCE")
    elif blocker == "owner_proof":
        monkeypatch.setattr(reminder.manual_repeat_preflight, "ready", lambda db: False)
    else:
        with Session(engine) as db, db.begin():
            setattr(billing.control(db), blocker, False)
    assert reminder.initialize(engine, settings, NOW) == "blocked"
    assert record(engine)[1] and record(engine)[3] == {}
    monkeypatch.setenv("MANUAL_PAYMENT_BACKUP_REFERENCE", "libfile_SYNTHETIC_OFFLINE_FIXTURE")
    monkeypatch.setattr(reminder.manual_repeat_preflight, "ready", lambda db: True)
    with Session(engine) as db, db.begin():
        ctrl = billing.control(db)
        ctrl.sales = ctrl.enforce = True
    assert reminder.initialize(engine, settings, NOW+1) == "blocked"
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("blocked"), NOW+2) == "inactive"


def test_one_shot_preserves_previous_campaign_and_paid_access(armed):
    engine, settings, _ = armed
    with Session(engine) as db, db.begin():
        db.add(BillingCampaign(id=manual_launch.CAMPAIGN, not_before=NOW-100,
            deadline=NOW+1000, timezone="Europe/Kyiv", content={"private": "unchanged"},
            audience={"selected": 1}, status="complete", blockers=[]))
        db.add(CampaignRecipient(campaign_id=manual_launch.CAMPAIGN, user_id=UID,
            state="sent", attempted_at=NOW-50, message_id=999))
        db.add(Entitlement(user_id=UID, expires_at=NOW+9000, updated_at=NOW))
    assert reminder.initialize(engine, settings, NOW) == "running"
    assert reminder.initialize(engine, settings, NOW+1) == "running"
    # Paid users are included in this conditional service notice, without
    # suggesting that someone who already paid should make another payment.
    assert record(engine)[3] == {"pending": 1}
    calls = []
    sender = accepted(calls)
    def durable_sender(*args, **kwargs):
        with Session(engine) as db:
            assert db.get(CampaignRecipient, (reminder.CAMPAIGN, UID)).state == "sending"
        return sender(*args, **kwargs)
    assert reminder.tick(engine, settings, durable_sender, NOW+2) == "sent"
    assert reminder.tick(engine, settings, durable_sender, NOW+3) == "yielding"
    assert reminder.tick(engine, settings, durable_sender, NOW+4) == "empty"
    assert reminder.initialize(engine, settings, NOW+5) == "complete"
    assert reminder.tick(engine, settings, durable_sender, NOW+6) == "inactive"
    assert len(calls) == 1 and calls[0]["text"] == reminder.COPY
    assert calls[0]["allow_paid_broadcast"] is False
    assert calls[0]["reply_markup"]["inline_keyboard"] == [[{
        "text": reminder.BUTTON, "callback_data": manual_checkout.PREFIX+"view"}]]
    with Session(engine) as db:
        assert billing.expiry(db, UID) == NOW+9000
        assert billing.control(db).sales and billing.control(db).enforce
        old = db.get(BillingCampaign, manual_launch.CAMPAIGN)
        assert old.status == "complete" and old.content == {"private": "unchanged"}
        old_item = db.get(CampaignRecipient, (manual_launch.CAMPAIGN, UID))
        assert (old_item.state, old_item.message_id, old_item.attempted_at) == ("sent", 999, NOW-50)


def test_snapshot_excludes_stopped_optout_blocked_and_previous_403(armed):
    engine, settings, _ = armed
    with Session(engine) as db, db.begin():
        for uid in (333, 444, 555, 666):
            db.add(User(id=uid, ready=True))
        db.add_all([
            MarketingConsent(user_id=333, allowed=False, blocked=False, source="opt_out", at=NOW),
            MarketingConsent(user_id=444, allowed=True, blocked=True, source="blocked", at=NOW),
            CampaignRecipient(campaign_id=manual_launch.CAMPAIGN, user_id=555, state="failed", error="403"),
            CampaignRecipient(campaign_id=manual_launch.CAMPAIGN, user_id=666, state="failed", error="400"),
        ])
    reminder.initialize(engine, settings, NOW)
    with Session(engine) as db:
        selected = set(db.scalars(select(CampaignRecipient.user_id).where(
            CampaignRecipient.campaign_id == reminder.CAMPAIGN)))
    assert selected == {UID, 666}
    assert record(engine)[2]["selected"] == 2


@pytest.mark.parametrize("change", ["stop", "optout", "previous_403"])
def test_eligibility_is_rechecked_after_snapshot(armed, change):
    engine, settings, _ = armed
    reminder.initialize(engine, settings, NOW)
    with Session(engine) as db, db.begin():
        if change == "stop":
            db.get(User, UID).ready = False
        elif change == "optout":
            db.add(MarketingConsent(user_id=UID, allowed=False, blocked=False, source="opt_out", at=NOW))
        else:
            db.add(CampaignRecipient(campaign_id=manual_launch.CAMPAIGN, user_id=UID, state="failed", error="403"))
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("excluded"), NOW+1) == "excluded"


@pytest.mark.parametrize("result", [{}, None, {"ok": True}, {"error_code": 500}, "timeout"])
def test_unknown_results_are_never_automatically_retried(armed, result):
    engine, settings, _ = armed
    reminder.initialize(engine, settings, NOW)
    calls = []
    def send(*args, **kwargs):
        calls.append(1)
        if result == "timeout":
            raise TimeoutError("synthetic unknown outcome")
        return result
    assert reminder.tick(engine, settings, send, NOW+1) == "uncertain"
    assert reminder.tick(engine, settings, send, NOW+80) == "empty"
    assert reminder.initialize(engine, settings, NOW+81) == "complete"
    assert reminder.tick(engine, settings, send, NOW+82) == "inactive"
    assert len(calls) == 1


def test_scoped_recovery_preserves_other_campaign_and_notices(armed):
    engine, settings, _ = armed
    reminder.initialize(engine, settings, NOW)
    with Session(engine) as db, db.begin():
        db.get(CampaignRecipient, (reminder.CAMPAIGN, UID)).state = "sending"
        db.get(CampaignRecipient, (reminder.CAMPAIGN, UID)).attempted_at = NOW-100
        db.add(CampaignRecipient(campaign_id=manual_launch.CAMPAIGN, user_id=UID,
            state="sending", attempted_at=NOW-100))
        db.add(BillingNotice(id="synthetic-prior-notice", kind="admin", user_id=ADMIN,
            text="Synthetic", state="sending", attempted_at=NOW-100))
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("never replay"), NOW+1) == "empty"
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (reminder.CAMPAIGN, UID)).state == "uncertain"
        assert db.get(CampaignRecipient, (manual_launch.CAMPAIGN, UID)).state == "sending"
        assert db.get(BillingNotice, "synthetic-prior-notice").state == "sending"


@pytest.mark.parametrize("loss", ["profile", "sales", "enforce", "heartbeat"])
def test_readiness_loss_pauses_permanently_without_changing_controls(armed, monkeypatch, loss):
    engine, settings, _ = armed
    reminder.initialize(engine, settings, NOW)
    if loss == "profile":
        monkeypatch.delenv(manual_checkout.subscription_preview.RECIPIENT_ENV)
    elif loss in ("sales", "enforce"):
        with Session(engine) as db, db.begin():
            setattr(billing.control(db), loss, False)
    when = NOW+181 if loss == "heartbeat" else NOW+1
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("readiness lost"), when) == "paused"
    assert reminder.initialize(engine, settings, when+1) == "paused"
    assert record(engine)[3] == {"pending": 1}
    with Session(engine) as db:
        assert billing.control(db).sales == (loss != "sales")
        assert billing.control(db).enforce == (loss != "enforce")


def test_explicit_rate_limit_waits_and_then_retries(armed):
    engine, settings, _ = armed
    reminder.initialize(engine, settings, NOW)
    assert reminder.tick(engine, settings, lambda *a, **k: {
        "error_code": 429, "parameters": {"retry_after": 10}}, NOW+1) == "retry"
    assert reminder.tick(engine, settings, lambda *a, **k: pytest.fail("too early"), NOW+5) == "empty"
    calls = []
    assert reminder.tick(engine, settings, accepted(calls), NOW+12) == "sent"
    assert len(calls) == 1
