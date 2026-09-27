"""Cross-feed acceleration through the real discovery/evaluation/queue path."""
from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend import active_window, shared_distribution
from backend.models import (Delivery, DeliveryTiming, Listing, MonitorActiveWindow, MonitorFeed,
    MonitorJob, MonitorMatch, MonitorMembership, MonitorSeen, Range, Search, SourceCache, User)
from backend.monitor import Monitor, reset_watch
from backend.tests.test_monitor import p, add_search, drain, wake, details
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_active_window import enable_window
from backend.worker import enqueue


def setup(p, monkeypatch, **quote_args):
    p.settings = replace(p.settings, ria_shared_distribution_enabled=True)
    calls = enable(p, monkeypatch, **quote_args)
    p.settings = p.runner.settings
    return calls


def align_subscriptions(p):
    # Both subscriptions have existed longer than the 10-minute index overlap.
    drain(p)
    wake(p, 601)
    drain(p)


def discover_first_only(p, sid="124"):
    p.ads[sid] = p.clock[0] + 1
    wake(p)
    with Session(p.engine) as db:
        first = db.get(MonitorMembership, 1).feed_id
        db.execute(update(MonitorFeed).where(MonitorFeed.id != first).values(next_poll=p.clock[0] + 180))
        db.commit()
    assert p.runner.tick()
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (1, sid)).state == "pending"
        assert db.get(MonitorSeen, (2, sid)) is None


def dispatch(p, count=4):
    for _ in range(count):
        p.runner.deliver_tick()


def test_two_compatible_groups_receive_one_quote_before_second_poll_and_after_restart(p, monkeypatch):
    calls = setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    before = len(p.calls)
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    dispatch(p)
    assert {(uid, car.source_id) for uid, car in p.sent} == {(111, "124"), (222, "124")}
    assert calls == ["124"] and len(details(p, "124")) == 1
    assert [path for path, _ in p.calls[before:]] == ["info"]
    with Session(p.engine) as db:
        times = list(db.scalars(select(DeliveryTiming)))
        assert len(times) == 2
        assert max(t.accepted_at for t in times) - min(t.accepted_at for t in times) < 1
        assert db.get(MonitorFeed, db.get(MonitorMembership, 2).feed_id).next_poll > p.clock[0] + 170
    p.clock[0] += 180
    drain(p)
    dispatch(p)
    assert len(p.sent) == 2 and calls == ["124"] and len(details(p, "124")) == 1


@pytest.mark.parametrize("change", [
    {"price": Range(to=9999)}, {"price": Range(**{"from": 10001})},
    {"year": Range(to=2016)}, {"mileage": Range(to=99)},
    {"minDiscount": 50}, {"brand": "Other", "model": ""}, {"model": "Other"}, {"region": "Other область"},
    {"fuel": ["Other"]}, {"transmission": ["Other"]}, {"body": ["Other"]}])
def test_other_users_filters_and_discount_are_not_bypassed(p, monkeypatch, change):
    setup(p, monkeypatch)
    # Official, cached IDs differ even when labels are valid dictionary names.
    align_subscriptions(p)
    with Session(p.engine) as db:
        for path in ("categories/1/marks", "categories/1/marks/84/models", "states", "type",
                     "categories/1/bodystyles", "categories/1/gearboxes"):
            digest = hashlib.sha256(json.dumps([path, {}], sort_keys=True).encode()).hexdigest()
            row = db.get(SourceCache, digest)
            items = row.payload["items"] if row else []
            db.merge(SourceCache(id=digest, expires_at=p.clock[0]+1000,
                payload={"items": [*items, {"name": "Other", "value": 999}]}))
        db.commit()
    p.clock[0] -= 602
    add_search(p, **({"price": Range(to=20000)} | change))
    p.clock[0] += 602
    p.runner.sync()
    discover_first_only(p)
    drain(p)
    dispatch(p)
    assert [uid for uid, _ in p.sent] == [111]


