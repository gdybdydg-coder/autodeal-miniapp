"""Optional real PostgreSQL lock checks, restricted to a disposable localhost DB.

Run in CI with AUTODEAL_TEST_POSTGRES_URL using the existing fixture identity.
No source, Telegram, payment, or production database calls are made.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend import billing, worker
from backend.app import Settings
from backend.billing_models import BillingControl, Entitlement
from backend.models import Base, Delivery, Filters, Search, User
from backend.tests.test_backend import car


@pytest.fixture
def postgres_queue():
    configured = os.getenv("AUTODEAL_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("Disposable localhost PostgreSQL fixture not configured")
    url = make_url(configured)
    if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1"
            or url.database != "autodeal_manual_fixture" or url.username != "autodeal_fixture"
            or url.password != "fixture-only" or url.query or not url.port):
        pytest.fail("Expected the isolated localhost delivery fixture database")
    # Do not weaken the external-I/O fence. libpq uses the fully specified,
    # validated loopback target; test data lives only in this generated schema.
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    schema = "delivery_fixture_" + uuid.uuid4().hex
    engine = None
    with admin.begin() as conn:
        conn.execute(text('CREATE SCHEMA "' + schema + '"'))
    try:
        isolated_url = url.update_query_dict({"options":
            "-csearch_path=" + schema + " -cstatement_timeout=5000"})
        engine = create_engine(isolated_url, connect_args={"connect_timeout": 5})
        Base.metadata.create_all(engine)
        now = time.time()
        with Session(engine) as db:
            db.add(BillingControl(id=billing.CONTROL, enforce=True))
            for uid in (111, 222):
                db.add(User(id=uid, ready=True))
                db.add(Search(user_id=uid, fingerprint=str(uid), name="Synthetic",
                    filters=Filters().model_dump(mode="json", by_alias=True), enabled=True))
                db.add(Entitlement(user_id=uid, expires_at=now + 3600, updated_at=now))
            db.commit()
        worker.ingest(engine, [car("car-1"), car("car-2"), car("car-3")], now=now)
        worker.enqueue(engine, now=now)
        settings = Settings(str(isolated_url), "synthetic-token", "synthetic-webhook", True, True)
        yield engine, settings, now
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as conn:
            conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_real_locked_user_backlog_does_not_block_other_recipient(postgres_queue):
    engine, settings, now = postgres_queue
    calls = []
    def sender(uid, listing):
        calls.append((uid, listing.source_id))
        return {"ok": True, "result": {"message_id": 1}}
    with Session(engine) as lock_holder, ThreadPoolExecutor(max_workers=1) as pool:
        lock_holder.scalar(select(User).where(User.id == 111).with_for_update())
        future = pool.submit(worker.deliver_one, engine, settings, sender, now)
        try:
            # The lock remains held while another recipient must be served.
            assert future.result(timeout=2) == "sent"
            assert calls == [(222, "car-1")]
        finally:
            lock_holder.rollback()
    with Session(engine) as db:
        first_user = list(db.scalars(select(Delivery).where(Delivery.user_id == 111)))
        assert len(first_user) == 3 and all(row.state == "pending" for row in first_user)


def test_real_slow_sender_holds_one_user_lock_without_consuming_other_slots(postgres_queue):
    engine, settings, now = postgres_queue
    entered, release = threading.Event(), threading.Event()
    calls = []
    def sender(uid, listing):
        calls.append((uid, listing.source_id))
        if uid == 111:
            entered.set()
            assert release.wait(5), "test did not release the synthetic slow sender"
        return {"ok": True, "result": {"message_id": uid}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(worker.deliver_one, engine, settings, sender, now)
        try:
            assert entered.wait(2)
            other = pool.submit(worker.deliver_one, engine, settings, sender, now)
            assert other.result(timeout=2) == "sent"
            assert calls == [(111, "car-1"), (222, "car-1")]
            assert not slow.done()
        finally:
            release.set()
        assert slow.result(timeout=2) == "sent"
    with Session(engine) as db:
        sent = list(db.scalars(select(Delivery).where(Delivery.state == "sent")))
        assert len(sent) == 2 and {row.user_id for row in sent} == {111, 222}
