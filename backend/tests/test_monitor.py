import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from backend.app import Settings
from backend.auto_ria import RiaError
from backend.models import (Base, Delivery, Filters, Listing, MonitorControl, MonitorFeed, MonitorJob,
                            MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch,
                            Search, SourceBudget, SourceProbe, User)
from backend.monitor import Monitor, initialize, poll_interval, reset_watch, runtime_status
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch, initialize_budget
from backend.tests.test_ria_search import fixture_fetch, raw
from backend.worker import deliver_one


def add_search(p, sid=2, uid=222, **changes):
    filters = p.filters.model_copy(update=changes)
    with Session(p.engine) as db:
        if db.get(User, uid) is None:
            db.add(User(id=uid, ready=True))
        db.add(Search(id=sid, user_id=uid, name="Golf", filters=filters.canonical(),
                      fingerprint=filters.fingerprint(), enabled=True))
        db.flush()
        reset_watch(db, sid, True)
        db.commit()


@pytest.fixture
def p(tmp_path, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr("backend.monitor.time.time", lambda: clock[0])
    engine = create_engine("sqlite:///" + str(tmp_path / "monitor.db"))
    Base.metadata.create_all(engine)
    initialize_budget(engine)
    initialize(engine)
    settings = Settings("unused", "test-token", "x" * 32, True, True,
                        auto_ria_api_key="test-only", monitor_enabled=True)
    filters = Filters(brand="Volkswagen", model="Golf", region="Хмельницька область", fuel=["Дизель"])
    with Session(engine) as db:
        db.add(SourceProbe(id="telegram-webhook-v1", status="configured", checked_at=time.time(), result={}))
        db.commit()
    calls, sent, ads, prices = [], [], {"123": clock[0] - 3600}, {}
    base = fixture_fetch(calls)
    def fetch(key, path, params):
        if path == "search":
            calls.append((path, dict(params)))
            if "generation_id[0][0]" in params:
                ids = [str(n) for n in range(90000, 90005)]
                total = len(ids)
            else:
                assert "created_after" in params and "created_before" in params
                after = datetime.fromisoformat(params["created_after"]).timestamp()
                before = datetime.fromisoformat(params["created_before"]).timestamp()
                ids = sorted((sid for sid, created in ads.items() if after < created < before),
                             key=lambda sid: (ads[sid], sid), reverse=True)
                total = len(ids)
                start = params["page"] * params["countpage"]
                ids = ids[start:start + params["countpage"]]
            return {"result": {"search_result": {"ids": ids, "count": total}}}
        if path == "info":
            calls.append((path, dict(params)))
            sid = params["auto_id"]
            data = raw(sid, USD=prices.get(sid, 15000 if int(sid) >= 90000 else 10000))
            data["autoData"]["fuelName"] = "Дизель, 2 л."
            return data
        return base(key, path, params)
    def factory(engine, key):
        return RiaSearch(engine, key, fetch=fetch, limits=BudgetLimits(900, 3000, 90000))
    def sender(uid, car):
        sent.append((uid, car))
        return {"ok": True, "result": {"message_id": len(sent)}}
    p = SimpleNamespace(engine=engine, settings=settings, filters=filters, clock=clock,
                        calls=calls, sent=sent, ads=ads, prices=prices)
    p.runner = Monitor(engine, settings, factory, sender)
    add_search(p, sid=1, uid=111)
    clock[0] += 2
    yield p
    engine.dispose()


def wake(p, seconds=60):
    p.clock[0] += seconds
    with Session(p.engine) as db:
        db.execute(update(MonitorFeed).values(next_poll=0))
        db.commit()


def drain(p, limit=600):
    for _ in range(limit):
        worked = p.runner.tick()
        p.runner.deliver_tick()
        p.clock[0] += .001
        if not worked:
            return
    pytest.fail("monitor did not reach a wait/checkpoint")


def searches(p):
    return [params for path, params in p.calls if path == "search" and "created_after" in params]


def details(p, sid):
    return [params for path, params in p.calls if path == "info" and params["auto_id"] == sid]


def test_initial_activation_ignores_old_ads_then_restart_delivers_new_once(p):
    drain(p)
    assert not p.sent and not details(p, "123")
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    assert p.sent[0][1].market == 15000 and p.sent[0][1].comparables == 5
    wake(p)
    drain(p)
    assert len(p.sent) == 1 and len(details(p, "124")) == 1
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(Delivery)))) == 1
        assert db.get(MonitorSeen, (1, "123")) is None


