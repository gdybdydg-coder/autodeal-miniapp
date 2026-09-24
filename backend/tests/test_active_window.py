from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import active_window
from backend.ria_budget import BudgetLimits
from backend.models import (Delivery, Listing, MonitorActiveWindow, MonitorControl, MonitorFeed,
                            MonitorJob, MonitorSeen, MonitorWatch, Range, Search, SourceBudget, SourceProbe)
from backend.worker import enqueue
from backend.monitor import Monitor, reset_watch, runtime_status
from backend.tests.test_monitor import p, drain, wake, details, add_search


def enable_window(p, monkeypatch):
    for name, value in (("HOURLY", 900), ("DAILY", 3000), ("TOTAL", 90000)):
        monkeypatch.setenv("RIA_REQUESTS_" + name + "_CAP", str(value))
    p.stock = ["123"]
    p.settings = replace(p.settings, ria_active_window_enabled=True)
    p.runner.settings = p.settings
    factory = p.runner.search_factory
    def with_stock(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            if path == "search" and "generation_id[0][0]" not in params and "published_after" not in params:
                p.calls.append((path, dict(params)))
                assert params["page"] == 0 and params["countpage"] == 50
                return {"result": {"search_result": {"ids": p.stock[:50], "count": len(p.stock)}}}
            return original(api_key, path, params)
        source.fetch = fetch
        return source
    p.runner.search_factory = with_stock


def test_first_active_page_is_a_baseline_without_replaying_old_listings(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    assert not p.sent and not details(p, "123")
    with Session(p.engine) as db:
        window = db.scalar(select(MonitorActiveWindow))
        assert window.window == ["123"] and window.status == "baseline"
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    wake(p, 301)
    drain(p)
    assert not p.sent and not details(p, "123")


def test_reenabling_after_old_snapshot_rebaselines_without_replaying_current_page(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        row = db.scalar(select(MonitorActiveWindow))
        row.window, row.checked_at, row.next_poll = ["999"], p.clock[0] - 3600, 0
        db.delete(db.get(SourceProbe, active_window.BASELINE_ID))
        db.commit()
    wake(p, 301)
    drain(p)
    with Session(p.engine) as db:
        row = db.scalar(select(MonitorActiveWindow))
        assert row.window == ["123"] and row.status == "baseline"
        assert db.get(SourceProbe, active_window.BASELINE_ID)
    assert not p.sent and not details(p, "123")


def test_disable_then_reenable_starts_from_current_page_baseline(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    p.runner.sync()
    with Session(p.engine) as db:
        row = db.scalar(select(MonitorActiveWindow))
        assert row.window == [] and row.checked_at == 0
        assert db.get(SourceProbe, active_window.BASELINE_ID)
    p.stock = ["124", "123"]
    p.runner.settings = p.settings
    wake(p, 301)
    drain(p)
    with Session(p.engine) as db:
        row = db.scalar(select(MonitorActiveWindow))
        assert row.window == ["124", "123"] and row.status == "baseline"
    assert not p.sent and not details(p, "124")


def test_new_first_page_entries_are_queued_without_a_catalog_scan(p, monkeypatch):
    enable_window(p, monkeypatch)
    p.stock = [str(n) for n in range(1000, 1100)]
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        window = db.scalar(select(MonitorActiveWindow))
        assert len(window.window) == 50 and window.source_total == 100
        assert window.status == "baseline"
        assert not list(db.scalars(select(MonitorJob)))
    p.stock = ["2000", "2001"] + [str(n) for n in range(1000, 1098)]
    wake(p, 301)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["2000", "2001"]
    assert not details(p, "1000")
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorJob)))) == 2
    state = runtime_status(p.engine, True, 111, active_window_enabled=True)["active_window"]
    assert state["coverage"] == "latest_active_window_diff" and not state["historical_pagination"]
    assert "1000" not in str(state)
    assert not runtime_status(p.engine, True, 999, active_window_enabled=True)["active_window"]["state_counts"]


