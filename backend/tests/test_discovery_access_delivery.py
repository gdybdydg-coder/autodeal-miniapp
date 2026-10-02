"""Current manual entitlement state gates car delivery, independently of /stop."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import billing
from backend.billing_models import BillingControl, Entitlement
from backend.models import Delivery, Listing, Search, User
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_ria_ai_price import enable
from backend.worker import enqueue, deliver_one


def access(p, uid, until):
    with Session(p.engine) as db, db.begin():
        if db.get(BillingControl, billing.CONTROL) is None:
            db.add(BillingControl(id=billing.CONTROL, enforce=True))
        row = db.get(Entitlement, uid)
        if row is None:
            db.add(Entitlement(user_id=uid, expires_at=until, updated_at=p.clock[0]))
        else:
            row.expires_at, row.updated_at = until, p.clock[0]


def prepare(p, monkeypatch):
    enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    for _ in range(10):  # Multiple activation boundaries may require extra slices.
        assert p.runner.tick()
        with Session(p.engine) as db:
            if db.scalar(select(Listing)) is not None:
                return
    raise AssertionError("Synthetic listing did not finish valuation")


def test_active_manual_access_is_honoured_at_queue_and_delivery(p, monkeypatch):
    access(p, 111, p.clock[0] + 86400)
    prepare(p, monkeypatch)
    enqueue(p.engine, require_provider_range=True)
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "sent"
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]


def test_expiry_at_send_boundary_cancels_without_impairing_other_paid_user(p, monkeypatch):
    add_search(p)
    access(p, 111, p.clock[0] + 86400)
    access(p, 222, p.clock[0] + 86400)
    prepare(p, monkeypatch)
    enqueue(p.engine, require_provider_range=True)
    access(p, 111, p.clock[0])  # Latest committed state, exact expiry boundary.
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "cancelled"
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "sent"
    assert [(uid, car.source_id) for uid, car in p.sent] == [(222, "124")]


def test_access_change_is_visible_to_next_queue_check_without_process_restart(p, monkeypatch):
    access(p, 111, p.clock[0] - 1)
    prepare(p, monkeypatch)
    enqueue(p.engine, require_provider_range=True)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)) is None
    access(p, 111, p.clock[0] + 86400)
    enqueue(p.engine, require_provider_range=True)
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "sent"
    assert len(p.sent) == 1


def test_paid_access_never_overrides_stop_between_queue_and_send(p, monkeypatch):
    access(p, 111, p.clock[0] + 86400)
    prepare(p, monkeypatch)
    enqueue(p.engine, require_provider_range=True)
    with Session(p.engine) as db, db.begin():
        db.get(User, 111).ready = False
        db.get(Search, 1).enabled = False
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "cancelled"
    access(p, 111, p.clock[0] + 30 * 86400)
    enqueue(p.engine, require_provider_range=True)
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "empty"
    with Session(p.engine) as db:
        assert db.get(User, 111).ready is False
        assert db.get(Search, 1).enabled is False
    assert not p.sent
