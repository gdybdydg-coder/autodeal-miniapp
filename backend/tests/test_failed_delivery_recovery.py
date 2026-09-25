from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import failed_delivery_recovery as recovery
from backend.models import Delivery, DeliveryTiming, MonitorWatch, Search, SourceProbe, User
from backend.tests.test_monitor import p, drain, wake, add_search, details
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_parallel_monitor import dispatch


def prepare(p, monkeypatch, state='failed'):
    quotes = enable(p, monkeypatch)
    add_search(p)
    p.runner.sender = lambda *_: {'ok': False, 'error_code': 400}
    drain(p)
    p.ads['124'] = p.clock[0] + 1
    wake(p)
    drain(p)
    dispatch(p)
    with Session(p.engine) as db:
        own = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        own.state = state
        if state == 'sent':
            own.message_id = 71
            db.get(DeliveryTiming, own.id).accepted_at = p.clock[0]
        db.commit()
    p.runner.settings = replace(p.runner.settings, admin_telegram_id=111,
                                ria_failed_delivery_recovery_id='124')
    return quotes


def recover(p):
    assert p.runner.claim()
    try:
        recovery.recover_once(p.runner)
    finally:
        p.runner.release('idle')


def test_retry_only_failed_owner_delivery_once_with_fresh_price_and_same_epoch(p, monkeypatch):
    quotes = prepare(p, monkeypatch)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
    p.clock[0] += 301
    p.prices['124'] = 9000
    sent = []
    p.runner.sender = lambda uid, car: sent.append((uid, car.price)) or {
        'ok': True, 'result': {'message_id': 72, 'date': int(p.clock[0])}}
    recover(p)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == 'pending'
        assert db.scalar(select(Delivery).where(Delivery.user_id == 222)).state == 'failed'
    drain(p)
    p.clock[0] += 6  # The normal stale-evidence worker retry is five seconds.
    dispatch(p)
    assert sent == [(111, 9000)] and len(details(p, '124')) == 2
    recover(p)
    dispatch(p)
    assert sent == [(111, 9000)]
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(SourceProbe, recovery.key('124', 111)).status == 'queued'


@pytest.mark.parametrize('state', ['sent', 'uncertain', 'sending', 'pending', 'cancelled'])
def test_recovery_never_changes_non_failed_claims(p, monkeypatch, state):
    prepare(p, monkeypatch, state)
    recover(p)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == state
        assert db.get(SourceProbe, recovery.key('124', 111)).status == 'not_eligible'


@pytest.mark.parametrize('blocker', ['stopped', 'not_ready', 'changed_filter', 'message_id', 'accepted_at', 'no_admin'])
def test_recovery_cannot_bypass_consent_filters_or_receipts(p, monkeypatch, blocker):
    prepare(p, monkeypatch)
    with Session(p.engine) as db:
        own = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        if blocker == 'stopped': db.get(Search, 1).enabled = False
        if blocker == 'not_ready': db.get(User, 111).ready = False
        if blocker == 'changed_filter': db.get(Search, 1).fingerprint = 'changed'
        if blocker == 'message_id': own.message_id = 1
        if blocker == 'accepted_at': db.get(DeliveryTiming, own.id).accepted_at = p.clock[0]
        db.commit()
    if blocker == 'no_admin': p.runner.settings = replace(p.runner.settings, admin_telegram_id=0)
    recover(p)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == 'failed'


def test_price_above_discount_threshold_cancels_recovery(p, monkeypatch):
    prepare(p, monkeypatch)
    p.clock[0] += 301
    p.prices['124'] = 16000
    p.runner.sender = lambda *_: pytest.fail('recovered car no longer meets discount')
    recover(p)
    drain(p)
    p.clock[0] += 6
    dispatch(p)
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == 'cancelled'
