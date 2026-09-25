"""Wall-clock boundaries, durable checkpoints and unchanged hard quota gates."""
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import poll_schedule
from backend.app import Settings, create_app
from backend.models import (Delivery, MonitorFeed, MonitorMembership,
                            MonitorWatch, Search, SourceBudget, User)
from backend.monitor import Monitor, poll_interval, reset_watch
from backend.ria_budget import BudgetLimits
from backend.tests.test_monitor import p, drain, searches, details
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_active_window import enable_window


def utc(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("instant,period,interval", [
    ("2026-09-25T04:59:59+00:00", "night", 140),
    ("2026-09-25T05:00:00+00:00", "day", 110),
    ("2026-09-25T14:59:59+00:00", "day", 110),
    ("2026-09-25T15:00:00+00:00", "evening", 60),
    ("2026-09-25T19:59:59+00:00", "evening", 60),
    ("2026-09-25T20:00:00+00:00", "night", 140),
    ("2026-09-25T21:00:00+00:00", "night", 140),
    ("2026-01-25T06:00:00+00:00", "day", 110),
    ("2026-01-25T16:00:00+00:00", "evening", 60),
    ("2026-01-25T21:00:00+00:00", "night", 140),
    ("2026-03-29T00:30:00+00:00", "night", 140),
    ("2026-03-29T01:30:00+00:00", "night", 140),
    ("2026-10-25T00:30:00+00:00", "night", 140),
    ("2026-10-25T01:30:00+00:00", "night", 140),
])
def test_kyiv_boundaries_include_winter_and_both_dst_transitions(instant, period, interval):
    caps = BudgetLimits(1200, 18000, 90000)
    report = poll_schedule.policy(15, caps, utc(instant))
    assert report["active_period"] == period
    assert report["interval_seconds"] == report["requested_interval_seconds"] == interval
    assert not report["budget_limited"]


@pytest.mark.parametrize("groups", [0, 1, 14, 15, 100, 200])
def test_budget_guard_reserves_capacity_and_never_raises_caps(groups):
    caps = BudgetLimits(900, 12000, 90000)
    report = poll_schedule.policy(groups, caps, utc("2026-09-25T07:00:00+00:00"))
    searches_per_day = 0
    for period, (_, start, end, target) in zip(report["periods"], poll_schedule.PERIODS):
        interval = period["interval_seconds"]
        assert interval >= target
        assert groups * 3600 / interval <= caps.hourly * .75
        searches_per_day += groups * ((end - start) % 24) * 3600 / interval
    assert searches_per_day <= caps.daily * .75
    assert caps == BudgetLimits(900, 12000, 90000)
    if groups == 15:
        assert [p["interval_seconds"] for p in report["periods"]] == [201, 158, 86]
        assert report["requested_search_calls_per_day"] == 12881
        assert report["minimum_planning_limits"]["hourly"] == 1200


def test_missing_tzdata_or_disabled_schedule_keeps_monitor_available(monkeypatch):
    caps = BudgetLimits(900, 12000, 90000)
    assert poll_interval(15, caps, provider_pricing_enabled=True) == 144
    monkeypatch.setattr(poll_schedule, "KYIV", None)
    assert poll_interval(15, caps, provider_pricing_enabled=True, schedule_enabled=True) == 144
    assert poll_schedule.policy(15, caps)["reason"] == "timezone_unavailable"
    assert poll_interval(15, caps, provider_pricing_enabled=True,
                         active_window_enabled=True, schedule_enabled=True) == 216


def configured(p, monkeypatch, instant="2026-09-25T14:58:30+00:00"):
    ai_calls = enable(p, monkeypatch)
    p.runner.settings = replace(p.runner.settings, ria_poll_schedule_enabled=True)
    for name, value in (("HOURLY", 900), ("DAILY", 12000), ("TOTAL", 90000)):
        monkeypatch.setenv("RIA_REQUESTS_" + name + "_CAP", str(value))
    factory = p.runner.search_factory
    def current_limits(engine, key):
        source = factory(engine, key)
        source.limits = BudgetLimits.env()
        return source
    p.runner.search_factory = current_limits
    # Move the existing fixture activation to the tested date, without waiting
    # through months of simulated catch-up. This setup never runs on a server.
    p.clock[0] = utc(instant)
    with Session(p.engine) as db:
        db.scalar(select(MonitorMembership)).started_at = p.clock[0] - 1
        db.commit()
    return ai_calls


@pytest.mark.parametrize("active_supplement", [False, True])
def test_morning_transition_uses_new_interval_before_previous_timer_expires(p, monkeypatch, active_supplement):
    if active_supplement:
        enable_window(p, monkeypatch)
    configured(p, monkeypatch, "2026-09-25T04:58:10+00:00")
    drain(p)
    before = len(searches(p))
    p.ads["124"] = p.clock[0] + 1
    p.clock[0] = utc("2026-09-25T05:00:00+00:00")
    drain(p)
    assert len(searches(p)) == before + 1
    assert [car.source_id for _, car in p.sent] == ["124"]
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.next_poll - feed.checked_at == 110


@pytest.mark.parametrize("active_supplement", [False, True])
def test_transition_and_restart_deliver_only_new_ads_without_changing_epochs(p, monkeypatch, active_supplement):
    if active_supplement:
        enable_window(p, monkeypatch)
    ai_calls = configured(p, monkeypatch)
    drain(p)  # 17:58:30 Kyiv: next check would be 18:00:20 at daytime spacing.
    before = len(searches(p))
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        assert db.scalar(select(MonitorFeed)).next_poll - db.scalar(select(MonitorFeed)).checked_at == 110
    p.ads["124"] = p.clock[0] + 1
    p.clock[0] = utc("2026-09-25T15:00:00+00:00")
    drain(p)  # New evening policy makes the saved feed due immediately.
    assert len(searches(p)) == before + 1
    assert [car.source_id for _, car in p.sent] == ["124"]
    assert ai_calls == ["124"] and len(details(p, "124")) == 1
    assert not details(p, "123")
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        cursor, deadline = feed.cursor, feed.next_poll
        assert deadline - feed.checked_at == 60
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.scalar(select(Delivery)).state == "sent"
        budget_total = db.get(SourceBudget, "auto_ria").total
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    assert not p.runner.tick()
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorFeed)).cursor == cursor
        assert db.scalar(select(MonitorFeed)).next_poll == deadline
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.scalar(select(Delivery)).state == "sent"
        assert db.get(SourceBudget, "auto_ria").total == budget_total
    p.clock[0] += 61
    drain(p)
    assert len(p.sent) == 1 and ai_calls == ["124"]
    client = TestClient(create_app(p.runner.settings, p.engine))
    status = client.get("/api/source-status").json()["monitor"]
    assert status["schedule"]["active_period"] == "evening"
    assert status["schedule"]["requested_interval_seconds"] == status["interval_seconds"] == 60


