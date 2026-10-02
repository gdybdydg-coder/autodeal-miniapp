"""Offline queue/access regressions. No Telegram, provider or production keys."""
import time

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend import billing, worker
from backend.app import Settings
from backend.billing_models import BillingControl, Entitlement
from backend.models import Base, Delivery, Filters, Search, User
from backend.tests.test_backend import car


@pytest.fixture
def queue(tmp_path):
    url = "sqlite:///" + str(tmp_path / "delivery-audit.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    settings = Settings(url, "synthetic-token", "synthetic-webhook", True, True)
    now = time.time()
    with Session(engine) as db:
        db.add(BillingControl(id=billing.CONTROL, enforce=True))
        for uid in (111, 222):
            db.add(User(id=uid, ready=True))
            db.add(Search(user_id=uid, fingerprint=str(uid), name="Synthetic",
                          filters=Filters().model_dump(mode="json", by_alias=True), enabled=True))
            db.add(Entitlement(user_id=uid, expires_at=now + 100, updated_at=now))
        db.commit()
    worker.ingest(engine, [car("synthetic-car", observed_at=now)], now=now)
    worker.enqueue(engine, now=now)
    yield engine, settings, now
    engine.dispose()


def accepted():
    return {"ok": True, "result": {"message_id": 1}}


@pytest.mark.parametrize("parameters", [None, [], "unexpected", {"retry_after": None},
    {"retry_after": "not-an-integer"}, {"retry_after": float("inf")}, {"retry_after": True}])
def test_malformed_429_delay_never_strands_claim_or_blocks_other_user(queue, parameters):
    engine, settings, now = queue
    result = {"ok": False, "error_code": 429, "parameters": parameters}
    assert worker.deliver_one(engine, settings, lambda *args: result, now=now) == "pending"
    with Session(engine) as db:
        first = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        assert first.state == "pending" and first.retry_at == now + 60
    recipients = []
    assert worker.deliver_one(engine, settings,
        lambda uid, _: recipients.append(uid) or accepted(), now=now) == "sent"
    assert recipients == [222]


def test_access_expired_while_claiming_is_rechecked_with_current_clock(queue, monkeypatch):
    engine, settings, now = queue
    clock = [now]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    def advance_on_user_lock(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT users."):
            clock[0] = now + 101
    event.listen(engine, "before_cursor_execute", advance_on_user_lock)
    try:
        assert worker.deliver_one(engine, settings,
            lambda *args: pytest.fail("expired access was sent a message")) == "cancelled"
    finally:
        event.remove(engine, "before_cursor_execute", advance_on_user_lock)


def test_retry_after_starts_when_rejection_arrives_not_before_network_wait(queue, monkeypatch):
    engine, settings, now = queue
    clock = [now]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    def rejected(*args):
        clock[0] = now + 15
        return {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}
    assert worker.deliver_one(engine, settings, rejected) == "pending"
    with Session(engine) as db:
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).retry_at == now + 45


@pytest.mark.parametrize("first_state", ["uncertain", "blocked", "stopped", "expired"])
def test_one_ineligible_or_uncertain_recipient_does_not_consume_other_users_copy(queue, first_state):
    engine, settings, now = queue
    calls = []
    if first_state in {"stopped", "expired"}:
        with Session(engine) as db:
            if first_state == "stopped":
                db.get(User, 111).ready = False
                db.scalar(select(Search).where(Search.user_id == 111)).enabled = False
            else:
                db.get(Entitlement, 111).expires_at = now
            db.commit()
    def sender(uid, listing):
        calls.append(uid)
        if uid == 111:
            return {"uncertain": True} if first_state == "uncertain" else {"ok": False, "error_code": 403}
        return accepted()
    result = worker.deliver_one(engine, settings, sender, now=now)
    assert result == {"uncertain": "uncertain", "blocked": "failed",
                      "stopped": "cancelled", "expired": "cancelled"}[first_state]
    assert worker.deliver_one(engine, settings, sender, now=now) == "sent"
    assert calls == ([111, 222] if first_state in {"uncertain", "blocked"} else [222])
    engine.dispose()  # A new connection reads persisted outcomes after restart.
    worker.enqueue(engine, now=now)
    assert worker.deliver_one(engine, settings,
        lambda *args: pytest.fail("replayed a sent/uncertain/cancelled pair"), now=now) == "empty"


def test_access_renewal_is_visible_in_new_send_session_and_does_not_enable_stopped_search(queue):
    engine, settings, now = queue
    with Session(engine) as db:
        db.get(Entitlement, 111).expires_at = now + 1
        db.get(User, 222).ready = False
        db.scalar(select(Search).where(Search.user_id == 222)).enabled = False
        db.commit()
    # Model the committed result of the owner's existing approval, without
    # exercising or changing payment logic and without approving any real user.
    with Session(engine) as db:
        for uid in (111, 222):
            db.get(Entitlement, uid).expires_at = now + 86400
        db.commit()
    recipients = []
    assert worker.deliver_one(engine, settings,
        lambda uid, _: recipients.append(uid) or accepted(), now=now + 2) == "sent"
    assert worker.deliver_one(engine, settings,
        lambda *args: pytest.fail("payment resumed /stop"), now=now + 2) == "cancelled"
    assert recipients == [111]


def test_locked_recipient_is_deferred_without_occupying_another_send_slot(queue, monkeypatch):
    engine, settings, now = queue
    user_selects = []
    real_session = Session
    class LockedUserSession(real_session):
        def scalar(self, statement, *args, **kwargs):
            # SQLite lacks row locks. Model only PostgreSQL's documented result
            # for a locked User row and verify the SQL actually requests it.
            descriptions = getattr(statement, "column_descriptions", [])
            if descriptions and descriptions[0].get("expr") is User:
                sql = str(statement.compile(dialect=postgresql.dialect()))
                user_selects.append(sql)
                assert "FOR UPDATE SKIP LOCKED" in sql, "one chat would block this worker slot"
                user = super().scalar(statement, *args, **kwargs)
                return None if user is not None and user.id == 111 else user
            return super().scalar(statement, *args, **kwargs)
    monkeypatch.setattr(worker, "Session", LockedUserSession)
    recipients = []
    def send(uid, _):
        assert uid != 111, "locked chat was sent a message"
        recipients.append(uid)
        return accepted()
    # Progress in this same work slot, even though busy rows lead the queue.
    assert worker.deliver_one(engine, settings, send, now=now) == "sent"
    with Session(engine) as db:
        item = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        assert item.state == "pending" and item.retry_at > now
    assert recipients == [222] and len(user_selects) == 2


def test_deleted_user_is_cancelled_instead_of_treated_as_temporarily_locked(queue):
    engine, settings, now = queue
    with Session(engine) as db:
        db.delete(db.get(User, 111))
        db.commit()
    assert worker.deliver_one(engine, settings,
        lambda *args: pytest.fail("deleted user sent a message"), now=now) == "cancelled"
    assert worker.deliver_one(engine, settings, lambda *args: accepted(), now=now) == "sent"