def test_more_than_fifty_new_ads_survive_restart_and_are_all_evaluated(p):
    drain(p)
    p.ads.update({str(n): p.clock[0] + 1 for n in range(1000, 1117)})
    wake(p)
    assert p.runner.tick()  # Persist first page before any valuation.
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.context["page"] == 1
        assert len(list(db.scalars(select(MonitorJob)))) == 50
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert {car.source_id for _, car in p.sent} == {str(n) for n in range(1000, 1117)}
    assert len(p.sent) == 117
    with Session(p.engine) as db:
        assert not db.scalar(select(MonitorJob).where(MonitorJob.state == "pending"))
        assert db.scalar(select(MonitorFeed)).context == {}
    assert {params["page"] for params in searches(p)} == {0, 1, 2}


def test_manual_cached_price_cannot_authorize_a_notification(p):
    drain(p)
    source = p.runner.search_factory(p.engine, "test")
    source.acquire()
    source.car("124")
    source.release()
    p.prices["124"] = 30000
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert not p.sent and len(details(p, "124")) == 2


def test_pending_job_older_than_five_minutes_is_retained_and_price_rechecked(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    assert not details(p, "124")
    wake(p, 601)
    p.prices["124"] = 30000
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (1, "124")).state == "checked"
    assert details(p, "124") and not p.sent


def test_stop_during_info_invalidates_recipient_and_inflight_match(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    factory = p.runner.search_factory
    def stop_on_info(engine, key):
        source = factory(engine, key)
        original = source.car
        def car(sid, **kwargs):
            if sid == "124":
                with Session(engine) as db:
                    db.get(Search, 1).enabled = False
                    reset_watch(db, 1, False)
                    db.commit()
            return original(sid, **kwargs)
        source.car = car
        return source
    p.runner.search_factory = stop_on_info
    wake(p)
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        assert not list(db.scalars(select(MonitorMatch)))


def test_quota_wait_keeps_discovery_checkpoint_and_makes_no_upstream_calls(p):
    drain(p)
    before = len(p.calls)
    with Session(p.engine) as db:
        checkpoint = db.scalar(select(MonitorFeed)).cursor
        db.get(SourceBudget, "auto_ria").total = 90000
        db.commit()
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert len(p.calls) == before and not p.sent
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.status == "quota_exceeded" and feed.cursor == checkpoint
        assert feed.context and feed.next_poll > time.time()


def test_valuation_hourly_quota_recovers_after_restart_without_losing_job(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    with Session(p.engine) as db:
        db.get(SourceBudget, "auto_ria").calls = [time.time()] * 900
        db.commit()
    before = len(p.calls)
    p.runner.tick()
    assert len(p.calls) == before
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "124")
        assert job.state == "pending" and job.reason == "quota_exceeded"
        assert job.next_run >= time.time() + 3600
    wake(p, 3601)
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124"]


def test_identical_filters_share_one_poll_and_valuation_for_two_users(p):
    add_search(p)
    drain(p)
    p.calls.clear()
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert {uid for uid, _ in p.sent} == {111, 222}
    assert len(details(p, "124")) == 1
    # Activation boundaries require temporary extra slices during the lookback.
    wake(p, 181)
    drain(p)
    wake(p)
    p.calls.clear()
    drain(p)
    assert len(searches(p)) == 1
    assert runtime_status(p.engine, True)["active_filter_groups"] == 1


def test_multiple_filters_share_valuation_and_overlapping_subscriptions_deduplicate(p):
    add_search(p, sid=2, uid=111, region="")
    add_search(p, sid=3, uid=222, region="")
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    assert sorted(uid for uid, _ in p.sent) == [111, 222]
    assert len(details(p, "124")) == 1
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorMatch)))) == 3
    assert runtime_status(p.engine, True)["active_filter_groups"] == 2


