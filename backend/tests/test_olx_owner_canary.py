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


@pytest.fixture
def sample(live, monkeypatch):
    live.settings = replace(live.settings, olx_owner_canary_test_ads_enabled=True)
    canary.initialize(live.engine, live.settings, NOW)
    with Session(live.engine) as db:
        state = dict(db.get(SourceProbe, canary.STATE).result)
        state['test_urls'] = [URL]
    car = canary.parse_detail_snapshot(page(), fetched_at=NOW, truncated=False)['listing']
    return live, state, car


def test_authorized_sample_keeps_observed_currency_and_no_profitability_claim(sample):
    b, state, car = sample
    car['title'] = '<b>Example & car</b>'
    assert canary.send_test_ad(b.engine, b.settings, state, car, b.sender)
    assert not canary.send_test_ad(b.engine, b.settings, state, car, b.sender)
    method, payload = b.sent[0]
    assert method == 'sendMessage' and payload['chat_id'] == 900
    assert '25500 грн' in payload['text'] and 'Тестове оголошення' in payload['text']
    assert 'не підтверджені' in payload['text'] and '&lt;b&gt;' in payload['text']
    assert 'ринкова ціна' not in payload['text'] and 'знижка' not in payload['text']
    assert payload['reply_markup']['inline_keyboard'][0][0]['url'] == URL
    assert canary.send_test_ad(b.engine, replace(b.settings, olx_owner_canary_test_ads_enabled=False), state, car, b.sender) is False
    assert len(b.sent) == 1


@pytest.mark.parametrize('field, value', [('region', 'Полтавська область'), ('brand', 'BMW'),
    ('model', 'X5'), ('year_min', 2000), ('mileage_km_max', 100), ('body', 'sedan'),
    ('fuel', 'diesel'), ('transmission', 'automatic'), ('price_min', 1000)])
def test_test_sample_known_filter_contradictions_block(sample, field, value):
    b, state, car = sample
    car.update(region='Чернівецька область', body='wagon', fuel='petrol', transmission='manual', mileage_km=100000)
    car['price'], car['currency'] = '500', 'USD'
    assert canary.sample_match(car, [{'filters': {field: value, 'currency': 'USD'}}]) is False


def test_test_sample_missing_requested_region_is_held_and_no_fx_fabrication(sample):
    b, state, car = sample
    assert not canary.sample_match(car, [{'filters': {'region': ['Чернівецька'], 'currency': 'USD'}}])
    car['region'] = 'Чернівецька область'
    assert canary.sample_match(car, [{'filters': {'region': ['Чернівецька'], 'price_min': 1000, 'currency': 'USD'}}])
    assert car['usd_price']['status'] != 'ready' and car['currency'] == 'UAH'


@pytest.mark.parametrize('issue', ['uncleared', 'display_conflict', 'stopped', 'unpaid', 'expired', 'paused', 'off'])
def test_sample_rechecks_current_owner_permissions_and_source_evidence(sample, issue):
    b, state, car = sample
    if issue == 'uncleared': car['eligibility_review']['status'] = 'excluded'
    elif issue == 'display_conflict': car['observed_asking_display']['status'] = 'needs_review'
    elif issue == 'off': b.settings = replace(b.settings, olx_owner_canary_test_ads_enabled=False)
    else:
        with Session(b.engine) as db:
            if issue == 'stopped': db.get(User, 900).ready = False
            if issue == 'unpaid': db.get(PaymentRequest, 'test-900').state = 'review'
            if issue == 'expired': db.get(PaymentRequest, 'test-900').expires_at = NOW
            if issue == 'paused': db.get(SourceProbe, canary.STATE).status = 'paused_by_owner'
            db.commit()
    assert not canary.send_test_ad(b.engine, b.settings, state, car, b.sender)
    assert not b.sent


@pytest.mark.parametrize('response', [{'uncertain': True}, {'ok': True, 'result': True},
    {'ok': True, 'result': {'message_id': -1}}, {'ok': True, 'result': {'message_id': 555, 'chat': {'id': 100}}}])
def test_sample_uncertain_or_wrong_receipt_never_replayed_or_falls_back(sample, response):
    b, state, car = sample
    car['photos'] = ['https://images.olxcdn.com/example.jpg']
    def sender(*args): b.sent.append(args[1]); return response
    assert not canary.send_test_ad(b.engine, b.settings, state, car, sender)
    assert not canary.send_test_ad(b.engine, b.settings, state, car, sender)
    assert b.sent == ['sendPhoto']


