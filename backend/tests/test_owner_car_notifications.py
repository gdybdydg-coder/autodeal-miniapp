"""Isolated paid-client discovery, queue, owner observation and crash recovery."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import owner_car_notifications as copies, paid_source_access
from backend.billing_models import Entitlement
from backend.manual_payment_models import ManualBase
from backend.models import Car, Delivery, DeliveryTiming, Listing, MonitorJob, Search, SourceProbe, User
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import approve
from backend.tests.test_ria_ai_price import enable
from backend.worker import TelegramSender, deliver_one, enqueue

OWNER = 999


def configure(p, monkeypatch):
    ManualBase.metadata.create_all(p.engine)
    p.settings = replace(p.settings, admin_telegram_id=OWNER)
    paid_source_access.configure(p.engine, p.settings)
    with Session(p.engine) as db:
        db.add(User(id=OWNER, ready=True))
        db.commit()
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


def test_two_paid_clients_one_unpaid_and_owner_receive_one_shared_car(p, monkeypatch):
    quotes = configure(p, monkeypatch)
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    approve(p,111); approve(p,222)
    publish(p)
    assert sorted(uid for uid, _ in p.sent) == [111, 222, OWNER]
    assert quotes == ["124"]
    assert sum(path == "info" and params["auto_id"] == "124" for path, params in p.calls) == 1
    with Session(p.engine) as db:
        assert not paid_source_access.allowed(db, OWNER, p.clock[0])
        assert db.scalar(select(func.count(Search.id)).where(Search.user_id == OWNER)) == 0
        assert db.get(Entitlement, OWNER) is None
    assert len(owner_deliveries(p)) == 1
    card = next(car for uid, car in p.sent if uid == OWNER)
    text, _ = TelegramSender.card(card)
    assert "Копія клієнтського сповіщення" in text and "Дані на" in text


@pytest.mark.parametrize("clients", [3, 200])
def test_every_matching_paid_client_gets_card_owner_only_once(p, monkeypatch, clients):
    quotes = configure(p, monkeypatch)
    approve(p,111)
    for sid in range(2, clients+1):
        add_search(p, sid=sid, uid=1000+sid)
        approve(p,1000+sid)
    publish(p)
    for _ in range(clients + 3):
        p.runner.deliver_tick()
    expected = {111, OWNER, *(1000+sid for sid in range(2, clients+1))}
    assert {uid for uid, _ in p.sent} == expected
    assert len(p.sent) == clients + 1 and quotes == ["124"]
    assert len(owner_deliveries(p)) == 1


@pytest.mark.parametrize("change", ["stop", "expire", "disable", "filter"])
def test_paid_client_final_checks_still_exclude_ineligible_recipient(p, monkeypatch, change):
    configure(p, monkeypatch)
    add_search(p, sid=2, uid=222)
    approve(p,111); approve(p,222)
    queue_new(p)
    with Session(p.engine) as db:
        if change == "stop": db.get(User,222).ready = False
        elif change == "expire": db.get(Entitlement,222).expires_at = p.clock[0]
        elif change == "disable": db.get(Search,2).enabled = False
        else:
            search = db.get(Search,2)
            search.fingerprint = "edited-after-queue"
        db.commit()
    for _ in range(6): p.runner.deliver_tick()
    assert {uid for uid, _ in p.sent} == {111,OWNER}


def test_owner_without_paid_clients_never_calls_api_or_receives_cars(p, monkeypatch):
    quotes = configure(p, monkeypatch)
    add_search(p,sid=2,uid=OWNER)
    approve(p,OWNER)
    publish(p)
    assert not p.sent and not p.calls and not quotes
    assert not owner_deliveries(p)


@pytest.mark.parametrize("state", ["pending", "failed", "uncertain", "cancelled"])
def test_owner_requires_actual_confirmed_client_receipt(p, monkeypatch, state):
    configure(p, monkeypatch); approve(p)
    publish(p)
    with Session(p.engine) as db:
        copy = db.scalar(select(Delivery).where(Delivery.user_id == OWNER))
        db.delete(db.get(SourceProbe,copies.marker(copy.id)))
        db.delete(db.get(DeliveryTiming,copy.id));db.delete(copy)
        client = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        client.state = state
        db.commit()
    before = len(p.sent)
    copies.enqueue_confirmed(p.engine,p.settings)
    assert not owner_deliveries(p) and len(p.sent) == before


def test_gift_cannot_create_owner_copy_even_with_legacy_sent_record(p, monkeypatch):
    configure(p, monkeypatch); approve(p)
    publish(p)
    with Session(p.engine) as db:
        copy = db.scalar(select(Delivery).where(Delivery.user_id == OWNER))
        db.delete(db.get(SourceProbe,copies.marker(copy.id)))
        db.delete(db.get(DeliveryTiming,copy.id));db.delete(copy)
        from backend.manual_payment_models import PaymentRequest
        db.get(PaymentRequest,"fixture-111").state = "cancelled"
        db.commit()
    copies.enqueue_confirmed(p.engine,p.settings)
    assert not owner_deliveries(p)


def test_crash_before_owner_enqueue_recovers_from_durable_client_receipt(p, monkeypatch):
    configure(p, monkeypatch); approve(p)
    queue_new(p)
    assert deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0]) == "sent"
    assert not owner_deliveries(p)
    copies.initialize(p.engine,p.settings)  # Restart does not move the baseline.
    copies.enqueue_confirmed(p.engine,p.settings)
    assert len(owner_deliveries(p)) == 1
    assert deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0]) == "sent"
    copies.enqueue_confirmed(p.engine,p.settings)
    assert len(owner_deliveries(p)) == 1 and len(p.sent) == 2


def test_owner_stop_rechecked_after_copy_queue(p, monkeypatch):
    configure(p,monkeypatch); approve(p)
    queue_new(p)
    deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0])
    copies.enqueue_confirmed(p.engine,p.settings)
    with Session(p.engine) as db:
        db.get(User,OWNER).ready=False;db.commit()
    assert deliver_one(p.engine,p.settings,lambda *a: pytest.fail("owner stopped"),now=p.clock[0]) == "cancelled"
    assert [uid for uid,_ in p.sent] == [111]


@pytest.mark.parametrize("result,state", [({"uncertain":True},"uncertain"),
    ({"error_code":429,"parameters":{"retry_after":7}},"pending")])
def test_owner_transport_uses_existing_timeout_and_rate_limit_rules(p,monkeypatch,result,state):
    configure(p,monkeypatch);approve(p);publish(p)
    with Session(p.engine) as db:
        copy=db.scalar(select(Delivery).where(Delivery.user_id==OWNER));copy.state="pending"
        db.commit()
    p.clock[0]+=2
    assert deliver_one(p.engine,p.settings,lambda *a:result,now=p.clock[0]) == state
    if state=="uncertain":
        copies.enqueue_confirmed(p.engine,p.settings)
        assert deliver_one(p.engine,p.settings,lambda *a:pytest.fail("blind repeat"),now=p.clock[0]+100) == "empty"


def test_owner_snapshot_immutable_and_stale_copy_does_not_refresh_api(p,monkeypatch):
    configure(p,monkeypatch);approve(p)
    queue_new(p)
    deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0])
    copies.enqueue_confirmed(p.engine,p.settings)
    with Session(p.engine) as db:
        listing=db.scalar(select(Listing));listing.car={**listing.car,"price":99999}
        db.commit()
    before=len(p.calls);p.clock[0]+=600
    assert deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0]) == "sent"
    assert p.sent[-1][0]==OWNER and p.sent[-1][1].price==10000 and len(p.calls)==before
    assert "Ринкова ціна" in TelegramSender.card(p.sent[-1][1])[0]
    with Session(p.engine) as db: assert db.get(MonitorJob,"124").state != "pending"


def test_switch_off_cancels_pending_owner_copy_only(p,monkeypatch):
    configure(p,monkeypatch);approve(p)
    queue_new(p)
    deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0])
    copies.enqueue_confirmed(p.engine,p.settings)
    off=replace(p.settings,owner_car_notifications_enabled=False)
    assert deliver_one(p.engine,off,lambda *a:pytest.fail("disabled"),now=p.clock[0]) == "cancelled"
    assert [uid for uid,_ in p.sent]==[111]


def test_parallel_enqueuers_cannot_duplicate_owner_card(p,monkeypatch):
    configure(p,monkeypatch);approve(p)
    queue_new(p)
    deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:copies.enqueue_confirmed(p.engine,p.settings),range(4)))
    assert len(owner_deliveries(p))==1


def test_source_failure_in_owner_queue_cannot_stop_client_delivery(p,monkeypatch):
    configure(p,monkeypatch);approve(p)
    def broken(*a): raise ValueError("invalid snapshot")
    monkeypatch.setattr(copies,"_enqueue_confirmed",broken)
    publish(p)
    assert [uid for uid,_ in p.sent]==[111]


def test_requested_bora_recovery_once_without_replaying_other_history(p,monkeypatch):
    configure(p,monkeypatch);approve(p)
    p.prices[copies.REQUESTED_LISTING]=2000
    publish(p,sid=copies.REQUESTED_LISTING)
    with Session(p.engine) as db:
        copy=db.scalar(select(Delivery).where(Delivery.user_id==OWNER))
        db.delete(db.get(SourceProbe,copies.marker(copy.id)))
        db.delete(db.get(DeliveryTiming,copy.id));db.delete(copy)
        db.delete(db.get(SourceProbe,copies.RECOVERY));db.delete(db.get(SourceProbe,copies.CONTROL))
        db.commit()
    p.clock[0]+=3600
    before=len(p.calls)
    copies.initialize(p.engine,p.settings)
    assert len(owner_deliveries(p))==1
    assert deliver_one(p.engine,p.settings,p.runner.sender,now=p.clock[0]) == "sent"
    copies.initialize(p.engine,p.settings)
    assert len(owner_deliveries(p))==1 and len(p.calls)==before
