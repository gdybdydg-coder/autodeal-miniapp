"""Paid normal discovery must outlive a supplemental evidence timeout.

Temporary SQLite and fixture transports only; no real source or Telegram I/O.
"""
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import poll_schedule, recent_publications as rp
from backend.models import Delivery, Listing, MonitorFeed, MonitorJob, MonitorMatch, MonitorMembership, MonitorSeen, Search, User
from backend.monitor import Monitor
from backend.ria_budget import BudgetLimits
from backend.tests.test_monitor import p, add_search, drain, details, searches
from backend.tests.test_paid_sources_production import approve, strict
from backend.tests.test_recent_publications import setup, offer, dispatch
from backend.tests.test_ria_ai_price import enable


def test_confirmed_paid_search_extends_night_wait_and_resumes_once_after_restart(p, monkeypatch):
    for name, value in (("HOURLY", 900), ("DAILY", 3000), ("TOTAL", 90000)):
        monkeypatch.setenv("RIA_REQUESTS_" + name + "_CAP", str(value))
    p.clock[0] = datetime.fromisoformat("2026-10-03T19:59:50+00:00").timestamp()
    strict(p)
    approve(p, purchase_until=p.clock[0] + 86400, access_until=p.clock[0] + 86400)
    with Session(p.engine) as db:
        db.scalar(select(MonitorMembership)).started_at = p.clock[0] - 1
        db.commit()
    quotes = enable(p, monkeypatch)
    p.runner.settings = replace(p.runner.settings, ria_poll_schedule_enabled=True)
    drain(p)
    before = len(searches(p))
    p.ads["124"] = p.clock[0] + 11
    p.clock[0] += 75
    drain(p)
    with Session(p.engine) as db:
        from backend.source_pipeline_health import snapshot
        status = snapshot(db, p.runner.settings, p.clock[0])
        feed = db.scalar(select(MonitorFeed))
        next_poll = feed.next_poll
        assert feed.next_poll - feed.checked_at == 3600
    assert status["primary"]["interval_seconds"] == 3600
    assert len(searches(p)) == before and not quotes and not p.sent
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    assert not p.runner.tick()
    p.clock[0] = next_poll + .001
    drain(p)
    assert len(searches(p)) == before + 1
    assert quotes == ["124"]
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    assert not p.runner.tick() and len(p.sent) == 1


@pytest.mark.parametrize("retirement", ["expired", "legacy_expired", "disabled"])
def test_fresh_primary_confirmation_recovers_unclaimed_html_retirement(p, monkeypatch, retirement):
    strict(p)
    add_search(p)
    approve(p, purchase_until=p.clock[0] + 86400, access_until=p.clock[0] + 86400)
    approve(p, 222, purchase_until=p.clock[0] + 86400, access_until=p.clock[0] + 86400)
    quotes = setup(p, monkeypatch)
    added = offer(p)
    # Activation boundaries can require more than one ordinary primary page.
    # Stop exactly when the shared public-card detail is durably validated,
    # before the next tick would request the pricing quote.
    for _ in range(8):
        p.runner.tick()
        with Session(p.engine) as db:
            if db.get(MonitorJob, "77") is not None:
                break
    else:
        pytest.fail("fixture did not reach shared HTML detail validation")
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        feed.next_poll = p.clock[0] + rp.MAX_AGE + 200
        db.commit()
    if retirement in {"expired", "legacy_expired"}:
        p.clock[0] += rp.MAX_AGE + 1
    else:
        p.runner.settings = replace(p.settings, ria_recent_publications_enabled=False)
    p.runner.tick()
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").state == "cancelled"
        assert db.get(MonitorSeen, (1, "77")).state in {"cancelled", "html_cancelled"}
        assert db.scalar(select(Delivery)) is None
        if retirement == "legacy_expired":
            # Saved shape produced by the preceding production release: it
            # retained valid dated proof but erased the reason and origin.
            from backend.shared_distribution import proof
            job = db.get(MonitorJob, "77")
            job.reason, job.result = "", proof(job.result)
            db.get(MonitorSeen, (1, "77")).state = "cancelled"
            db.get(MonitorSeen, (2, "77")).state = "cancelled"
            db.commit()
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    p.ads["77"] = added  # Independent dated API discovery now confirms publication.
    with Session(p.engine) as db:
        db.scalar(select(MonitorFeed)).next_poll = 0
        db.commit()
    drain(p)
    dispatch(p)
    assert quotes == ["77"]
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "77"), (222, "77")]
    assert p.sent[0][1].pipeline["discovery_kind"] == "new_publication"
    before = len(details(p, "77"))
    with Session(p.engine) as db:
        db.scalar(select(MonitorFeed)).next_poll = 0
        db.commit()
    drain(p)
    assert len(details(p, "77")) == before and len(p.sent) == 2


@pytest.mark.parametrize("groups", [0, 1, 3, 100, 200])
@pytest.mark.parametrize("instant", ["2026-10-03T20:01:00+00:00", "2026-01-03T21:01:00+00:00",
                                     "2026-03-29T01:30:00+00:00", "2026-10-25T01:30:00+00:00"])
