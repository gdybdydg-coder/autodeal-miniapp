"""Confirmed-only notifications, including legacy queues at rollout."""
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend import ria_market_range
from backend.models import (Delivery, Listing, MonitorActiveWindow, MonitorFeed,
                            MonitorJob, MonitorMatch, MonitorSeen, User)
from backend.ria_search import RiaSearch
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_ria_ai_price import enable, discover_new, wire
from backend.tests.test_active_window import enable_window
from backend.tests.test_shared_distribution import align_subscriptions, discover_first_only, dispatch
from backend.models import Range
from backend.worker import deliver_one, eligible, enqueue


def strict(p):
    p.settings = replace(p.runner.settings, ria_confirmed_deals_only=True,
                         ria_shared_distribution_enabled=True)
    p.runner.settings = p.settings


@pytest.mark.parametrize("failure", ["ai_access_denied", "ai_connection_error", "ai_invalid_response",
                                      "quota_exceeded", "missing_range"])
@pytest.mark.parametrize("active", [False, True])
def test_unconfirmed_new_and_old_ads_are_not_notified(p, monkeypatch, failure, active):
    if active:
        enable_window(p, monkeypatch)
        p.stock = []
    calls = enable(p, monkeypatch, response={} if failure == "missing_range" else None,
                   error=None if failure == "missing_range" else failure)
    strict(p)
    drain(p)
    p.prices["124"] = 6900
    if active:
        p.stock = ["124"]
        with Session(p.engine) as db:
            db.execute(update(MonitorActiveWindow).values(next_poll=0))
            db.execute(update(MonitorFeed).values(next_poll=p.clock[0]+1000))
            db.commit()
    else:
        discover_new(p)
    drain(p)
    assert calls == ["124"] and p.sent == []
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "unvalued"
        assert db.get(MonitorSeen, (1, "124")).state == "unvalued"
        assert db.scalar(select(MonitorMatch)) is None
        assert db.scalar(select(Delivery)) is None
    # No uncontrolled quote retry, peer fallback, or re-entry of this seen ID.
    wake(p, 301)
    drain(p)
    assert calls == ["124"] and p.sent == []


@pytest.mark.parametrize("price,expected", [(6900, False), (4900, True)])
def test_golf_price_must_beat_confirmed_market_even_with_missing_gearbox(p, monkeypatch, price, expected):
    calls = enable(p, monkeypatch, response=wire(6100))  # adjusted lower bound $5505.25
    strict(p)
    old = RiaSearch.car
    def car(self, *args, **kw):
        candidate = old(self, *args, **kw)
        candidate.update(year=2007, mileage=250000, gear_id=None, transmission="")
        return candidate
    monkeypatch.setattr(RiaSearch, "car", car)
    p.prices["124"] = price
    add_search(p, price=Range(to=7000), minDiscount=10)
    align_subscriptions(p)
    discover_first_only(p)
    drain(p)
    dispatch(p)
    # First subscription requires 15%; second 10%. $4900 qualifies only for 10%.
    assert [uid for uid, _ in p.sent] == ([222] if expected else [])
    assert calls == ["124"]
    if expected:
        assert p.sent[0][1].market == 5505.25


def legacy_info(p, monkeypatch):
    enable(p, monkeypatch, response={})
    drain(p)
    discover_new(p)
    assert p.runner.tick()
    assert p.runner.tick()
    with Session(p.engine) as db:
        assert db.scalar(select(Listing)).car["market"] is None


@pytest.mark.parametrize("stale", [False, True])
def test_existing_info_matches_cannot_enter_new_queue_or_refresh_loop(p, monkeypatch, stale):
    legacy_info(p, monkeypatch)
    strict(p)
    if stale:
        p.clock[0] += 301
    enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
    with Session(p.engine) as db:
        assert not eligible(db, 111, db.scalar(select(Listing)), p.clock[0],
                            require_provider_range=True, require_confirmed_deal=True)
        assert db.scalar(select(Delivery)) is None
        assert db.get(MonitorJob, "124").state == "informational"


@pytest.mark.parametrize("stale", [False, True])
def test_preexisting_pending_info_is_cancelled_before_network_or_refresh(p, monkeypatch, stale):
    legacy_info(p, monkeypatch)
    enqueue(p.engine)
    strict(p)
    if stale:
        p.clock[0] += 301
    before = len(p.calls)
    assert deliver_one(p.engine, p.settings, p.runner.sender) == "cancelled"
    assert not p.sent and len(p.calls) == before
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "informational"
        assert db.get(MonitorSeen, (1, "124")).state == "informational"


@pytest.mark.parametrize("state", ["sent", "uncertain", "failed", "cancelled"])
def test_existing_final_claims_are_not_reopened_on_policy_change(p, monkeypatch, state):
    legacy_info(p, monkeypatch)
    enqueue(p.engine)
    with Session(p.engine) as db:
        db.scalar(select(Delivery)).state = state
        db.commit()
    strict(p)
    enqueue(p.engine, require_confirmed_deal=True)
    assert deliver_one(p.engine, p.settings, p.runner.sender) == "empty"
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == state


def test_rejected_information_does_not_block_later_confirmed_deal(p, monkeypatch):
    legacy_info(p, monkeypatch)
    enqueue(p.engine)
    strict(p)
    enable(p, monkeypatch)
    discover_new(p, "125")
    drain(p)
    dispatch(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "125")]
    with Session(p.engine) as db:
        assert sorted(db.scalars(select(Delivery.state))) == ["cancelled", "sent"]


def test_stop_during_confirmed_quote_still_prevents_delivery(p, monkeypatch):
    def stop():
        with Session(p.engine) as db:
            db.get(User, 111).ready = False
            db.commit()
    enable(p, monkeypatch, action=stop)
    strict(p)
    drain(p)
    discover_new(p)
    drain(p)
    assert not p.sent


def test_policy_diagnostics_describe_actual_missing_quote_behavior():
    assert ria_market_range.policy(confirmed_deals_only=True)["missing_range"] == "suppress_notification"
    assert ria_market_range.policy()["missing_range"] == "informational_without_market_price"