def test_price_drop_on_seen_non_deal_does_not_recheck_old_listing(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    p.stock = ["124", "123"]
    p.prices["124"] = 16000
    wake(p, 301)
    drain(p)
    assert not p.sent
    p.prices["124"] = 10000
    wake(p, 301)
    drain(p)
    assert not p.sent and len(details(p, "124")) == 1

    p.stock = ["123"]  # An ID leaving the first page must not reset its claim.
    wake(p, 301)
    drain(p)
    p.stock = ["124", "123"]
    wake(p, 301)
    drain(p)
    assert not p.sent and len(details(p, "124")) == 1
    wake(p, 1801)
    drain(p)
    assert not p.sent and len(details(p, "124")) == 1
    wake(p, 1801)
    drain(p)
    assert not p.sent and len(details(p, "124")) == 1


def test_supplement_pauses_before_consuming_reserved_primary_budget(p, monkeypatch):
    enable_window(p, monkeypatch)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [p.clock[0]] * 720
        db.commit()
    p.ads["124"] = p.clock[0] - 1
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124"]
    assert not details(p, "123")
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorActiveWindow)).status == "reserved_for_new_publications"
    assert not any(path == "search" and "published_after" not in params
                   and "generation_id[0][0]" not in params for path, params in p.calls)


def test_republished_id_is_discovered_when_daily_primary_usage_exceeds_half(p, monkeypatch):
    enable_window(p, monkeypatch)
    monkeypatch.setenv("RIA_REQUESTS_DAILY_CAP", "12000")
    factory = p.runner.search_factory
    def live_limits(engine, key):
        source = factory(engine, key)
        source.limits = BudgetLimits(900, 12000, 90000)
        return source
    p.runner.search_factory = live_limits
    drain(p)  # An existing ID forms the first-page baseline.
    assert not p.sent
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.calls = [p.clock[0] - 7200] * 7244 + [p.clock[0]] * 265
        db.commit()
    # An older numerical ID is newly republished in the active page, but does
    # not appear in the source's publication-window response.
    p.stock = ["39658048", "123"]
    p.prices["39658048"] = 10000
    wake(p, 301)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "39658048")]
    assert details(p, "39658048")
    assert not details(p, "123")
    with Session(p.engine) as db:
        assert active_window.budget_available(db, BudgetLimits(900, 12000, 90000))


def test_supplement_still_pauses_with_only_primary_daily_headroom_left(p, monkeypatch):
    enable_window(p, monkeypatch)
    limits = BudgetLimits(900, 12000, 90000)
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.calls = [p.clock[0] - 7200] * 9510 + [p.clock[0]] * 60
        db.commit()
    with Session(p.engine) as db:
        assert not active_window.budget_available(db, limits)


def test_one_call_newest_page_is_checked_before_bounded_valuation_can_resume(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [p.clock[0]] * 691
        db.commit()
    p.stock = ["39658048", "123"]
    p.prices["39658048"] = 10000
    wake(p, 301)
    drain(p)
    assert not details(p, "39658048") and not p.sent
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorActiveWindow)).status == "watching"
        assert db.get(MonitorJob, "39658048").state == "pending"
        assert not active_window.budget_available(db)
        assert active_window.budget_available(db, reserve=1)
    # The listing is evaluated only when a full safe valuation step becomes
    # available; delivery is still deduplicated for the same user and ID.
    wake(p, 3601)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "39658048")]


def test_new_arrival_is_evaluated_before_older_supplemental_job(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="999", first_seen=p.clock[0] - 100,
                          result={"discovery_kind": "active_window"}))
        db.add(MonitorSeen(search_id=1, source_id="999", epoch=db.get(MonitorWatch, 1).epoch,
                           state="pending", first_seen=p.clock[0] - 100))
        db.get(MonitorControl, "pilot").status = "idle"
        db.commit()
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124", "999"]


def test_stop_during_supplemental_fetch_cannot_create_interest(p, monkeypatch):
    enable_window(p, monkeypatch)
    factory = p.runner.search_factory
    def stopped(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(api_key, path, params):
            if path == "search" and "published_after" not in params and "generation_id[0][0]" not in params:
                with Session(p.engine) as db:
                    db.get(Search, 1).enabled = False
                    reset_watch(db, 1, False)
                    db.commit()
            return original(api_key, path, params)
        source.fetch = fetch
        return source
    p.runner.search_factory = stopped
    drain(p)
    assert not p.sent and not details(p, "123")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "123") is None


