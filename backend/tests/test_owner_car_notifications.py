"""Ordinary owner delivery and retirement of historical copy reservations."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import owner_car_notifications as copies, paid_source_access
from backend.billing_models import Entitlement
from backend.manual_payment_models import ManualBase
from backend.models import Delivery, DeliveryTiming, Listing, Search, SourceProbe, User
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import approve
from backend.tests.test_ria_ai_price import enable
from backend.worker import TelegramSender, deliver_one, enqueue

OWNER = 999


def configure(p, monkeypatch, *, own_search=False, own_paid=False, **filters):
    ManualBase.metadata.create_all(p.engine)
    p.settings = replace(p.settings, admin_telegram_id=OWNER)
    paid_source_access.configure(p.engine, p.settings)
    with Session(p.engine) as db:
        db.add(User(id=OWNER, ready=True))
        db.commit()
    if own_search:
        add_search(p, sid=2, uid=OWNER, **filters)
    if own_paid:
        approve(p, OWNER)
    quotes = enable(p, monkeypatch)
    p.settings = p.runner.settings
    copies.initialize(p.engine, p.settings)
    return quotes


def publish(p, sid="124"):
    drain(p)
    p.ads[sid] = p.clock[0] + 1
    wake(p)
    drain(p)
    for _ in range(5):
        p.runner.deliver_tick()


def owner_deliveries(p):
    with Session(p.engine) as db:
        return list(db.scalars(select(Delivery).where(Delivery.user_id == OWNER)))


def queue_new(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    for _ in range(20):
        p.runner.tick()
        with Session(p.engine) as db:
            if db.scalar(select(Listing).where(Listing.source_id == "124")) is not None:
                break
    else:
        pytest.fail("fixture car did not finish evaluation")
    enqueue(p.engine, now=p.clock[0], require_provider_range=True)


def legacy_copy(p, *, state="owner_pending", attempted=False, accepted=False):
    """The old release's durable queue record, without calling its copy logic."""
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        row = db.scalar(select(Delivery).where(Delivery.user_id == OWNER))
        if row is None:
            row = Delivery(user_id=OWNER, listing_id=listing.id, state=state, retry_at=0)
            db.add(row)
            db.flush()
            timing = DeliveryTiming(delivery_id=row.id, queued_at=p.clock[0])
            db.add(timing)
        else:
            row.state = state
            timing = db.get(DeliveryTiming, row.id)
        row.message_id = 90 if accepted else None
        timing.send_started_at = p.clock[0] if attempted else None
        timing.accepted_at = p.clock[0] if accepted else None
        key = copies.marker(row.id)
        probe = db.get(SourceProbe, key)
        if probe is None:
            probe = SourceProbe(id=key, requests=0)
            db.add(probe)
        probe.status, probe.checked_at = "queued", p.clock[0]
        probe.result = {"client_delivery_id": 1, "accepted_at": p.clock[0],
                        "car": listing.car}
        db.commit()
        return row.id, dict(probe.result)


def test_two_paid_clients_one_unpaid_never_create_admin_copy(p, monkeypatch):
    quotes = configure(p, monkeypatch)
    add_search(p, sid=3, uid=222)
    add_search(p, sid=4, uid=333)
    approve(p, 111)
    approve(p, 222)
    publish(p)
    assert sorted(uid for uid, _ in p.sent) == [111, 222]
    assert quotes == ["124"]
    assert sum(path == "info" and params["auto_id"] == "124" for path, params in p.calls) == 1
    assert not owner_deliveries(p)
    with Session(p.engine) as db:
        assert db.get(Entitlement, OWNER) is None
        assert db.scalar(select(func.count(Search.id)).where(Search.user_id == OWNER)) == 0