@pytest.mark.parametrize("state,context,delay", [
    ("quota_exceeded", {}, 3600), ("busy", {}, 1),
    ("watching", {"clock": "published", "page": 1}, 60),
    ("watching", {}, 0),
])
def test_transition_preserves_backoffs_and_in_progress_pages(p, monkeypatch, state, context, delay):
    configured(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        feed.status, feed.context = state, context
        feed.next_poll = feed.checked_at + delay
        expected = (feed.cursor, feed.next_poll, dict(feed.context), feed.status)
        db.commit()
    p.clock[0] = utc("2026-09-25T15:00:00+00:00")
    assert p.runner.claim()
    try:
        with Session(p.engine) as db:
            p.runner.reschedule(db, {db.scalar(select(MonitorFeed)).id})
            feed = db.scalar(select(MonitorFeed))
            assert (feed.cursor, feed.next_poll, feed.context, feed.status) == expected
    finally:
        p.runner.release("idle")


def test_night_transition_extends_wait_and_does_not_reenable_stopped_search(p, monkeypatch):
    configured(p, monkeypatch)
    p.clock[0] = utc("2026-09-25T19:59:30+00:00")
    drain(p)
    before = len(searches(p))
    p.clock[0] = utc("2026-09-25T20:00:31+00:00")
    assert not p.runner.tick()
    assert len(searches(p)) == before
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.next_poll - feed.checked_at == 140
        db.get(User, 111).ready = False
        db.get(Search, 1).enabled = False
        reset_watch(db, 1, False)
        db.commit()
    p.clock[0] += 200
    assert not p.runner.tick()
    with Session(p.engine) as db:
        assert not db.get(Search, 1).enabled and not db.get(User, 111).ready
        assert db.get(MonitorWatch, 1) is None
    assert len(searches(p)) == before and not p.sent


@pytest.mark.parametrize("cap", ["hourly", "daily", "total", "upstream"])
def test_schedule_does_not_bypass_exhausted_quota(p, monkeypatch, cap):
    configured(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    p.clock[0] += 200
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        if cap == "hourly": budget.calls = [p.clock[0]] * 900
        if cap == "daily": budget.calls = [p.clock[0] - 4000] * 12000
        if cap == "total": budget.total = 90000
        if cap == "upstream": budget.blocked_until = p.clock[0] + 3600
        db.commit()
    calls = len(p.calls)
    drain(p)
    assert len(p.calls) == calls and not p.sent


def test_environment_can_disable_schedule_without_changing_limits(monkeypatch):
    for name in ("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"):
        monkeypatch.setenv(name, "test-only")
    monkeypatch.delenv("RIA_POLL_SCHEDULE_ENABLED", raising=False)
    assert Settings.env().ria_poll_schedule_enabled
    monkeypatch.setenv("RIA_POLL_SCHEDULE_ENABLED", "false")
    assert not Settings.env().ria_poll_schedule_enabled
