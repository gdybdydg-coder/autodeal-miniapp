"""Opted-in initial active cars use the existing paid pipeline and protections."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import Settings
from backend import ria_ai_price as ai
from backend.models import Delivery, Listing, MonitorJob, MonitorSeen, Search, Range
from backend.monitor import Monitor, poll_interval, reset_watch, runtime_status
from backend.ria_budget import BudgetLimits
from backend.tests.test_monitor import p, add_search, drain, wake, details
from backend.tests.test_active_window import enable_window
from backend.tests.test_ria_ai_price import enable, wire
from backend.tests.test_parallel_monitor import dispatch


def configured(p, monkeypatch, **kwargs):
    enable_window(p, monkeypatch)
    p.settings = replace(p.settings, ria_active_window_include_initial=True)
    return enable(p, monkeypatch, **kwargs)


def test_initial_active_page_is_bounded_and_restart_cannot_replay(p, monkeypatch):
    quotes = configured(p, monkeypatch)
    p.stock = [str(n) for n in range(1000, 1100)]
    drain(p)
    dispatch(p, 60)
    assert len(p.sent) == len(quotes) == 50
    assert {car.source_id for _, car in p.sent} == set(p.stock[:50])
    assert not details(p, '1050')
    with Session(p.engine) as db:
        before = [(x.source_id, x.state, x.epoch) for x in db.scalars(select(MonitorSeen))]
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    wake(p, 301)
    drain(p)
    dispatch(p, 60)
    assert len(p.sent) == len(quotes) == 50
    with Session(p.engine) as db:
        assert [(x.source_id, x.state, x.epoch) for x in db.scalars(select(MonitorSeen))] == before


def test_different_groups_reuse_fresh_initial_active_quote(p, monkeypatch):
    add_search(p, price=Range.model_validate({'to': 20000}))
    quotes = configured(p, monkeypatch)
    drain(p)
    dispatch(p)
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, '123'), (222, '123')]
    assert quotes == ['123'] and len(details(p, '123')) == 1


@pytest.mark.parametrize('state', ['sent', 'uncertain'])
def test_initial_active_page_preserves_existing_delivery_claims(p, monkeypatch, state):
    quotes = configured(p, monkeypatch)
    with Session(p.engine) as db:
        listing = Listing(source='auto_ria', source_id='123', car={})
        db.add(listing)
        db.flush()
        db.add(Delivery(user_id=111, listing_id=listing.id, state=state, message_id=71))
        db.commit()
    drain(p)
    assert not p.sent and not quotes and not details(p, '123')
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        assert (delivery.state, delivery.message_id) == (state, 71)
        assert db.get(MonitorJob, '123') is None


def test_stop_during_initial_active_quote_prevents_send(p, monkeypatch):
    def stop():
        with Session(p.engine) as db:
            db.get(Search, 1).enabled = False
            reset_watch(db, 1, False)
            db.commit()
    quotes = configured(p, monkeypatch, action=stop)
    drain(p)
    dispatch(p)
    assert quotes == ['123'] and not p.sent
    with Session(p.engine) as db:
        assert not db.get(Search, 1).enabled
        assert db.scalar(select(Delivery)) is None


@pytest.mark.parametrize('mode', ['discount', 'price', 'missing_quote'])
def test_initial_active_cards_respect_price_discount_and_unknown_market(p, monkeypatch, mode):
    quotes = configured(p, monkeypatch, **({'response': {}} if mode == 'missing_quote' else {}))
    with Session(p.engine) as db:
        search = db.get(Search, 1)
        filters = p.filters.model_copy(update={'minDiscount': 99} if mode == 'discount' else
            {'price': Range.model_validate({'to': 5000})} if mode == 'price' else {})
        search.filters, search.fingerprint = filters.canonical(), filters.fingerprint()
        db.commit()
    drain(p)
    dispatch(p)
    if mode == 'missing_quote':
        assert len(p.sent) == 1 and p.sent[0][1].market is None
    else:
        assert not p.sent
    assert quotes == ([] if mode == 'price' else ['123'])


@pytest.mark.parametrize('instant,expected', [(1790326800, 110), (1790348400, 60), (1790366400, 140)])
def test_paid_active_mode_preserves_kyiv_targets(p, monkeypatch, instant, expected):
    configured(p, monkeypatch)
    caps = BudgetLimits(4500, 90000, 90000)
    assert poll_interval(16, caps, provider_pricing_enabled=True, active_window_enabled=True,
                         schedule_enabled=True, now=instant) == expected
    drain(p)
    report = runtime_status(p.engine, True, active_window_enabled=True, provider_pricing_enabled=True,
                            schedule_enabled=True, active_window_include_initial=True)
    assert report['schedule']['enabled'] and report['source_parallelism'] == 4
    assert report['active_window']['include_initial']


def test_initial_active_option_is_explicit(monkeypatch):
    for key in ('DATABASE_URL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_WEBHOOK_SECRET'):
        monkeypatch.setenv(key, 'test')
    monkeypatch.delenv('RIA_ACTIVE_WINDOW_INCLUDE_INITIAL', raising=False)
    assert not Settings.env().ria_active_window_include_initial
    monkeypatch.setenv('RIA_ACTIVE_WINDOW_INCLUDE_INITIAL', 'true')
    assert Settings.env().ria_active_window_include_initial


def test_initial_active_cars_evaluate_four_at_once(p, monkeypatch):
    configured(p, monkeypatch)
    p.stock = ['200', '201', '202', '203']
    for _ in range(4):
        assert p.runner.tick()
        with Session(p.engine) as db:
            if len(list(db.scalars(select(MonitorJob)))) == 4:
                break
    entered, four, release, lock = [], threading.Event(), threading.Event(), threading.Lock()
    def quote(key, uid, sid):
        with lock:
            entered.append(sid)
            if len(entered) == 4:
                four.set()
        assert release.wait(5)
        return ai.parse_quote(wire(15000), sid)
    monkeypatch.setattr(ai, 'fetch_quote', quote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(p.runner.tick)
        try:
            assert four.wait(2), 'initial active valuations lost parallelism'
        finally:
            release.set()
            assert work.result(timeout=5)
    dispatch(p)
    assert sorted(entered) == p.stock
    assert len(p.sent) == 4