def test_paid_owner_and_client_each_receive_one_ordinary_matching_card(p, monkeypatch):
    quotes = configure(p, monkeypatch, own_search=True, own_paid=True)
    approve(p)
    publish(p)
    assert sorted(uid for uid, _ in p.sent) == [111, OWNER]
    assert quotes == ["124"]
    assert sum(path == "info" and params["auto_id"] == "124" for path, params in p.calls) == 1
    owner_card = next(car for uid, car in p.sent if uid == OWNER)
    assert "owner_copy" not in (owner_card.pipeline or {})
    text, _ = TelegramSender.card(owner_card)
    assert "Копія" not in text and "Дані на" not in text
    with Session(p.engine) as db:
        assert paid_source_access.allowed(db, OWNER, p.clock[0])
        assert db.get(SourceProbe, copies.marker(owner_deliveries(p)[0].id)) is None
    wake(p)
    drain(p)
    assert len(p.sent) == 2 and quotes == ["124"]


def test_paid_owner_nonmatching_filters_receive_no_client_car(p, monkeypatch):
    configure(p, monkeypatch, own_search=True, own_paid=True, minDiscount=40)
    approve(p)
    publish(p)
    assert [uid for uid, _ in p.sent] == [111]
    assert not owner_deliveries(p)


def test_owner_genuine_purchase_alone_uses_ordinary_pipeline(p, monkeypatch):
    quotes = configure(p, monkeypatch, own_search=True, own_paid=True)
    publish(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(OWNER, "124")]
    assert quotes == ["124"]


def test_owner_role_alone_never_grants_source_access_or_delivery(p, monkeypatch):
    quotes = configure(p, monkeypatch, own_search=True)
    publish(p)
    assert not p.sent and not p.calls and not quotes and not owner_deliveries(p)


@pytest.mark.parametrize("clients", [3, 200])
def test_all_paid_clients_receive_shared_car_without_owner_observation(p, monkeypatch, clients):
    quotes = configure(p, monkeypatch)
    approve(p)
    for sid in range(2, clients + 1):
        add_search(p, sid=sid + 1, uid=1000 + sid)
        approve(p, 1000 + sid)
    publish(p)
    for _ in range(clients + 3):
        p.runner.deliver_tick()
    assert {uid for uid, _ in p.sent} == {111, *(1000 + sid for sid in range(2, clients + 1))}
    assert len(p.sent) == clients and quotes == ["124"]
    assert not owner_deliveries(p)


@pytest.mark.parametrize("change", ["stop", "expire", "disable", "filter"])
def test_owner_ordinary_final_checks_match_client_checks(p, monkeypatch, change):
    configure(p, monkeypatch, own_search=True, own_paid=True)
    approve(p)
    queue_new(p)
    with Session(p.engine) as db:
        if change == "stop":
            db.get(User, OWNER).ready = False
        elif change == "expire":
            db.get(Entitlement, OWNER).expires_at = p.clock[0]
        elif change == "disable":
            db.get(Search, 2).enabled = False
        else:
            db.get(Search, 2).fingerprint = "edited-after-queue"
        db.commit()
    for _ in range(6):
        p.runner.deliver_tick()
    assert [uid for uid, _ in p.sent] == [111]


@pytest.mark.parametrize("state", ["owner_pending", "pending", "cancelled"])
def test_startup_retires_never_attempted_copy_and_preserves_snapshot(p, monkeypatch, state):
    configure(p, monkeypatch)
    approve(p)
    queue_new(p)
    copy_id, original = legacy_copy(p, state=state)
    copies.initialize(p.engine, p.settings)
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == "cancelled"
        assert db.get(DeliveryTiming, copy_id).send_started_at is None
        saved = db.get(SourceProbe, copies.marker(copy_id))
        assert saved.status == "retired_unattempted"
        assert saved.result["legacy_copy"] == original
    for _ in range(4):
        p.runner.deliver_tick()
    assert [uid for uid, _ in p.sent] == [111]


