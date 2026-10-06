"""Owner-authorized one-shot, fake transport and network-fenced SQLite only."""
from datetime import datetime
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import billing, tariff_reminder_audience as audience
from backend import tariff_reminder_once as once, tariff_reminders as daily_api
from backend.billing_models import BillingCampaign, CampaignRecipient, MarketingConsent, TariffReminderPreference, TariffReminderSchedule
from backend.manual_payment_models import PaymentRequest, ReceiptExpectation
from backend.models import Delivery, User
from backend.tests.test_tariff_reminders import daily, bank, review, UID, OTHER, THIRD, accepted, buyer, no_send

NOW = datetime.fromisoformat("2026-10-06T10:00:00+03:00").timestamp()


@pytest.fixture
def armed(daily, monkeypatch):
    engine, settings, _ = daily
    monkeypatch.setenv(once.ARM_ENV, once.ID)
    with Session(engine) as db, db.begin():
        # The explicitly authorized one-off must work without manufacturing a
        # daily marketing preference for an otherwise ready, unpaid client.
        db.delete(db.get(MarketingConsent, UID))
    return engine, settings


def tick(fixture, transport=no_send, now=NOW):
    return once.tick(*fixture, transport, now, clock=lambda: now)


def view(fixture):
    with Session(fixture[0]) as db:
        return once.summary(db)


def queue(fixture, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(daily_api, "high_priority", lambda db, now: True)
        assert tick(fixture) == "yielding_to_service"


def test_unarmed_creates_no_campaign_and_no_transport(daily):
    assert tick(daily[:2]) == "disabled"
    with Session(daily[0]) as db:
        assert db.get(BillingCampaign, once.ID) is None


def test_group_identifier_cannot_be_an_advertising_recipient(armed):
    with Session(armed[0]) as db, db.begin():
        db.get(User, UID).ready = False
        db.add(User(id=-12345, ready=True))
    assert tick(armed) == "complete"
    assert view(armed)["selected"] == 0


def test_two_current_buyers_and_one_created_request_only_third_is_sent(armed):
    engine, settings = armed
    with Session(engine) as db, db.begin():
        for uid in (OTHER, THIRD):
            user = db.get(User, uid)
            if user is None:
                db.add(User(id=uid, ready=True))
            else:
                user.ready = True
        buyer(db, UID, NOW+86400)
        buyer(db, OTHER, NOW+86400)
        db.add(PaymentRequest(id="UNPAID-CREATED", user_id=THIRD, state="created",
            created_at=NOW-10, updated_at=NOW-10))
    calls = []
    assert tick(armed, accepted(calls)) == "sent"
    assert [c["chat_id"] for c in calls] == [THIRD]
    assert calls[0]["text"] == daily_api.TEXT
    assert calls[0]["reply_markup"]["inline_keyboard"] == daily_api.BUTTONS
    assert tick(armed, now=NOW+2) == "complete"
    assert view(armed)["telegram_accepted"] == 1
    with Session(engine) as db:
        assert audience.count(db, settings, NOW) == 0
        assert db.scalar(select(func.count()).select_from(TariffReminderPreference)) == 0
        assert db.scalar(select(func.count()).select_from(MarketingConsent)) == 0


@pytest.mark.parametrize("change", ["daily_refusal", "global_refusal", "unknown_refusal", "blocked", "stop", "receipt", "review", "gift", "test"])
def test_all_applicable_exclusions_remain(armed, change):
    engine, settings = armed
    with Session(engine) as db, db.begin():
        if change == "daily_refusal":
            db.add(TariffReminderPreference(user_id=UID, enabled=False, update_id=2, at=NOW))
        elif change in ("global_refusal", "unknown_refusal", "blocked"):
            db.add(MarketingConsent(user_id=UID, allowed=change == "blocked", blocked=change == "blocked",
                source="explicit_opt_out" if change == "global_refusal" else "unknown", update_id=2, at=NOW))
        elif change == "stop":
            db.get(User, UID).ready = False
        elif change == "receipt":
            db.add(ReceiptExpectation(user_id=UID, active=True, started_at=NOW, updated_at=NOW))
        elif change == "review":
            db.add(PaymentRequest(id="REVIEW", user_id=UID, state="review", created_at=NOW, updated_at=NOW))
        elif change == "gift":
            from backend.billing_models import Entitlement
            db.add(Entitlement(user_id=UID, expires_at=NOW+100, updated_at=NOW))
        elif change == "test":
            settings = replace(settings, stats_excluded_user_ids=str(UID))
    assert tick((engine, settings)) == "complete"
    assert view(armed)["queued"] == view(armed)["telegram_accepted"] == 0


@pytest.mark.parametrize("change", ["paid", "refusal", "receipt", "stop", "off", "disarm"])
def test_change_after_claim_prevents_transport(armed, monkeypatch, change):
    original = once.promotion.dispatch_claim
    def changed(*args, **kwargs):
        with Session(armed[0]) as db, db.begin():
            if change == "paid": buyer(db, UID, NOW+86400)
            if change == "refusal": db.add(TariffReminderPreference(user_id=UID, enabled=False, update_id=2, at=NOW))
            if change == "receipt": db.add(ReceiptExpectation(user_id=UID, active=True, started_at=NOW, updated_at=NOW))
            if change == "stop": db.get(User, UID).ready = False
            if change == "off": db.get(TariffReminderSchedule, daily_api.ID).enabled = False
        if change == "disarm": monkeypatch.delenv(once.ARM_ENV)
        return original(*args, **kwargs)
    monkeypatch.setattr(once.promotion, "dispatch_claim", changed)
    assert tick(armed) in ("excluded", "deferred")
    assert view(armed)["telegram_accepted"] == 0


def test_once_survives_restart_and_does_not_rearm_or_change_daily_schedule(armed):
    with Session(armed[0]) as db:
        row = db.get(TariffReminderSchedule, daily_api.ID)
        before = (row.next_run_at, row.first_run_at, row.last_campaign_id)
    calls = []
    assert tick(armed, accepted(calls)) == "sent"
    assert tick(armed, now=NOW+2) == "complete"
    for now in (NOW+3, NOW+100, NOW+86400):
        assert tick(armed, now=now) == "complete"
    assert len(calls) == 1
    with Session(armed[0]) as db:
        row = db.get(TariffReminderSchedule, daily_api.ID)
        assert (row.next_run_at, row.first_run_at, row.last_campaign_id) == before


def test_no_duplicate_if_daily_campaign_already_sent_today(armed):
    key = daily_api.PREFIX+"2026-10-06"
    with Session(armed[0]) as db, db.begin():
        db.add(BillingCampaign(id=key, status="complete", not_before=NOW-3600, deadline=NOW-1800,
            timezone="Europe/Kyiv", content={}, audience={}, blockers=[]))
        db.add(CampaignRecipient(campaign_id=key, user_id=UID, state="sent", message_id=123))
    assert tick(armed) == "complete"
    assert view(armed)["selected"] == 0


def test_rate_limit_then_payment_rechecks_before_retry(armed):
    def limited(*args, **kwargs):
        return {"ok":False, "error_code":429, "parameters":{"retry_after":10}}
    assert tick(armed, limited) == "retry"
    with Session(armed[0]) as db, db.begin(): buyer(db, UID, NOW+86400)
    assert tick(armed, now=NOW+12) == "excluded"
    assert tick(armed, now=NOW+14) == "complete"
    assert view(armed)["telegram_accepted"] == 0


def test_uncertain_transport_is_never_repeated(armed):
    def timeout(*args, **kwargs): raise TimeoutError("synthetic")
    assert tick(armed, timeout) == "uncertain"
    assert tick(armed, now=NOW+2) == "complete"
    assert tick(armed, now=NOW+100) == "complete"
    assert view(armed)["uncertain"] == 1


def test_window_expiry_and_disarm_cancel_unattempted_queue(armed, monkeypatch):
    queue(armed, monkeypatch)
    assert tick(armed, now=NOW+1800) == "expired"
    assert view(armed)["expired"] == 1
    assert tick(armed, now=NOW+86400) == "expired"


def test_new_campaign_cannot_start_after_fixed_authorization_expiry(armed):
    assert tick(armed, now=once.EXPIRES) == "not_armed_for_this_date"
    assert view(armed)["installed"] is False


def test_database_failure_defers_instead_of_sending(armed, monkeypatch):
    queue(armed, monkeypatch)
    def unavailable(*args, **kwargs): raise OperationalError("synthetic", {}, Exception())
    monkeypatch.setattr(once, "eligible", unavailable)
    with pytest.raises(OperationalError): tick(armed)
    assert view(armed)["telegram_accepted"] == 0


def test_two_workers_share_one_queue_and_never_duplicate_claims(armed):
    from concurrent.futures import ThreadPoolExecutor
    calls = []
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: tick(armed, accepted(calls)), range(2)))
    assert results.count("sent") == 1
    assert len(calls) == 1
    assert view(armed)["queued"] == 1


