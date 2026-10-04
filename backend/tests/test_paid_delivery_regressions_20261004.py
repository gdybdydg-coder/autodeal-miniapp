"""Production paid policy through real discovery, queue and transport boundaries.

SQLite and fixture AUTO.RIA/Telegram only. No production data or network.
"""
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import delivery_diagnostic, paid_source_access
from backend.billing_models import Entitlement
from backend.manual_payment_models import PaymentRequest
from backend.models import Delivery, DeliveryTiming, Listing, Search, SourceBudget, User
from backend.monitor import Monitor
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import approve, strict
from backend.tests.test_ria_ai_price import enable
from backend.worker import TelegramSender, deliver_one, enqueue


def queued(p, monkeypatch):
    """A normally discovered/evaluated new car, with its dispatch paused."""
    quotes = enable(p, monkeypatch)
    drain(p)
    monkeypatch.setattr(p.runner, "deliver_tick", lambda: None)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    enqueue(p.engine, now=p.clock[0], require_provider_range=True)
    return quotes


@pytest.mark.parametrize("failure_at", ["claimed_access", "transport_access"])
def test_access_read_failure_before_any_transport_is_deferred_with_reason(p, monkeypatch, failure_at):
    strict(p)
    approve(p)
    queued(p, monkeypatch)
    previous = paid_source_access.allowed
    checks = [0]
    def read(db, uid, now=None):
        checks[0] += 1
        if checks[0] == (1 if failure_at == "claimed_access" else 2):
            raise RuntimeError("isolated access read unavailable")
        return previous(db, uid, now)
    monkeypatch.setattr(paid_source_access, "allowed", read)
    before = p.clock[0]
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "pending"
    assert not p.sent
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        timing = db.get(DeliveryTiming, delivery.id)
        assert delivery.retry_at >= before + 60
        assert timing.send_started_at is None and timing.accepted_at is None
        assert delivery_diagnostic.receipt(db, delivery.id)["attempts"][-1]["reason"] == "access_unavailable"
        assert db.get(PaymentRequest, "fixture-111").state == "approved"
        assert db.get(Entitlement, 111).expires_at > p.clock[0]
    # Read recovery retries the same claim and creates no new source calls.
    before_calls = list(p.calls)
    p.clock[0] += 61
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "sent"
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    assert p.calls == before_calls


def test_strict_overlapping_dispatch_and_restart_keep_one_claim_per_paid_user(p, monkeypatch):
    strict(p)
    approve(p)
    add_search(p, sid=2, uid=222)
    approve(p, 222)
    add_search(p, sid=3, uid=333)
    quotes = queued(p, monkeypatch)
    before_calls = list(p.calls)
    with Session(p.engine) as db:
        before_budget = db.get(SourceBudget, "auto_ria").total
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(lambda _: deliver_one(
            p.engine, p.runner.settings, p.runner.sender, now=p.clock[0]), range(4)))
    assert set(states) <= {"sent", "busy", "empty"}
    for _ in range(3):
        deliver_one(p.engine, p.runner.settings, p.runner.sender, now=p.clock[0])
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "124"), (222, "124")]
    assert quotes == ["124"] and p.calls == before_calls
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before_budget
        assert len(list(db.scalars(select(Delivery)))) == 2
        assert {row.state for row in db.scalars(select(Delivery))} == {"sent"}


def test_access_read_failure_before_text_fallback_records_both_rejection_and_defer(p, monkeypatch):
    strict(p)
    approve(p)
    queued(p, monkeypatch)
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        listing.car = {**listing.car, "photo": "https://cdn0.riastatic.com/photosnew/auto/photo/test.jpg"}
        db.commit()
    previous = paid_source_access.allowed
    checks = [0]
    def read(db, uid, now=None):
        checks[0] += 1
        if checks[0] == 3:  # Initial queue check, photo check, then text fallback.
            raise RuntimeError("isolated access read unavailable")
        return previous(db, uid, now)
    monkeypatch.setattr(paid_source_access, "allowed", read)
    requests = []
    def handler(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        if len(requests) == 1:
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                "description": "Bad Request: wrong type of the web page content"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    sender = TelegramSender("fixture-token")
    monkeypatch.setattr(sender.photos, "load", lambda _: None)
    before_calls = list(p.calls)
    assert deliver_one(p.engine, p.runner.settings, sender) == "pending"
    assert requests == ["sendPhoto"]
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        receipt = delivery_diagnostic.receipt(db, delivery.id)
        assert [attempt["reason"] for attempt in receipt["attempts"]] == ["photo_content_type", "access_unavailable"]
        assert not any(attempt["accepted"] for attempt in receipt["attempts"])
        assert db.get(DeliveryTiming, delivery.id).accepted_at is None
    p.clock[0] += 61
    assert deliver_one(p.engine, p.runner.settings, sender) == "sent"
    assert requests == ["sendPhoto", "sendPhoto"]
    assert p.calls == before_calls


def test_strict_timeout_is_uncertain_and_restart_never_replays_it(p, monkeypatch):
    strict(p)
    approve(p)
    quotes = queued(p, monkeypatch)
    calls = []
    def timeout(uid, car):
        calls.append((uid, car.source_id))
        raise httpx.ReadTimeout("isolated ambiguous Telegram response")
    before_calls = list(p.calls)
    assert deliver_one(p.engine, p.runner.settings, timeout) == "uncertain"
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, timeout)
    drain(p)
    assert deliver_one(p.engine, p.runner.settings, timeout) == "empty"
    assert calls == [(111, "124")]
    assert quotes == ["124"] and p.calls == before_calls
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        assert delivery.state == "uncertain"
        assert delivery.message_id is None
        assert db.get(DeliveryTiming, delivery.id).accepted_at is None


@pytest.mark.parametrize("blocker", ["purchase_expired", "stop", "search_disabled"])
def test_strict_rate_limited_retry_rechecks_access_and_search_without_source_cost(p, monkeypatch, blocker):
    strict(p)
    approve(p)
    queued(p, monkeypatch)
    calls = []
    def throttled(uid, car):
        calls.append((uid, car.source_id))
        return {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}
    before_calls = list(p.calls)
    assert deliver_one(p.engine, p.runner.settings, throttled) == "pending"
    with Session(p.engine) as db:
        if blocker == "purchase_expired":
            db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0] + 10
            db.get(Entitlement, 111).expires_at = p.clock[0] + 10
        elif blocker == "stop":
            db.get(User, 111).ready = False
        else:
            db.get(Search, 1).enabled = False
        db.commit()
    p.clock[0] += 31
    assert deliver_one(p.engine, p.runner.settings, throttled) == "cancelled"
    assert calls == [(111, "124")] and p.calls == before_calls