@pytest.mark.parametrize("state", ["sent", "uncertain", "failed", "sending", "owner_pending", "pending"])
def test_startup_does_not_reset_or_replay_attempted_copy(p, monkeypatch, state):
    configure(p, monkeypatch)
    approve(p)
    queue_new(p)
    copy_id, original = legacy_copy(p, state=state, attempted=True, accepted=state == "sent")
    copies.initialize(p.engine, p.settings)
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == state
        assert db.get(SourceProbe, copies.marker(copy_id)).result["legacy_copy"] == original
    for _ in range(4):
        p.runner.deliver_tick()
    assert [uid for uid, _ in p.sent] == [111]


def test_current_paid_match_reuses_retired_copy_claim_as_standard_delivery(p, monkeypatch):
    configure(p, monkeypatch, own_search=True, own_paid=True)
    approve(p)
    queue_new(p)
    copy_id, original = legacy_copy(p)
    copies.initialize(p.engine, p.settings)
    before = len(p.calls)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: enqueue(p.engine, now=p.clock[0], require_provider_range=True), range(4)))
    for _ in range(5):
        p.runner.deliver_tick()
    assert sorted(uid for uid, _ in p.sent) == [111, OWNER]
    assert len(p.calls) == before
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == "sent"
        assert db.get(SourceProbe, copies.marker(copy_id)).status == "ordinary_reclaimed"
        assert db.get(SourceProbe, copies.marker(copy_id)).result["legacy_copy"] == original
    assert "owner_copy" not in (p.sent[-1][1].pipeline or {})


def test_retired_copy_without_own_current_match_never_revives(p, monkeypatch):
    configure(p, monkeypatch, own_search=True, own_paid=True, minDiscount=40)
    approve(p)
    queue_new(p)
    copy_id, _ = legacy_copy(p)
    copies.initialize(p.engine, p.settings)
    for _ in range(5):
        p.runner.deliver_tick()
    assert [uid for uid, _ in p.sent] == [111]
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == "cancelled"


def test_restart_never_recovers_explicit_old_copy_or_replays_client_history(p, monkeypatch):
    configure(p, monkeypatch)
    approve(p)
    p.prices["40514216"] = 2000
    publish(p, sid="40514216")
    before = (len(p.calls), len(p.sent))
    for _ in range(3):
        copies.initialize(p.engine, p.settings)
        p.runner.deliver_tick()
    assert not owner_deliveries(p)
    assert (len(p.calls), len(p.sent)) == before


def test_dispatch_retires_late_predecessor_copy_without_waiting_for_restart(p, monkeypatch):
    configure(p, monkeypatch)
    approve(p)
    queue_new(p)
    copy_id, _ = legacy_copy(p)
    # A predecessor wrote after startup retirement; allow the next bounded
    # cleanup pass without advancing the real test runner's event-loop clock.
    from weakref import WeakKeyDictionary
    monkeypatch.setattr(copies, "_cleanup_checks", WeakKeyDictionary())
    for _ in range(4):
        p.runner.deliver_tick()
    assert [uid for uid, _ in p.sent] == [111]
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == "cancelled"
        assert db.get(SourceProbe, copies.marker(copy_id)).status == "retired_unattempted"


def test_retired_stale_copy_claim_never_refreshes_or_replays_history(p, monkeypatch):
    configure(p, monkeypatch, own_search=True, own_paid=True)
    approve(p)
    queue_new(p)
    copy_id, _ = legacy_copy(p)
    copies.initialize(p.engine, p.settings)
    # Let the legitimate client finish before the old snapshot ages out.
    assert deliver_one(p.engine, p.settings, p.runner.sender, now=p.clock[0]) == "sent"
    before = len(p.calls)
    p.clock[0] += 600
    enqueue(p.engine, now=p.clock[0], require_provider_range=True)
    assert deliver_one(p.engine, p.settings, p.runner.sender, now=p.clock[0]) == "empty"
    assert len(p.calls) == before and [uid for uid, _ in p.sent] == [111]
    with Session(p.engine) as db:
        assert db.get(Delivery, copy_id).state == "cancelled"