def test_day_campaign_continues_and_oneoff_has_no_source_calls(armed, monkeypatch):
    from backend import auto_ria, ria_search, ria_ai_price, valuation
    def forbidden(*args, **kwargs): pytest.fail("Automotive API is forbidden for tariff reminders")
    for module, name in ((auto_ria, "fetch_json"), (ria_search, "fetch_json"),
                         (ria_ai_price, "fetch_quote"), (valuation, "estimate")):
        monkeypatch.setattr(module, name, forbidden)
    calls = []
    daily_api.tick(*armed, accepted(calls), NOW)
    assert len(calls) == 1
    with Session(armed[0]) as db:
        assert db.get(TariffReminderSchedule, daily_api.ID).next_run_at == daily_api.next_morning(NOW)
        assert daily_api.snapshot(db, armed[1], NOW)["immediate"]["telegram_accepted"] == 1


def test_car_delivery_has_priority_and_is_not_modified(armed):
    with Session(armed[0]) as db, db.begin():
        db.add(Delivery(id=987, user_id=UID, listing_id=456, state="pending", retry_at=NOW))
    assert tick(armed) == "yielding_to_service"
    with Session(armed[0]) as db:
        assert db.get(Delivery, 987).state == "pending"
        assert once.summary(db)["pending"] == 1
        assert once.summary(db)["telegram_accepted"] == 0


def test_oneoff_database_error_does_not_suppress_daily_scheduler(armed, monkeypatch):
    def unavailable(*args, **kwargs): raise OperationalError("synthetic", {}, Exception())
    monkeypatch.setattr(once, "tick", unavailable)
    daily_api.tick(*armed, no_send, NOW)
    with Session(armed[0]) as db:
        assert db.get(TariffReminderSchedule, daily_api.ID).next_run_at == daily_api.next_morning(NOW)