def test_sample_batch_cap_two_preserves_customer_filter_and_payment(sample, monkeypatch):
    b, state, car = sample
    with Session(b.engine) as db:
        before = (db.get(User, 900).ready, db.get(PaymentRequest, 'test-900').state)
    for i in range(3):
        car['id'] = str(100+i)
        assert canary.send_test_ad(b.engine, b.settings, state, car, b.sender) == (i < 2)
    assert len(b.sent) == 2
    with Session(b.engine) as db:
        assert before == (db.get(User, 900).ready, db.get(PaymentRequest, 'test-900').state)
        assert db.get(SourceProbe, canary.TEST_BATCH).requests == 2


def test_stop_immediately_after_durable_sample_reservation_blocks_sender(sample, monkeypatch):
    b, state, car = sample
    original = canary.permitted
    calls = []
    def permitted(*args):
        calls.append(1)
        if len(calls) == 2:
            with Session(b.engine) as db:
                db.get(User, 900).ready = False; db.commit()
        return original(*args)
    monkeypatch.setattr(canary, 'permitted', permitted)
    assert not canary.send_test_ad(b.engine, b.settings, state, car, b.sender)
    assert not b.sent


def test_previously_seen_baseline_can_be_explicit_sample_once(sample):
    b, state, car = sample
    with Session(b.engine) as db:
        row = db.get(SourceProbe, canary.STATE)
        row.result = {**row.result, 'seen': ['123'], 'cycles': 1}
        db.commit()
    result = tick(b)
    assert result['new_ids'] == 0 and result['car_sends'] == 1
    b.clock[0] += 301
    assert tick(b)['car_sends'] == 0


def test_first_detail_timeout_does_not_discard_second_sample(sample, monkeypatch):
    import httpx
    b, state, car = sample
    first = 'https://www.olx.ua/d/uk/obyavlenie/other-example.html'
    two_cards = (f'<html><div data-testid="l-card" id="124"><a data-testid="card-title-link" href="{first}">Other</a></div>').encode() + SEARCH.removeprefix(b'<html>')
    original = b.fetch
    def fetch(url):
        if url == first:
            b.calls.append(url)
            raise httpx.ReadTimeout('temporary upstream timeout')
        if url == canary.URL:
            b.calls.append(url)
            # Put the failed detail first without inventing publication proof.
            return 200, two_cards, False
        return original(url)
    result = canary.tick(b.engine, b.settings, fetch, b.sender)
    assert result['status'] == 'technical_hold' and result['error_type'] == 'ReadTimeout'
    assert result['details'] == result['car_sends'] == 1
    assert b.calls == [canary.URL, first, URL]


def test_grounded_category_choice_uses_current_named_region_only():
    assert canary.test_search_url([{'filters': {'region': ['Чернівецька область']}}]) == canary.TEST_SEARCHES['чернівецька']
    assert canary.test_search_url([{'filters': {'region': ['Хмельницька']}}]) == canary.TEST_SEARCHES['хмельницька']
    assert canary.test_search_url([{'filters': {'region': ['Полтавська']}}]) == canary.URL


def test_only_requested_sample_has_four_bounded_extra_public_reservations(sample, monkeypatch):
    b, state, car = sample
    monkeypatch.setattr(canary, 'MAX_BYTES', canary.MAX_HTML)
    token, _ = canary.claim(b.engine, b.settings, NOW)
    for _ in range(1+canary.TEST_EXTRA_REQUESTS):
        assert canary.reserve(b.engine, token, b.settings, NOW)
    assert not canary.reserve(b.engine, token, b.settings, NOW)


def test_sample_extra_allowance_ends_after_two_attempts(sample, monkeypatch):
    b, state, car = sample
    monkeypatch.setattr(canary, 'MAX_BYTES', canary.MAX_HTML)
    token, _ = canary.claim(b.engine, b.settings, NOW)
    assert canary.reserve(b.engine, token, b.settings, NOW)
    with Session(b.engine) as db:
        db.merge(SourceProbe(id=canary.TEST_BATCH, status='active', checked_at=NOW, requests=2, result={}))
        db.commit()
    assert not canary.reserve(b.engine, token, b.settings, NOW)