@pytest.mark.parametrize("change", ["stop", "disable", "reactivate", "edit", "late_join"])
def test_changes_while_quote_is_in_flight_do_not_attach_old_epoch(p, monkeypatch, change):
    def during_quote():
        if change == "late_join":
            add_search(p, sid=3, uid=333, price=Range(to=21000))
            return
        with Session(p.engine) as db:
            if change == "stop":
                db.get(User, 222).ready = False
            elif change == "disable":
                db.get(Search, 2).enabled = False
            else:
                if change == "edit":
                    search = db.get(Search, 2)
                    filters = p.filters.model_copy(update={"price": Range(to=21000)})
                    search.filters, search.fingerprint = filters.canonical(), filters.fingerprint()
                reset_watch(db, 2, True)
            db.commit()
    setup(p, monkeypatch, action=during_quote)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    # One evaluation only: do not start a newly created group's ordinary poll.
    assert p.runner.tick()
    dispatch(p)
    expected = {111, 222} if change == "late_join" else {111}
    assert {uid for uid, _ in p.sent} == expected
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (3 if change == "late_join" else 2, "124")) is None


@pytest.mark.parametrize("state", ["sent", "uncertain", "failed", "pending", "cancelled"])
def test_existing_delivery_claims_and_seen_ids_are_untouched(p, monkeypatch, state):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    with Session(p.engine) as db:
        listing = Listing(source="auto_ria", source_id="124", car={})
        db.add(listing)
        db.flush()
        delivery = Delivery(user_id=222, listing_id=listing.id, state=state, retry_at=p.clock[0] + 1000)
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
    drain(p)
    dispatch(p)
    assert [uid for uid, _ in p.sent] == [111]
    with Session(p.engine) as db:
        assert db.get(Delivery, delivery_id).state == state
        assert db.get(MonitorSeen, (2, "124")) is None


def test_previously_checked_search_is_not_rescanned(p, monkeypatch):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    with Session(p.engine) as db:
        member = db.get(MonitorMembership, 2)
        db.add(MonitorSeen(search_id=2, source_id="124", epoch=member.epoch, state="checked", first_seen=p.clock[0]-100))
        db.commit()
    drain(p)
    assert [uid for uid, _ in p.sent] == [111]


def test_stop_between_shared_snapshot_and_user_lock_is_rechecked(p, monkeypatch):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    original = shared_distribution.targets
    def after_snapshot(db, *args):
        targets = original(db, *args)
        assert any(row[1] == 222 for row in targets)
        with Session(p.engine) as other:
            other.get(User, 222).ready = False
            other.commit()
        return targets
    monkeypatch.setattr(shared_distribution, "targets", after_snapshot)
    assert p.runner.tick()
    dispatch(p)
    assert [uid for uid, _ in p.sent] == [111]


def test_two_matching_searches_of_same_user_queue_only_one_delivery(p, monkeypatch):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    add_search(p, sid=3, uid=222, price=Range(to=21000), region=["Хмельницька область"])
    align_subscriptions(p)
    discover_first_only(p)
    drain(p)
    dispatch(p)
    assert sorted(uid for uid, _ in p.sent) == [111, 222]
    with Session(p.engine) as db:
        assert len(list(db.scalars(select(MonitorMatch)))) == 3


@pytest.mark.parametrize("allowed", [False, True])
def test_condition_exclusions_remain_in_force_but_repairs_are_allowed(p, monkeypatch, allowed):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    align_subscriptions(p)
    discover_first_only(p)
    from backend.ria_search import RiaSearch
    old = RiaSearch.car
    def car(self, *args, **kw):
        candidate = old(self, *args, **kw)
        candidate["condition_exclusions"] = ["damage", "onRepairParts"] if allowed else ["abroad"]
        return candidate
    monkeypatch.setattr(RiaSearch, "car", car)
    drain(p)
    dispatch(p)
    assert {uid for uid, _ in p.sent} == ({111, 222} if allowed else set())


@pytest.mark.parametrize("missing_quote", [False, True])
def test_information_cards_and_missing_optional_details_share_safely(p, monkeypatch, missing_quote):
    calls = setup(p, monkeypatch, **({"error": "ai_upstream_error"} if missing_quote else {}))
    add_search(p, price=Range(to=20000), minDiscount=10)
    align_subscriptions(p)
    discover_first_only(p)
    original_car = p.runner.search_factory
    def factory(*args):
        source = original_car(*args)
        old = source.car
        def car(*a, **kw):
            candidate = old(*a, **kw)
            candidate.update(fuel_id=None, gear_id=None, body_id=None, mileage=None)
            return candidate
        # fork() returns RiaSearch rather than this instance override.
        source.fork = lambda: source
        source.car = car
        return source
    p.runner.search_factory = factory
    drain(p)
    dispatch(p)
    assert {uid for uid, _ in p.sent} == {111, 222} and calls == ["124"]
    assert all((car.market is None) == missing_quote for _, car in p.sent)


