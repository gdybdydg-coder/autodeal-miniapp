"""Persistent daily reminders on isolated synthetic databases and fixture sends."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import multiprocessing
import os
from threading import Barrier, Lock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import billing, manual_checkout, manual_payments, subscription_promotion
from backend import tariff_reminders as reminders
from backend.billing_models import (AccessEvent, BillingCampaign, BillingNotice, CampaignRecipient,
                                    Entitlement, MarketingConsent, TariffReminderPreference,
                                    TariffReminderSchedule)
from backend.manual_payment_models import PaymentNotice, PaymentRequest, ReceiptExpectation
from backend.models import BotReply, Delivery, Search, User
from backend.tests.test_manual_checkout import bank
from backend.tests.test_manual_payments import review, ADMIN, UID, OTHER


def at(value):
    return datetime.fromisoformat(value).timestamp()


BEFORE = at("2026-10-03T08:59:00+03:00")
MORNING = at("2026-10-03T09:00:00+03:00")
KEY = reminders.PREFIX + "2026-10-03"
THIRD = 333


@pytest.fixture
def daily(bank):
    engine, settings, client = bank
    settings = replace(settings, manual_payment_notices_enabled=True)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.get(User, UID).ready = True
        db.add(MarketingConsent(user_id=UID, allowed=True, blocked=False, at=BEFORE, update_id=1,
                              source="explicit_marketing_button_v1"))
    assert reminders.initialize(engine, settings, BEFORE) == "enabled"
    return engine, settings, client


def no_send(*args, **kwargs):
    pytest.fail("No reminder transport is permitted in this state")


def accepted(calls):
    def send(token, method, payload, **kwargs):
        assert method == "sendMessage"
        assert kwargs == {"timeout": 5}
        assert payload["allow_paid_broadcast"] is False
        calls.append(payload)
        return {"ok": True, "result": {"message_id": len(calls)}}
    return send


def snapshot(engine, settings, now=MORNING):
    with Session(engine) as db:
        return reminders.snapshot(db, settings, now)


def queue_without_transport(daily, monkeypatch, now=MORNING):
    engine, settings, _ = daily
    with monkeypatch.context() as patch:
        patch.setattr(reminders, "high_priority", lambda db, now: True)
        assert reminders.tick(engine, settings, no_send, now) == "yielding_to_service"


def buyer(db, uid, until=MORNING+86400):
    db.add(PaymentRequest(id="SYNTHETIC-" + str(uid), user_id=uid, state="approved",
                          created_at=BEFORE-100, updated_at=BEFORE, expires_at=until))


@pytest.mark.parametrize("current, expected", [
    ("2026-10-03T08:59:59+03:00", "2026-10-03T09:00:00+03:00"),
    ("2026-10-03T09:00:00+03:00", "2026-10-04T09:00:00+03:00"),
    ("2026-10-03T20:00:00+03:00", "2026-10-04T09:00:00+03:00"),
    ("2026-10-24T09:01:00+03:00", "2026-10-25T09:00:00+02:00"),
    ("2026-10-25T08:59:00+02:00", "2026-10-25T09:00:00+02:00"),
    ("2026-03-28T09:01:00+02:00", "2026-03-29T09:00:00+03:00"),
    ("2026-03-29T08:59:00+03:00", "2026-03-29T09:00:00+03:00"),
])
def test_next_morning_is_strictly_future_and_uses_kyiv_dst(current, expected):
    actual = reminders.next_morning(at(current))
    assert actual == at(expected)
    assert actual > at(current)
    assert datetime.fromtimestamp(actual, ZoneInfo("Europe/Kyiv")).hour == 9


@pytest.mark.parametrize("day, utc_offset", [("2026-03-29", 3), ("2026-10-25", 2)])
def test_bounded_morning_window_follows_dst(day, utc_offset):
    now = at(f"{day}T09:15:00+0{utc_offset}:00")
    begin, end = reminders.bounds(now)
    assert end - begin == reminders.WINDOW_MINUTES * 60
    assert datetime.fromtimestamp(begin, reminders.ZONE).hour == 9
    assert datetime.fromtimestamp(end, reminders.ZONE).strftime("%H:%M") == "09:30"


def test_installation_is_future_only_and_restart_does_not_reset_schedule(daily):
    engine, settings, _ = daily
    assert reminders.tick(engine, settings, no_send, BEFORE+59) == "scheduled"
    assert reminders.initialize(engine, settings, MORNING+60) == "enabled"
    data = snapshot(engine, settings)
    assert data["installed"] and data["enabled"]
    assert data["first_run_at"] == data["next_run_at"] == MORNING
    assert data["next_run_kyiv"] == "2026-10-03T09:00:00+03:00"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BillingCampaign)) == 0
        assert db.scalar(select(func.count()).select_from(CampaignRecipient)) == 0


def test_installing_after_nine_does_not_send_late_or_create_today_queue(bank):
    engine, settings, _ = bank
    settings = replace(settings, manual_payment_notices_enabled=True)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
    assert reminders.initialize(engine, settings, MORNING+60) == "enabled"
    assert reminders.tick(engine, settings, no_send, MORNING+61) == "scheduled"
    assert snapshot(engine, settings)["next_run_at"] == at("2026-10-04T09:00:00+03:00")


def test_three_clients_two_confirmed_purchases_only_third_gets_reminder(daily):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.get(User, OTHER).ready = True
        db.add(User(id=THIRD, ready=True))
        db.add_all([MarketingConsent(user_id=u, allowed=True, blocked=False, at=BEFORE, update_id=1,
                    source="explicit_marketing_button_v1") for u in (OTHER, THIRD)])
        buyer(db, UID)
        buyer(db, OTHER, until=MORNING-100)  # Former buyers are never first-purchase targets.
    calls = []
    assert reminders.tick(engine, settings, accepted(calls), MORNING) == "sent"
    assert [r["chat_id"] for r in calls] == [THIRD]
    assert calls[0]["text"] == reminders.TEXT
    assert calls[0]["reply_markup"]["inline_keyboard"] == reminders.BUTTONS
    assert reminders.BUTTONS[0][0]["callback_data"] == manual_checkout.PREFIX + "view"
    assert "OLX" not in calls[0]["text"]
    assert reminders.tick(engine, settings, no_send, MORNING+2) == "complete"
    data = snapshot(engine, settings, MORNING+2)
    assert data["eligible_recipients"] == 1
    assert data["last_result"]["selected"] == data["last_result"]["sent"] == 1
    assert data["last_result"]["telegram_confirmation_is_read_receipt"] is False


@pytest.mark.parametrize("state", ["review", "clarification", "awaiting_screenshot"])
def test_payment_submission_or_review_excludes_reminder(daily, state):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.add(PaymentRequest(id="SYNTHETIC-WAIT", user_id=UID,
            state="created" if state == "awaiting_screenshot" else state,
            created_at=BEFORE, updated_at=BEFORE))
        if state == "awaiting_screenshot":
            db.add(ReceiptExpectation(user_id=UID, request_id="SYNTHETIC-WAIT", active=True,
                                     started_at=BEFORE, updated_at=BEFORE))
    assert reminders.tick(engine, settings, no_send, MORNING) == "complete"
    assert snapshot(engine, settings)["last_result"]["selected"] == 0


@pytest.mark.parametrize("change", ["paid", "opt_out", "stop", "campaign_off"])
def test_changes_after_claim_are_checked_before_actual_transport(daily, monkeypatch, change):
    engine, settings, _ = daily
    guarded = subscription_promotion.dispatch_claim
    def change_after_claim(*args, **kwargs):
        with Session(engine) as db:
            assert db.get(CampaignRecipient, (KEY, UID)).state == "sending"
        if change == "opt_out":
            reminders.set_preference(engine, settings, UID, False, 1000, MORNING)
        elif change == "campaign_off":
            reminders.set_enabled(engine, settings, ADMIN, False, MORNING)
        else:
            with Session(engine) as db, db.begin():
                billing.control(db, lock=True)
                if change == "paid":
                    buyer(db, UID)
                else:
                    db.get(User, UID).ready = False
        return guarded(*args, **kwargs)
    monkeypatch.setattr(subscription_promotion, "dispatch_claim", change_after_claim)
    outcome = reminders.tick(engine, settings, no_send, MORNING)
    assert outcome in {"excluded", "uncertain"}
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (KEY, UID)).state in {"excluded", "uncertain"}


def test_opt_out_survives_restart_and_does_not_change_paid_car_search(daily):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.add(Entitlement(user_id=UID, expires_at=MORNING+86400, updated_at=BEFORE))
        row = db.scalar(select(Search).where(Search.user_id == UID))
        row.enabled = True
        before = row.filters, row.after_listing
    assert reminders.set_preference(engine, settings, UID, False, 100, MORNING) is False
    assert reminders.initialize(engine, settings, MORNING+1) == "enabled"
    with Session(engine) as db:
        assert db.get(TariffReminderPreference, UID).enabled is False
        assert billing.allowed(db, UID, MORNING+1)
        assert db.get(User, UID).ready is True
        row = db.scalar(select(Search).where(Search.user_id == UID))
        assert row.enabled and (row.filters, row.after_listing) == before
        assert db.get(MarketingConsent, UID).allowed is True
    assert reminders.tick(engine, settings, no_send, MORNING+1) == "complete"
    assert snapshot(engine, settings)["last_result"]["selected"] == 0


@pytest.mark.parametrize("priority", ["car", "command", "payment", "support", "support_reply", "refund_review"])
def test_existing_car_and_payment_messages_have_priority(daily, priority):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        if priority == "car":
            db.add(Delivery(user_id=UID, listing_id=12345, state="pending"))
        elif priority == "command":
            db.add(BotReply(user_id=UID, command_at=int(MORNING), update_id=444,
                            command="help", state="pending"))
        elif priority == "payment":
            db.add(PaymentNotice(id="SYNTHETIC-PRIORITY", request_id="SYNTHETIC", revision=0,
                                 kind="approved", user_id=OTHER, text="Fixture", state="pending"))
        elif priority == "refund_review":
            db.add(BillingNotice(id="refund-review:SYNTHETIC", kind="admin", user_id=ADMIN,
                                 text="Fixture refund review", state="pending"))
        else:
            db.add(BillingNotice(id="SYNTHETIC-SUPPORT", kind=priority, user_id=OTHER,
                                 text="Fixture", state="pending"))
    assert reminders.tick(engine, settings, no_send, MORNING) == "yielding_to_service"
    with Session(engine) as db:
        item = db.get(CampaignRecipient, (KEY, UID))
        assert item.state == "pending" and item.attempted_at == 0


def test_retry_after_stops_whole_campaign_and_rechecks_payment(daily):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.get(User, OTHER).ready = True
        db.add(MarketingConsent(user_id=OTHER, allowed=True, blocked=False, at=BEFORE, update_id=1,
                              source="explicit_marketing_button_v1"))
    calls = []
    def limited(*args, **kwargs):
        calls.append(1)
        return {"ok": False, "error_code": 429, "parameters": {"retry_after": 10}}
    assert reminders.tick(engine, settings, limited, MORNING) == "retry"
    assert reminders.tick(engine, settings, no_send, MORNING+5) == "paced"
    with Session(engine) as db, db.begin():
        buyer(db, UID)
    assert reminders.tick(engine, settings, no_send, MORNING+11) == "excluded"
    sent = []
    assert reminders.tick(engine, settings, accepted(sent), MORNING+12) == "sent"
    assert [r["chat_id"] for r in sent] == [OTHER]
    assert len(calls) == 1


@pytest.mark.parametrize("newer_result", ["rate_limited", "slow_success"])
def test_delayed_claim_cannot_bypass_a_newer_workers_global_pause(daily, monkeypatch, newer_result):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.get(User, OTHER).ready = True
        db.add(MarketingConsent(user_id=OTHER, allowed=True, blocked=False,
            source="explicit_marketing_button_v1", update_id=1, at=BEFORE))
    guarded = subscription_promotion.dispatch_claim
    old_clock, newer_clock, calls, interleaved = [MORNING], [MORNING+2], [], []
    def newer_sender(token, method, payload, **kwargs):
        assert method == "sendMessage" and payload["chat_id"] == OTHER
        calls.append(payload["chat_id"])
        if newer_result == "rate_limited":
            return {"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}
        newer_clock[0] = MORNING+7  # A slow but confirmed transport completion.
        return {"ok": True, "result": {"message_id": 123}}
    def newer_worker_before_old_dispatch(*args, **kwargs):
        if args[3] == UID and not interleaved:
            interleaved.append(True)
            with Session(engine) as db:
                assert db.get(CampaignRecipient, (KEY, UID)).state == "sending"
            outcome = reminders.tick(engine, settings, newer_sender, clock=lambda: newer_clock[0])
            assert outcome == ("retry" if newer_result == "rate_limited" else "sent")
            old_clock[0] = newer_clock[0]
        return guarded(*args, **kwargs)
    monkeypatch.setattr(subscription_promotion, "dispatch_claim", newer_worker_before_old_dispatch)
    assert reminders.tick(engine, settings, no_send, clock=lambda: old_clock[0]) == "deferred"
    assert calls == [OTHER]
    expected_pause = MORNING + (63 if newer_result == "rate_limited" else 9)
    with Session(engine) as db:
        assert db.get(BillingCampaign, KEY).next_send == expected_pause
        delayed = db.get(CampaignRecipient, (KEY, UID))
        assert delayed.state == "pending" and delayed.attempted_at == 0
        assert delayed.retry_at >= old_clock[0]+30
    assert reminders.tick(engine, settings, no_send, expected_pause-1) == "paced"
    if newer_result == "rate_limited":
        resumed = []
        assert reminders.tick(engine, settings, accepted(resumed), expected_pause) == "sent"
        assert [payload["chat_id"] for payload in resumed] == [UID]
    else:
        assert reminders.tick(engine, settings, no_send, expected_pause) == "waiting_retry"
        resumed = []
        assert reminders.tick(engine, settings, accepted(resumed), MORNING+37) == "sent"
        assert [payload["chat_id"] for payload in resumed] == [UID]


@pytest.mark.parametrize("response", [{}, None, {"ok": True}, {"error_code": 500}, "timeout"])
def test_ambiguous_outcome_is_quarantined_without_blind_retry(daily, response):
    engine, settings, _ = daily
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(1)
        if response == "timeout":
            raise TimeoutError("Synthetic transport timeout")
        return response
    assert reminders.tick(engine, settings, uncertain, MORNING) == "uncertain"
    assert reminders.tick(engine, settings, no_send, MORNING+60) == "complete"
    assert reminders.tick(engine, settings, no_send, MORNING+600) == "scheduled"
    assert len(calls) == 1
    assert snapshot(engine, settings)["last_result"]["uncertain"] == 1


def test_morning_window_expires_retries_and_never_sends_in_evening(daily):
    engine, settings, _ = daily
    assert reminders.tick(engine, settings, lambda *a, **k: {
        "ok": False, "error_code": 429, "parameters": {"retry_after": 2000}}, MORNING) == "retry"
    assert reminders.tick(engine, settings, no_send, MORNING+1800) == "scheduled"
    assert reminders.tick(engine, settings, no_send, at("2026-10-03T21:00:00+03:00")) == "scheduled"
    data = snapshot(engine, settings)
    assert data["last_result"]["status"] == "expired"
    assert data["last_result"]["expired"] == 1


def test_long_outage_skips_missed_days_instead_of_catching_up(daily):
    engine, settings, _ = daily
    recovered = at("2026-10-06T09:10:00+03:00")
    calls = []
    assert reminders.tick(engine, settings, accepted(calls), recovered) == "sent"
    assert len(calls) == 1
    assert snapshot(engine, settings, recovered)["next_run_at"] == at("2026-10-07T09:00:00+03:00")
    with Session(engine) as db:
        assert list(db.scalars(select(BillingCampaign.id))) == [reminders.PREFIX + "2026-10-06"]


def test_long_outage_recovered_after_morning_window_does_not_send(daily):
    engine, settings, _ = daily
    recovered = at("2026-10-06T19:00:00+03:00")
    assert reminders.tick(engine, settings, no_send, recovered) == "scheduled"
    assert snapshot(engine, settings, recovered)["next_run_at"] == at("2026-10-07T09:00:00+03:00")
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BillingCampaign)) == 0


def test_admin_disable_cancels_queue_and_reenable_is_future_only(daily, monkeypatch):
    engine, settings, _ = daily
    queue_without_transport(daily, monkeypatch)
    disabled = reminders.set_enabled(engine, settings, ADMIN, False, MORNING+1)
    assert not disabled["enabled"] and disabled["next_run_at"] is None
    assert disabled["last_result"]["status"] == "cancelled"
    assert disabled["last_result"]["excluded"] == 1
    assert reminders.tick(engine, settings, no_send, MORNING+2) == "disabled"
    assert reminders.initialize(engine, settings, MORNING+3) == "disabled"
    enabled = reminders.set_enabled(engine, settings, ADMIN, True, MORNING+4)
    assert enabled["next_run_at"] == at("2026-10-04T09:00:00+03:00")
    assert reminders.tick(engine, settings, no_send, MORNING+5) == "scheduled"


def test_non_admin_cannot_change_schedule_or_read_audience(daily, monkeypatch):
    engine, settings, _ = daily
    monkeypatch.setattr(reminders.audience, "count", lambda *args: pytest.fail("No unauthorized audience read"))
    with pytest.raises(manual_payments.ReviewError, match="owner_only"):
        reminders.set_enabled(engine, settings, UID, False, MORNING)
    with Session(engine) as db:
        assert db.get(TariffReminderSchedule, reminders.ID).enabled is True


def test_two_workers_create_one_daily_campaign_and_do_not_duplicate_send(daily):
    engine, settings, _ = daily
    barrier, lock, calls = Barrier(2), Lock(), []
    sender = accepted(calls)
    def send(*args, **kwargs):
        with lock:
            return sender(*args, **kwargs)
    def worker():
        barrier.wait(timeout=10)
        return reminders.tick(engine, settings, send, MORNING)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        outcomes = [f.result(timeout=15) for f in futures]
    assert outcomes.count("sent") == 1
    assert len(calls) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BillingCampaign)) == 1
        assert db.scalar(select(func.count()).select_from(CampaignRecipient)) == 1
        assert db.get(CampaignRecipient, (KEY, UID)).state == "sent"
    assert reminders.tick(engine, settings, no_send, MORNING+2) == "complete"
    assert reminders.tick(engine, settings, no_send, MORNING+30) == "scheduled"


def isolated_process_worker(url, settings, barrier, output):
    """A separate OS process owns a new connection pool and fixture transport."""
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    try:
        barrier.wait(timeout=10)
        def send(token, method, payload, **kwargs):
            assert method == "sendMessage" and kwargs == {"timeout": 5}
            assert payload["allow_paid_broadcast"] is False
            output.put(("transport", os.getpid(), payload["chat_id"]))
            return {"ok": True, "result": {"message_id": 456}}
        outcome = reminders.tick(engine, settings, send, MORNING)
        output.put(("outcome", os.getpid(), outcome))
    except Exception as error:
        output.put(("error", os.getpid(), type(error).__name__))
    finally:
        engine.dispose()


def test_two_os_processes_with_separate_pools_share_one_daily_campaign(daily):
    engine, settings, _ = daily
    if engine.dialect.name != "sqlite":
        pytest.skip("Explicitly checks the disposable SQLite process fixture")
    context = multiprocessing.get_context("fork")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=isolated_process_worker,
        args=(str(engine.url), settings, barrier, output)) for _ in range(2)]
    records = []
    try:
        for process in processes:
            process.start()
        while sum(record[0] in {"outcome", "error"} for record in records) < 2:
            records.append(output.get(timeout=15))
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert not any(record[0] == "error" for record in records)
        outcomes = [record for record in records if record[0] == "outcome"]
        transports = [record for record in records if record[0] == "transport"]
        assert len({record[1] for record in outcomes}) == 2
        assert [record[2] for record in outcomes].count("sent") == 1
        assert [record[2] for record in transports] == [UID]
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(BillingCampaign)) == 1
            assert db.scalar(select(func.count()).select_from(CampaignRecipient)) == 1
            assert db.get(CampaignRecipient, (KEY, UID)).state == "sent"
        assert reminders.tick(engine, settings, no_send, MORNING+2) == "complete"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()
        output.join_thread()


def test_schedule_and_recipient_progress_survive_new_engine(daily, monkeypatch):
    engine, settings, _ = daily
    queue_without_transport(daily, monkeypatch)
    reopened = create_engine(engine.url, connect_args={"check_same_thread": False, "timeout": 30})
    try:
        assert reminders.initialize(reopened, settings, MORNING+1) == "enabled"
        assert snapshot(reopened, settings)["next_run_at"] == at("2026-10-04T09:00:00+03:00")
        sent = []
        assert reminders.tick(reopened, settings, accepted(sent), MORNING+2) == "sent"
        assert reminders.tick(reopened, settings, no_send, MORNING+4) == "complete"
        assert len(sent) == 1
    finally:
        reopened.dispose()
    assert reminders.tick(engine, settings, no_send, MORNING+5) == "scheduled"


def test_crashed_claim_recovers_uncertain_without_replaying_other_campaign(daily, monkeypatch):
    engine, settings, _ = daily
    queue_without_transport(daily, monkeypatch)
    with Session(engine) as db, db.begin():
        item = db.get(CampaignRecipient, (KEY, UID))
        item.state, item.attempted_at = "sending", MORNING
        db.add(CampaignRecipient(campaign_id="SYNTHETIC-OTHER", user_id=UID,
                                 state="sending", attempted_at=MORNING))
    assert reminders.tick(engine, settings, no_send, MORNING+61) == "complete"
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (KEY, UID)).state == "uncertain"
        assert db.get(CampaignRecipient, ("SYNTHETIC-OTHER", UID)).state == "sending"


def test_database_failure_after_claim_defers_without_sending(daily, monkeypatch):
    engine, settings, _ = daily
    guarded = subscription_promotion.dispatch_claim
    def unavailable(conn, cursor, statement, parameters, context, executemany):
        if "manual_payment_requests" in statement:
            raise OperationalError("SYNTHETIC", {}, Exception("Fixture storage failure"))
    def break_after_claim(*args, **kwargs):
        event.listen(engine, "before_cursor_execute", unavailable)
        try:
            return guarded(*args, **kwargs)
        finally:
            event.remove(engine, "before_cursor_execute", unavailable)
    monkeypatch.setattr(subscription_promotion, "dispatch_claim", break_after_claim)
    assert reminders.tick(engine, settings, no_send, MORNING) == "deferred"
    with Session(engine) as db:
        item = db.get(CampaignRecipient, (KEY, UID))
        assert item.state == "pending" and item.attempted_at == 0
        assert item.retry_at >= MORNING+30


def test_actual_send_rechecks_clock_after_claim_and_never_crosses_window(daily):
    engine, settings, _ = daily
    times = iter((MORNING+1799, MORNING+1800))
    assert reminders.tick(engine, settings, no_send, clock=lambda: next(times)) == "deferred"
    with Session(engine) as db:
        item = db.get(CampaignRecipient, (KEY, UID))
        assert item.state == "pending" and item.attempted_at == 0
    assert reminders.tick(engine, settings, no_send, MORNING+1801) == "scheduled"
    assert snapshot(engine, settings)["last_result"]["expired"] == 1


def test_clock_crossing_deadline_during_final_eligibility_query_prevents_send(daily, monkeypatch):
    engine, settings, _ = daily
    clock, crossed = [MORNING+1799], []
    guarded = subscription_promotion.dispatch_claim
    def cross_during_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT users.id") and "manual_payment_requests" in statement:
            clock[0] = MORNING+1800
            crossed.append(True)
    def final_read_crosses_deadline(*args, **kwargs):
        event.listen(engine, "before_cursor_execute", cross_during_sql)
        try:
            return guarded(*args, **kwargs)
        finally:
            event.remove(engine, "before_cursor_execute", cross_during_sql)
    monkeypatch.setattr(subscription_promotion, "dispatch_claim", final_read_crosses_deadline)
    assert reminders.tick(engine, settings, no_send, clock=lambda: clock[0]) == "deferred"
    assert crossed
    with Session(engine) as db:
        item = db.get(CampaignRecipient, (KEY, UID))
        assert item.state == "pending" and item.attempted_at == 0
    assert reminders.tick(engine, settings, no_send, MORNING+1801) == "scheduled"
    assert snapshot(engine, settings)["last_result"]["expired"] == 1


def test_database_outcome_write_failure_quarantines_a_send_that_already_started(daily):
    engine, settings, _ = daily
    calls = []
    def fail_outcome_write(conn, cursor, statement, parameters, context, executemany):
        if calls and statement.startswith("UPDATE billing_campaign_recipients"):
            raise OperationalError("SYNTHETIC", {}, Exception("Fixture result write failure"))
    event.listen(engine, "before_cursor_execute", fail_outcome_write)
    try:
        assert reminders.tick(engine, settings, accepted(calls), MORNING) == "uncertain"
    finally:
        event.remove(engine, "before_cursor_execute", fail_outcome_write)
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (KEY, UID)).state == "sending"
    assert reminders.tick(engine, settings, no_send, MORNING+61) == "complete"
    with Session(engine) as db:
        assert db.get(CampaignRecipient, (KEY, UID)).state == "uncertain"
    assert len(calls) == 1


def test_one_reminder_per_local_day_allows_only_next_days_new_campaign(daily):
    engine, settings, _ = daily
    calls = []
    assert reminders.tick(engine, settings, accepted(calls), MORNING) == "sent"
    assert reminders.tick(engine, settings, no_send, MORNING+2) == "complete"
    assert reminders.tick(engine, settings, no_send, MORNING+10) == "scheduled"
    next_day = at("2026-10-04T09:00:00+03:00")
    assert reminders.tick(engine, settings, accepted(calls), next_day) == "sent"
    assert reminders.tick(engine, settings, no_send, next_day+2) == "complete"
    with Session(engine) as db:
        rows = db.execute(select(CampaignRecipient.campaign_id, CampaignRecipient.state)).all()
        assert sorted(rows) == [(KEY, "sent"), (reminders.PREFIX + "2026-10-04", "sent")]
    assert len(calls) == 2


def test_database_failure_while_building_audience_has_no_queue_and_no_false_zero(daily):
    engine, settings, _ = daily
    PaymentRequest.__table__.drop(engine)
    with pytest.raises(subscription_promotion.EligibilityUnavailable):
        reminders.tick(engine, settings, no_send, MORNING)
    with pytest.raises(subscription_promotion.EligibilityUnavailable):
        snapshot(engine, settings)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CampaignRecipient)) == 0
        assert db.get(TariffReminderSchedule, reminders.ID).next_run_at == MORNING


def test_no_auto_ria_search_valuation_or_paid_quota_use(daily, monkeypatch):
    from backend import auto_ria, ria_ai_price, ria_search, valuation
    engine, settings, _ = daily
    for module, name in ((auto_ria, "fetch_json"), (ria_search, "fetch_json"),
                         (ria_ai_price, "fetch_quote"), (valuation, "estimate")):
        monkeypatch.setattr(module, name, lambda *a, **k: pytest.fail("Reminder must not use a car API or valuation"))
    paid_calls = []
    def forbid_paid_query(conn, cursor, statement, parameters, context, executemany):
        if any(table in statement.lower() for table in ("source_budget", "source_cache", "source_probe")):
            paid_calls.append(statement)
    event.listen(engine, "before_cursor_execute", forbid_paid_query)
    try:
        sent = []
        assert reminders.tick(engine, settings, accepted(sent), MORNING) == "sent"
        assert reminders.tick(engine, settings, no_send, MORNING+2) == "complete"
        assert snapshot(engine, settings)["enabled"] is True
        assert not paid_calls and len(sent) == 1
    finally:
        event.remove(engine, "before_cursor_execute", forbid_paid_query)