def test_late_joiner_never_receives_existing_group_backlog(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()  # Work is queued for the original user only.
    p.clock[0] += 2
    add_search(p)
    p.ads["125"] = p.clock[0] + 2
    wake(p)
    drain(p)
    assert {(uid, car.source_id) for uid, car in p.sent} == {(111, "124"), (111, "125"), (222, "125")}


def test_reenable_cancels_old_epoch_and_does_not_replay_backlog(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.sender = lambda *_: {"error_code": 429, "parameters": {"retry_after": 1}}
    drain(p)
    with Session(p.engine) as db:
        reset_watch(db, 1, True)
        db.commit()
    assert deliver_one(p.engine, p.settings, lambda *_: pytest.fail("old epoch"), now=time.time()+2) == "cancelled"
    wake(p)
    drain(p)
    assert not p.sent


def test_deleted_head_during_pagination_is_recovered_by_verification_pass(p):
    drain(p)
    p.ads.update({str(n): p.clock[0] + 1 for n in range(1000, 1100)})
    wake(p)
    p.runner.tick()  # 1099..1050; deletion shifts 1049 into the preceding page.
    del p.ads["1099"]
    drain(p)
    assert {car.source_id for _, car in p.sent} == {str(n) for n in range(1000, 1100)}
    assert len(details(p, "1049")) == 1


def test_new_head_is_deferred_to_next_frozen_window_without_skipping_pages(p):
    drain(p)
    p.ads.update({str(n): p.clock[0] + 1 for n in range(1000, 1070)})
    wake(p)
    p.runner.tick()
    p.ads["2000"] = p.clock[0] + 5
    drain(p)
    assert "2000" not in {car.source_id for _, car in p.sent}
    wake(p)
    drain(p)
    assert len(p.sent) == 71


def test_recent_late_indexing_is_recovered_by_overlap(p):
    drain(p)
    created = p.clock[0] + 1
    wake(p)
    drain(p)
    p.ads["124"] = created  # Appears in the index after its time window completed.
    wake(p)
    drain(p)
    assert [car.source_id for _, car in p.sent] == ["124"]


def test_lease_and_flags_prevent_parallel_or_disabled_provider_work(p):
    assert p.runner.claim()
    other = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    other.tick()
    assert not p.calls and not p.sent
    p.runner.release("idle")
    for settings in (replace(p.settings, monitor_enabled=False), replace(p.settings, delivery_enabled=False),
                     replace(p.settings, source_ready=False)):
        other.settings = settings
        other.tick()
        other.deliver_tick()
    assert not p.calls and not p.sent


def test_fair_interleaving_does_not_block_small_filter_behind_large_feed(p):
    drain(p)
    add_search(p, sid=2, uid=222, region="")
    p.ads.update({str(n): p.clock[0] + 2 for n in range(1000, 1120)})
    wake(p)
    for _ in range(8):
        p.runner.tick()
        p.clock[0] += .01
    with Session(p.engine) as db:
        assert all(w.initialized for w in db.scalars(select(MonitorWatch)))
        assert db.scalar(select(MonitorJob).where(MonitorJob.state == "checked"))


def test_interval_adapts_to_distinct_groups_without_increasing_caps():
    caps = BudgetLimits(900, 3000, 90000)
    assert poll_interval(1, caps) == 60
    assert poll_interval(2, caps) == 116
    assert poll_interval(20, caps) == 1152


def test_delivery_can_run_while_provider_lease_is_held(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    assert not p.sent
    assert p.runner.claim()
    p.runner.deliver_tick()
    assert len(p.sent) == 1
    p.runner.release("idle")


def test_slow_search_response_does_not_turn_poll_interval_into_a_busy_loop(p):
    factory = p.runner.search_factory
    def slow(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(*args):
            p.clock[0] += 3
            return original(*args)
        source.fetch = fetch
        return source
    p.runner.search_factory = slow
    drain(p)
    assert len(searches(p)) == 1
    with Session(p.engine) as db:
        assert db.scalar(select(MonitorFeed)).next_poll > time.time() + 50


@pytest.mark.parametrize("new_price, delivered", [(10000, True), (30000, False)])
def test_old_telegram_queue_rechecks_price_before_dispatch(p, new_price, delivered):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    from backend.worker import enqueue
    enqueue(p.engine)
    p.clock[0] += 301
    p.prices["124"] = new_price
    p.runner.deliver_tick()
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "pending"
    drain(p)
    p.clock[0] += 6
    p.runner.deliver_tick()
    assert bool(p.sent) is delivered
    assert len(details(p, "124")) == 2
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == ("sent" if delivered else "cancelled")


def test_ageing_comparisons_refresh_even_when_candidate_price_is_still_fresh(p):
    drain(p)
    # Seed genuine peer-search observations with 10 seconds of useful life left
    # by the time the next candidate is evaluated.
    source = p.runner.search_factory(p.engine, "test")
    source.acquire()
    try:
        source.comparisons(source.car("777"))
    finally:
        source.release()
    p.ads["124"] = p.clock[0] + 1
    wake(p, 890)
    p.runner.tick()
    p.runner.tick()
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        assert listing and listing.car["market"] == 15000
        assert listing.car["valuation_evidence"]["version"] == "strict-v2"
    # The candidate is only 11 seconds old; its peers are now too old. This also
    # covers stale evidence before the first enqueue, not just an existing queue.
    p.clock[0] += 11
    for sid in range(90000, 90005):
        p.prices[str(sid)] = 10000
    p.runner.deliver_tick()
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "pending"
        assert db.scalar(select(Delivery)).state == "pending"
    drain(p)
    p.clock[0] += 6
    p.runner.deliver_tick()
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").result["rating"]["assessment"] == "not_deal"
        assert db.scalar(select(Delivery)).state == "cancelled"


def test_legacy_listing_without_comparable_proof_is_rechecked_instead_of_sent(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        listing.car = {key: value for key, value in listing.car.items() if key != "valuation_evidence"}
        db.commit()
    p.runner.deliver_tick()
    assert not p.sent
    drain(p)
    p.clock[0] += 6
    p.runner.deliver_tick()
    assert len(p.sent) == 1 and len(details(p, "124")) == 2
    assert p.sent[0][1].valuation_evidence["version"] == "strict-v2"


def test_known_changed_peer_price_invalidates_unexpired_evidence(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    p.clock[0] += 1
    source = p.runner.search_factory(p.engine, "test")
    source.acquire()
    try:
        for sid in range(90000, 90005):
            p.prices[str(sid)] = 10000
            source.car(str(sid), force=True)
    finally:
        source.release()
    p.runner.deliver_tick()
    assert not p.sent
    drain(p)
    p.clock[0] += 6
    p.runner.deliver_tick()
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").result["rating"]["assessment"] == "not_deal"
        assert db.scalar(select(Delivery)).state == "cancelled"


def test_repeated_provider_page_preserves_progress_and_surfaces_error(p):
    drain(p)
    p.ads.update({str(n): p.clock[0] + 1 for n in range(1000, 1120)})
    factory = p.runner.search_factory
    def repeated(engine, key):
        source = factory(engine, key)
        original = source.fetch
        def fetch(key, path, params):
            return original(key, path, {**params, "page": 0} if "created_after" in params else params)
        source.fetch = fetch
        return source
    p.runner.search_factory = repeated
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        feed = db.scalar(select(MonitorFeed))
        assert feed.status == "invalid_response" and feed.context["page"] == 1
        assert feed.cursor < time.time() - 50


def test_uncertain_valuation_is_recorded_without_fake_deal(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    factory = p.runner.search_factory
    def incomplete(engine, key):
        source = factory(engine, key)
        source.comparisons = lambda _: []
        return source
    p.runner.search_factory = incomplete
    wake(p)
    drain(p)
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "unvalued"
        assert db.get(MonitorSeen, (1, "124")).state == "unvalued"
        assert db.get(MonitorJob, "124").result["rating"]["valuation"] != "sample_median"