@pytest.mark.parametrize("initial", [False, True])
def test_active_window_fanout_requires_explicit_initial_opt_in(p, monkeypatch, initial):
    enable_window(p, monkeypatch)
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000))
    # Establish baselines, then surface an unseen old ad in the first group.
    drain(p)
    p.runner.settings = replace(p.runner.settings, ria_active_window_include_initial=initial)
    p.stock = ["124", "123"]
    p.clock[0] += 10
    with Session(p.engine) as db:
        first = db.get(MonitorMembership, 1).feed_id
        db.execute(update(MonitorFeed).values(next_poll=p.clock[0]+1000))
        db.execute(update(MonitorActiveWindow).values(next_poll=p.clock[0]+1000))
        db.get(MonitorActiveWindow, first).next_poll = 0
        db.commit()
    drain(p)
    dispatch(p)
    assert {uid for uid, _ in p.sent} == ({111, 222} if initial else {111})
    assert len(details(p, "124")) == 1


def test_expired_or_missing_dictionary_skips_extra_group_without_provider_calls(p, monkeypatch):
    setup(p, monkeypatch)
    add_search(p, price=Range(to=20000), transmission=["Автомат"])
    align_subscriptions(p)
    discover_first_only(p)
    with Session(p.engine) as db:
        digest = hashlib.sha256(json.dumps(["categories/1/gearboxes", {}], sort_keys=True).encode()).hexdigest()
        db.get(SourceCache, digest).expires_at = p.clock[0] - 1
        db.commit()
    before = len(p.calls)
    drain(p)
    assert [uid for uid, _ in p.sent] == [111]
    assert [path for path, _ in p.calls[before:]] == ["info"]


def test_two_hundred_different_compatible_filters_share_one_detail_and_quote(p, monkeypatch):
    calls = setup(p, monkeypatch)
    p.clock[0] -= 2
    for sid in range(2, 201):
        add_search(p, sid=sid, uid=1000+sid, price=Range(to=10000+sid))
    p.clock[0] += 2
    p.runner.sync()
    discover_first_only(p)
    assert p.runner.tick()
    enqueue(p.engine, require_provider_range=True)
    with Session(p.engine) as db:
        deliveries = list(db.scalars(select(Delivery)))
        assert len(deliveries) == len({row.user_id for row in deliveries}) == 200
        assert {row.state for row in deliveries} == {"pending"}
    assert calls == ["124"] and len(details(p, "124")) == 1
    # This proves queue fan-out and request reuse, not 200 real messages/s.


@pytest.mark.parametrize("feed_due", [False, True])
def test_fresh_jobs_lead_batch_with_one_old_primary_slot_and_search_reservation(p, monkeypatch, feed_due):
    setup(p, monkeypatch)
    align_subscriptions(p)
    p.ads.update({str(sid): p.clock[0]+1 for sid in range(200, 208)})
    wake(p)
    assert p.runner.tick()
    with Session(p.engine) as db:
        for sid in range(200, 204):
            db.get(MonitorJob, str(sid)).first_seen = p.clock[0] - 3600
        if feed_due:
            db.execute(update(MonitorFeed).values(next_poll=0))
        db.commit()
    tasks = []
    def batch(selected, groups):
        tasks.extend(selected)
        return "evaluate", True
    monkeypatch.setattr(p.runner, "parallel_step", batch)
    assert p.runner.tick()
    evaluated = [sid for kind, sid in tasks if kind == "evaluate"]
    assert len(tasks) == 4
    assert sum(kind == "discover" for kind, _ in tasks) == int(feed_due)
    assert evaluated[-1] == "200"
    assert all(int(sid) >= 204 for sid in evaluated[:-1])


@pytest.mark.parametrize("metadata", [{}, {"publication_after": float("nan")},
    {"publication_after": True}, {"publication_after": 9999999999999},
    {"shared_observed_at": 1800000000}])
def test_unproven_publication_never_uses_local_observation_as_activation_proof(p, monkeypatch, metadata):
    setup(p, monkeypatch)
    assert shared_distribution.cutoff(p.settings, metadata) is None