def test_paid_night_plan_keeps_timezone_and_reserved_hard_caps(groups, instant):
    caps = BudgetLimits(4500, 90000, 1102160)
    report = poll_schedule.policy(groups, caps, datetime.fromisoformat(instant).timestamp())
    assert report["active_period"] == "night"
    assert report["requested_interval_seconds"] == 3600
    requested_per_day = 0
    for period, (_, start, end, _) in zip(report["periods"], poll_schedule.PERIODS):
        interval = period["interval_seconds"]
        assert groups * 3600 / interval <= caps.hourly * .75
        requested_per_day += groups * ((end - start) % 24) * 3600 / interval
    assert requested_per_day <= caps.daily * .75
    if groups <= 3:
        assert report["interval_seconds"] == 3600 and not report["budget_limited"]


@pytest.mark.parametrize("claim", ["sent", "uncertain", "pending", "cancelled", "failed"])
def test_retired_html_never_reopens_a_telegram_claim(p, monkeypatch, claim):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick(); p.runner.tick()
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        job.state, job.reason = "cancelled", "html_publication_expired"
        seen = db.get(MonitorSeen, (1, "77")); seen.state = rp.RETIRED_STATE
        listing = Listing(source="auto_ria", source_id="77", car={})
        db.add(listing); db.flush()
        db.add(Delivery(user_id=111, listing_id=listing.id, state=claim)); db.commit()
        assert not rp.retired_interest(db, seen, job, 111, p.clock[0])
        assert db.scalar(select(Delivery)).state == claim


@pytest.mark.parametrize("state", ["checked", "unavailable", "excluded", "unvalued"])
def test_other_recorded_outcomes_are_not_reclassified_as_html_retirement(p, state):
    with Session(p.engine) as db:
        job = MonitorJob(source_id="77", state="cancelled", reason="html_publication_expired", first_seen=p.clock[0])
        seen = MonitorSeen(search_id=1, source_id="77", epoch="fixture", state=state, first_seen=p.clock[0])
        assert not rp.retired_interest(db, seen, job, 111, p.clock[0])


def test_missing_or_invalid_legacy_proof_is_not_a_recoverable_retirement(p):
    with Session(p.engine) as db:
        job = MonitorJob(source_id="77", state="cancelled", reason="", first_seen=p.clock[0], result={})
        seen = MonitorSeen(search_id=1, source_id="77", epoch="fixture", state="cancelled", first_seen=p.clock[0])
        assert not rp.retired_interest(db, seen, job, 111, p.clock[0])
        job.result = {"html_verified": True, "html_expires_at": p.clock[0] - 1}
        assert not rp.retired_interest(db, seen, job, 111, p.clock[0])


def test_parallel_primary_promotion_wins_over_stale_html_expiry(p, monkeypatch):
    strict(p); approve(p, purchase_until=p.clock[0]+86400, access_until=p.clock[0]+86400)
    quotes = setup(p, monkeypatch); added = offer(p)
    p.runner.tick(); p.runner.tick()
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        stale_snapshot = dict(job.result)
        job.result = {**job.result, "discovery_kind": "new_publication", "publication_after": added-1}
        db.commit()
    p.clock[0] += rp.MAX_AGE + 1
    assert p.runner.claim()
    p.runner.complete("77", stale_snapshot, "cancelled", reason="html_publication_expired")
    p.runner.release("idle")
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        assert job.state == "pending" and job.reason == ""
        assert job.result["discovery_kind"] == "new_publication"
        assert db.get(MonitorSeen, (1, "77")).state == "pending"
    drain(p)
    assert quotes == ["77"] and len(p.sent) == 1


def test_html_retirement_cannot_recreate_delivery_matches_from_saved_quote(p, monkeypatch):
    strict(p); approve(p, purchase_until=p.clock[0]+86400, access_until=p.clock[0]+86400)
    setup(p, monkeypatch); offer(p)
    p.runner.tick(); p.runner.tick(); p.runner.tick()
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        saved = dict(job.result)
        assert db.scalar(select(MonitorMatch)) is not None
        job.state = "pending"
        db.get(MonitorSeen, (1, "77")).state = "pending"
        db.commit()
    p.clock[0] += rp.MAX_AGE + 1
    assert p.runner.claim()
    p.runner.complete("77", saved, "cancelled", reason="html_publication_expired")
    p.runner.release("idle")
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorMatch)) is None
        assert db.scalar(select(Delivery)) is None


@pytest.mark.parametrize("restriction", ["unpaid", "stopped", "disabled_search"])
def test_primary_recovery_never_bypasses_current_paid_search_eligibility(p, monkeypatch, restriction):
    strict(p); approve(p)
    quotes = setup(p, monkeypatch); added = offer(p)
    p.runner.tick(); p.runner.tick()
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        job.state, job.reason = "cancelled", "recent_publications_disabled"
        db.get(MonitorSeen, (1, "77")).state = rp.RETIRED_STATE
        if restriction == "unpaid":
            from backend.manual_payment_models import PaymentRequest
            db.get(PaymentRequest, "fixture-111").state = "review"
        elif restriction == "stopped":
            db.get(User, 111).ready = False
        else:
            db.get(Search, 1).enabled = False
        db.scalar(select(MonitorFeed)).next_poll = 0
        db.commit()
    p.ads["77"] = added
    before = len(p.calls)
    drain(p)
    assert len(p.calls) == before and not quotes and not p.sent
