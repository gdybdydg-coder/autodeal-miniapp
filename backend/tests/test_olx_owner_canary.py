"""Live canary boundary with real policy/temp DB, fake OLX and Telegram only."""
from dataclasses import replace
import json

import pytest
from sqlalchemy.orm import Session

from backend import olx_owner_canary as canary
from backend.app import Settings
from backend.models import SourceProbe, User
from backend.manual_payment_models import PaymentRequest
from backend.tests.test_olx_isolated_integration import bench, client
from experiments.olx_offline.test_pipeline import NOW
from experiments.olx_offline.test_observed_price_provenance import page, URL

SEARCH = ('<html><div data-testid="l-card" id="123">'
          '<a data-testid="card-title-link" href="' + URL + '">Mazda</a>'
          '<p data-testid="ad-price">25 500 грн.</p></div></html>').encode()


@pytest.fixture
def live(bench, monkeypatch):
    client(bench, 900)
    settings = Settings(str(bench.engine.url), 'test-token', 'test-secret-only-'*3,
                        delivery_enabled=True, source_ready=True, admin_telegram_id=900,
                        olx_owner_canary_enabled=True, olx_owner_canary_until=NOW+7200)
    calls, sent = [], []
    monkeypatch.setattr(canary.time, 'time', lambda: bench.clock[0])
    def fetch(url):
        calls.append(url)
        return 200, SEARCH if url == canary.URL else page(), False
    def sender(token, method, payload):
        sent.append((method, payload))
        return {'ok': True, 'result': {'message_id': 555}}
    bench.settings, bench.fetch, bench.sender = settings, fetch, sender
    bench.calls, bench.sent = calls, sent
    return bench


def tick(b):
    return canary.tick(b.engine, b.settings, b.fetch, b.sender)


def test_owner_only_actual_policy_source_diagnostic_receipt_and_no_car_deal(live):
    client(live, 100)
    result = tick(live)
    assert result['pages'] == result['details'] == result['eligible'] == 1
    assert result['valuation_unconfirmed'] == 1
    assert result['car_sends'] == 0
    assert len(live.sent) == 1 and live.sent[0][1]['chat_id'] == 900
    with Session(live.engine) as db:
        state = db.get(SourceProbe, canary.STATE)
        assert state.result['search_ids'] == [900]
        assert state.requests == 2
        assert db.get(SourceProbe, canary.NOTICE).status == 'accepted'
        assert 'PRIVATE_' not in json.dumps(state.result)


@pytest.mark.parametrize('reason', ['disabled', 'expired_canary', 'unpaid', 'review', 'expired_purchase', 'stopped'])
def test_no_source_or_send_without_current_owner_search(live, reason):
    if reason == 'disabled': live.settings = replace(live.settings, olx_owner_canary_enabled=False)
    elif reason == 'expired_canary': live.settings = replace(live.settings, olx_owner_canary_until=NOW)
    else:
        with Session(live.engine) as db:
            if reason == 'unpaid': db.delete(db.get(PaymentRequest, 'test-900'))
            if reason == 'review': db.get(PaymentRequest, 'test-900').state = 'review'
            if reason == 'expired_purchase': db.get(PaymentRequest, 'test-900').expires_at = NOW
            if reason == 'stopped': db.get(User, 900).ready = False
            db.commit()
    tick(live)
    assert not live.calls and not live.sent


def test_later_paid_activation_is_not_poisoned_by_initial_unpaid(live):
    with Session(live.engine) as db:
        db.get(PaymentRequest, 'test-900').state = 'review'; db.commit()
    assert tick(live)['status'] == 'waiting_for_paid_ready_search'
    with Session(live.engine) as db:
        assert db.get(SourceProbe, canary.STATE) is None
        db.get(PaymentRequest, 'test-900').state = 'approved'; db.commit()
    assert tick(live)['details'] == 1


def test_stop_between_search_and_detail_blocks_next_request_and_notice(live):
    def fetch(url):
        live.calls.append(url)
        with Session(live.engine) as db:
            db.get(User, 900).ready = False; db.commit()
        return 200, SEARCH, False
    canary.tick(live.engine, live.settings, fetch, live.sender)
    assert live.calls == [canary.URL] and not live.sent