@pytest.mark.parametrize("already_queued", [True, False])
def test_disable_supplement_retires_old_work_and_queued_alerts_but_keeps_new(p, monkeypatch, already_queued):
    enable_window(p, monkeypatch)
    drain(p)  # Establish a baseline before the new active ID appears.
    p.stock = ["125", "123"]
    wake(p, 301)
    for _ in range(20):
        if not p.runner.tick():
            break
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        assert listing and listing.source_id == "125"
        listing_id = listing.id
        db.add(Delivery(user_id=222, listing_id=listing.id, state="sent", message_id=17))
        db.add(Delivery(user_id=333, listing_id=listing.id, state="uncertain"))
        db.add(MonitorJob(source_id="999", first_seen=p.clock[0], result={"discovery_kind": "active_window"}))
        db.add(MonitorSeen(search_id=1, source_id="999", epoch=db.get(MonitorWatch, 1).epoch,
                           state="pending", first_seen=p.clock[0]))
        db.commit()
    if already_queued:
        enqueue(p.engine)
    previous_old_checks = len(details(p, "125"))
    p.settings = replace(p.settings, ria_active_window_enabled=False)
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    p.ads["124"] = p.clock[0] + 1
    wake(p, 601)  # Stale supplemental cards must not initiate a price refresh.
    p.runner.deliver_tick()  # Delivery can run before the first monitor sync.
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124"]
    assert len(details(p, "125")) == previous_old_checks and not details(p, "999")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "999").state == "cancelled"
        assert db.get(MonitorSeen, (1, "999")).state == active_window.RETIRED_STATE
        assert db.scalar(select(Delivery.state).where(Delivery.user_id == 222)) == "sent"
        assert db.scalar(select(Delivery.state).where(Delivery.user_id == 333)) == "uncertain"
        old = db.scalar(select(Delivery).where(Delivery.user_id == 111, Delivery.listing_id == listing_id))
        assert (old.state == "cancelled") if already_queued else old is None


@pytest.mark.parametrize("first_delivery", ["sent", "uncertain"])
@pytest.mark.parametrize("second_active", [True, False])
def test_shared_informational_job_does_not_recheck_old_listing_or_duplicate_sent(
        p, monkeypatch, first_delivery, second_active):
    enable_window(p, monkeypatch)
    p.stock = ["124"]
    add_search(p, sid=2, uid=222, price=Range.model_validate({"to": 5000}))
    p.ads["124"] = p.clock[0] + 1
    p.clock[0] += 2
    factory, sender = p.runner.search_factory, p.runner.sender
    def information_only(engine, key):
        source = factory(engine, key)
        source.notification_comparisons = lambda _: []
        return source
    def send(uid, car):
        result = sender(uid, car)
        return {"uncertain": True} if uid == 111 and first_delivery == "uncertain" else result
    p.runner.search_factory, p.runner.sender = information_only, send
    # Both searches observed the same ID before evaluation. Search indexing can
    # lag the fresh detail price, so the second search must reject USD 10,000.
    assert p.runner.claim()
    try:
        p.runner.sync()
        with Session(p.engine) as db:
            feeds = list(db.scalars(select(MonitorFeed.id)))
        for feed_id in feeds:
            source = p.runner.search_factory(p.engine, "test-only")
            source.acquire()
            try:
                p.runner.discover(feed_id, source, 60)
            finally:
                source.release()
    finally:
        p.runner.release("discover")
    drain(p)
    assert [(uid, car.price) for uid, car in p.sent] == [(111, 10000)]
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "informational"
        assert db.get(MonitorSeen, (2, "124")).state == "informational"
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == first_delivery
        if not second_active:
            db.get(Search, 2).enabled = False
            reset_watch(db, 2, False)
            db.commit()
    p.prices["124"] = 4000
    wake(p, 1801)
    drain(p)
    expected = [(111, 10000)]
    assert [(uid, car.price) for uid, car in p.sent] == expected
    assert len(details(p, "124")) == 1
    wake(p, 1801)
    drain(p)
    assert [(uid, car.price) for uid, car in p.sent] == expected


