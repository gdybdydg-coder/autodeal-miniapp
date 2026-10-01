"""A transient provider outage must not permanently consume a fresh listing."""
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from backend.auto_ria import RiaError
from backend.models import MonitorJob, MonitorSeen, Search, User
from backend.monitor import Monitor
from backend.tests.test_monitor import p, drain, wake, add_search
from backend.tests.test_ria_ai_price import enable, discover_new, wire
from backend import ria_ai_price as ai


def configured(p, monkeypatch, failure):
    calls = enable(p, monkeypatch, error=failure)
    p.runner.settings = replace(p.runner.settings, ria_confirmed_deals_only=True)
    drain(p)
    discover_new(p)
    drain(p)
    return calls


@pytest.mark.parametrize('failure', ['ai_connection_error', 'ai_upstream_error', 'ai_invalid_response'])
def test_transient_quote_recovers_after_restart_without_finalizing_seen(p, monkeypatch, failure):
    calls = configured(p, monkeypatch, failure)
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, '124').state == 'pending'
        assert db.get(MonitorSeen, (1, '124')).state == 'pending'
    settings, factory = p.runner.settings, p.runner.search_factory
    p.runner = Monitor(p.engine, settings, factory, p.runner.sender)
    monkeypatch.setattr(ai, 'fetch_quote', lambda key, user, sid: ai.parse_quote(wire(15000), sid))
    wake(p, 61)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, '124')]
    wake(p, 61)
    drain(p)
    assert len(p.sent) == 1 and calls == ['124']


def test_repeated_transient_failures_are_bounded_and_retained(p, monkeypatch):
    calls = configured(p, monkeypatch, 'ai_connection_error')
    for _ in range(5):
        wake(p, 121)
        drain(p)
    assert calls == ['124'] * 3
    assert not p.sent
    with Session(p.engine) as db:
        job = db.get(MonitorJob, '124')
        assert job.state == 'unvalued'
        assert job.result['valuation_failure'] == 'ai_connection_error'
        assert job.result['valuation_failures'] == 3


def test_stop_while_waiting_prevents_quote_retry_and_delivery(p, monkeypatch):
    calls = configured(p, monkeypatch, 'ai_connection_error')
    with Session(p.engine) as db:
        db.get(User, 111).ready = False
        db.get(Search, 1).enabled = False
        db.commit()
    wake(p, 121)
    drain(p)
    assert calls == ['124'] and not p.sent


def test_access_denied_is_not_retried(p, monkeypatch):
    calls = configured(p, monkeypatch, 'ai_access_denied')
    wake(p, 121)
    drain(p)
    assert calls == ['124'] and not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, '124').state == 'unvalued'


def test_one_shared_retry_serves_two_users(p, monkeypatch):
    add_search(p)
    calls = configured(p, monkeypatch, 'ai_upstream_error')
    monkeypatch.setattr(ai, 'fetch_quote', lambda key, user, sid: ai.parse_quote(wire(15000), sid))
    wake(p, 61)
    drain(p)
    assert sorted(uid for uid, car in p.sent) == [111, 222]
    assert calls == ['124']


def test_quota_pause_keeps_fresh_job_pending_without_immediate_requests(p, monkeypatch):
    calls = configured(p, monkeypatch, 'quota_exceeded')
    for _ in range(3):
        wake(p, 61)
        drain(p)
    assert calls == ['124'] and not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, '124').state == 'pending'
        assert db.get(MonitorSeen, (1, '124')).state == 'pending'