def test_403_stops_without_retry_and_no_claim_of_working_delivery(live):
    def blocked(url):
        live.calls.append(url)
        return 403, b'', False
    result = canary.tick(live.engine, live.settings, blocked, live.sender)
    assert result['status'] == 'source_blocked' and not live.sent
    live.clock[0] += 301
    canary.tick(live.engine, live.settings, blocked, live.sender)
    assert len(live.calls) == 1


def test_restart_baseline_dedup_and_receipt_never_replayed(live):
    tick(live)
    live.clock[0] += 301
    result = tick(live)
    assert result['new_ids'] == result['details'] == 0
    assert len(live.sent) == 1


def test_unknown_telegram_receipt_is_durable_and_not_retried(live):
    def unknown(*args):
        live.sent.append('attempt'); return {'uncertain': True}
    canary.tick(live.engine, live.settings, live.fetch, unknown)
    live.clock[0] += 301
    canary.tick(live.engine, live.settings, live.fetch, unknown)
    assert live.sent == ['attempt']
    with Session(live.engine) as db: assert db.get(SourceProbe, canary.NOTICE).status == 'uncertain'


def event(uid=900, text='/olx_stop', private=True):
    return {'message': {'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private' if private else 'group'}, 'text': text}}


def test_command_owner_authorization_and_olx_stop_preserves_RIA_state(live):
    tick(live)
    for e in (event(100), event(private=False), event(text='/olx_stop@other_bot')):
        assert canary.handle(live.engine, live.settings, e) == {'ok': True}
        with Session(live.engine) as db: assert db.get(SourceProbe, canary.STATE).status == 'active'
    response = canary.handle(live.engine, live.settings, event())
    assert response['chat_id'] == 900
    with Session(live.engine) as db:
        assert db.get(SourceProbe, canary.STATE).status == 'paused_by_owner'
        assert db.get(User, 900).ready is True
        assert db.get(PaymentRequest, 'test-900').state == 'approved'
    live.clock[0] += 301
    assert tick(live)['status'] == 'paused_or_busy'


def test_budget_reserved_before_request_and_exhaustion_stops(live, monkeypatch):
    monkeypatch.setattr(canary, 'MAX_BYTES', canary.MAX_HTML)
    result = tick(live)
    assert len(live.calls) == 1 and not live.sent
    with Session(live.engine) as db:
        state = db.get(SourceProbe, canary.STATE)
        assert state.status == 'finished' and state.result['budget_bytes'] == canary.MAX_HTML
    live.clock[0] += 301
    assert tick(live)['status'] == 'paused_or_busy'


def test_duplicate_workers_cannot_share_lease(live):
    canary.initialize(live.engine, live.settings, NOW)
    assert canary.claim(live.engine, live.settings, NOW) is not None
    assert canary.claim(live.engine, live.settings, NOW) is None


def test_malformed_telegram_receipt_not_accepted_or_retried(live):
    def malformed(*args):
        live.sent.append('attempt'); return {'ok': True, 'result': True}
    canary.tick(live.engine, live.settings, live.fetch, malformed)
    live.clock[0] += 301
    canary.tick(live.engine, live.settings, live.fetch, malformed)
    assert live.sent == ['attempt']
    with Session(live.engine) as db: assert db.get(SourceProbe, canary.NOTICE).status != 'accepted'


@pytest.mark.parametrize('url', ['https://www.olx.ua:1234/d/uk/obyavlenie/example.html',
                               'https://user@www.olx.ua/d/uk/obyavlenie/example.html',
                               'https://www.olx.ua/api/v1/offers',
                               'https://example.invalid/d/uk/obyavlenie/example.html'])
def test_public_source_rejects_unapproved_transport_paths(url):
    with pytest.raises(ValueError): canary.public_fetch(url)


def test_stop_before_initialization_prevents_future_auto_launch(live):
    canary.handle(live.engine, live.settings, event())
    assert tick(live)['status'] == 'paused_or_busy'
    assert not live.calls and not live.sent