def test_recent_supplemental_arrival_does_not_wait_behind_accumulated_queue(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        for n in range(663):
            db.add(MonitorJob(source_id=str(20000 + n), first_seen=p.clock[0] - 21600 + n,
                              result={"discovery_kind": active_window.KIND}))
        db.add(MonitorJob(source_id="37319411", first_seen=p.clock[0],
                          result={"discovery_kind": active_window.KIND}))
        db.commit()
    selected = []
    monkeypatch.setattr(p.runner, "evaluate", lambda source_id, source: selected.append(source_id))
    assert p.runner.tick()
    assert selected == ["37319411"]


def test_primary_job_keeps_priority_over_even_newer_supplement(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="124", first_seen=p.clock[0] - 60))
        db.add(MonitorJob(source_id="37319411", first_seen=p.clock[0],
                          result={"discovery_kind": active_window.KIND}))
        db.commit()
    selected = []
    monkeypatch.setattr(p.runner, "evaluate", lambda source_id, source: selected.append(source_id))
    assert p.runner.tick()
    assert selected == ["124"]


def test_paid_arrival_is_evaluated_with_available_hourly_capacity(p, monkeypatch):
    from backend.tests.test_ria_ai_price import enable
    enable_window(p, monkeypatch)
    ai_calls = enable(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [p.clock[0]] * 424
        db.commit()
    p.stock = ["37319411", "123"]
    p.prices["37319411"] = 10000
    wake(p, 301)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "37319411")]
    assert ai_calls == ["37319411"]
    assert len(details(p, "37319411")) == 1


def test_due_newest_page_is_not_starved_by_supplemental_backlog(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="999", first_seen=p.clock[0] - 100,
                          result={"discovery_kind": active_window.KIND}))
        db.scalar(select(MonitorActiveWindow)).next_poll = 0
        db.commit()
    p.stock = ["37319411", "123"]
    selected = []
    monkeypatch.setattr(p.runner, "evaluate", lambda source_id, source: selected.append(source_id))
    assert p.runner.tick()
    assert selected == []
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "37319411").state == "pending"


def test_one_paid_valuation_serves_one_hundred_matching_users(p, monkeypatch):
    from backend.tests.test_ria_ai_price import enable
    enable_window(p, monkeypatch)
    ai_calls = enable(p, monkeypatch)
    for n in range(2, 101):
        add_search(p, sid=n, uid=1000 + n)
    drain(p)
    p.stock = ["37319411", "123"]
    p.prices["37319411"] = 10000
    wake(p, 301)
    drain(p)
    # Synthetic delivery only; no real subscribers, network or paid traffic.
    for _ in range(100):
        p.runner.deliver_tick()
    assert len(p.sent) == 100
    assert len({uid for uid, _ in p.sent}) == 100
    assert {car.source_id for _, car in p.sent} == {"37319411"}
    assert len(details(p, "37319411")) == 1
    assert ai_calls == ["37319411"]
    p.runner.deliver_tick()
    assert len(p.sent) == 100


@pytest.mark.parametrize("hourly,daily,total,allowed", [
    (424, 8256, 49286, True),
    (688, 9000, 50000, True),
    (689, 9000, 50000, False),
    (200, 9568, 50000, True),
    (200, 9569, 50000, False),
    (200, 8000, 89968, True),
    (200, 8000, 89969, False),
    (900, 9000, 50000, False),
])
def test_reserved_capacity_keeps_hourly_daily_and_total_bounds(p, hourly, daily, total, allowed):
    with Session(p.engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        row.calls = [p.clock[0]] * hourly + [p.clock[0] - 7200] * (daily - hourly)
        row.total = total
        db.commit()
        assert active_window.budget_available(db, BudgetLimits(900, 12000, 90000)) is allowed


def test_old_backlog_leaves_extra_capacity_for_fresh_arrivals(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [p.clock[0]] * 424
        db.add(MonitorJob(source_id="999", first_seen=p.clock[0] - 601,
                          result={"discovery_kind": active_window.KIND}))
        db.commit()
    selected = []
    monkeypatch.setattr(p.runner, "evaluate", lambda source_id, source: selected.append(source_id))
    assert not p.runner.tick()
    assert selected == []
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="37319411", first_seen=p.clock[0],
                          result={"discovery_kind": active_window.KIND}))
        db.commit()
    assert p.runner.tick()
    assert selected == ["37319411"]
    state = runtime_status(p.engine, True, active_window_enabled=True)["active_window"]
    assert state["valuation_budget_available"] and not state["backlog_budget_available"]
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "999").state == "pending"
        db.get(MonitorJob, "37319411").state = "checked"
        db.get(SourceBudget, "auto_ria").calls = []
        db.commit()
    assert p.runner.tick()
    assert selected == ["37319411", "999"]


def queue_supplement_without_evaluation(p):
    p.stock = ["124", "123"]
    wake(p, 301)
    for _ in range(20):
        assert p.runner.tick()
        with Session(p.engine) as db:
            job = db.get(MonitorJob, "124")
            if job is not None:
                assert job.state == "pending" and job.attempts == 0
                return
    pytest.fail("supplemental arrival was not queued")


def test_disabled_supplement_can_only_return_after_primary_publication_confirmation(p, monkeypatch):
    enable_window(p, monkeypatch)
    add_search(p, sid=2, uid=222)
    drain(p)
    queue_supplement_without_evaluation(p)
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    p.runner.sync()
    # An old/unconfirmed first-page entry stays retired even with ample quota.
    wake(p, 60)
    drain(p)
    assert not p.sent and not details(p, "124")
    before = len(p.calls)
    # Only the authoritative publication-window result can admit it again.
    p.ads["124"] = p.clock[0] + 1
    wake(p, 60)
    drain(p)
    assert {(uid, car.source_id) for uid, car in p.sent} == {(111, "124"), (222, "124")}
    assert len(details(p, "124")) == 1
    assert all("published_after" in params or "generation_id[0][0]" in params
               for path, params in p.calls[before:] if path == "search")
    wake(p, 3601)
    drain(p)
    assert len(p.sent) == 2 and len(details(p, "124")) == 1


def test_primary_confirmation_promotes_pending_supplement_before_disabling(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    queue_supplement_without_evaluation(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p, 60)
    assert p.runner.tick()  # Primary publication search confirms the same ID.
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    assert p.sent[0][1].pipeline["discovery_kind"] == "new_publication"


def test_primary_confirmation_of_retired_supplement_does_not_revive_stopped_search(p, monkeypatch):
    enable_window(p, monkeypatch)
    add_search(p, sid=2, uid=222)
    drain(p)
    queue_supplement_without_evaluation(p)
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    p.runner.sync()
    with Session(p.engine) as db:
        db.get(Search, 2).enabled = False
        reset_watch(db, 2, False)
        db.commit()
    p.ads["124"] = p.clock[0] + 1
    wake(p, 60)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 2) is None
        assert db.get(Search, 2).enabled is False


@pytest.mark.parametrize("legacy_origin", [False, True])
def test_disabling_also_retires_supplemental_price_refresh_already_requested_by_delivery(
        p, monkeypatch, legacy_origin):
    enable_window(p, monkeypatch)
    drain(p)
    queue_supplement_without_evaluation(p)
    for _ in range(20):
        if not p.runner.tick():
            break
    enqueue(p.engine)
    checked = len(details(p, "124"))
    p.clock[0] += 601
    p.runner.deliver_tick()  # Stale price requests normal revalidation, without sending.
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "pending"
        if legacy_origin:
            db.get(MonitorJob, "124").result = {}  # Pre-upgrade refresh lost provenance.
            db.commit()
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    drain(p)
    assert len(details(p, "124")) == checked
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "cancelled"


def test_primary_search_does_not_reopen_other_cancellations(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="124", state="cancelled", first_seen=p.clock[0]))
        db.add(MonitorSeen(search_id=1, source_id="124", epoch=db.get(MonitorWatch, 1).epoch,
                           state="cancelled", first_seen=p.clock[0]))
        db.commit()
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    p.ads["124"] = p.clock[0] + 1
    wake(p, 60)
    drain(p)
    assert not p.sent and not details(p, "124")


def test_retired_interest_respects_production_varchar_limit(p, monkeypatch):
    enable_window(p, monkeypatch)
    drain(p)
    queue_supplement_without_evaluation(p)
    # SQLite does not enforce VARCHAR lengths; reproduce PostgreSQL's bound
    # during the actual cancellation transition, without changing the schema.
    maximum = MonitorSeen.__table__.c.state.type.length
    with p.engine.begin() as connection:
        connection.exec_driver_sql(f"""CREATE TRIGGER enforce_seen_state_length
            BEFORE UPDATE OF state ON monitor_seen
            WHEN length(NEW.state) > {maximum}
            BEGIN SELECT RAISE(ABORT, 'state exceeds production varchar limit'); END""")
    p.runner.settings = replace(p.settings, ria_active_window_enabled=False)
    p.runner.sync()
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (1, "124")).state == active_window.RETIRED_STATE
        assert db.get(MonitorJob, "124").state == "cancelled"
