import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import delivery_diagnostic
from backend.models import Delivery, SourceProbe
from backend.worker import TelegramSender, deliver_one, ingest, enqueue
from backend.tests.test_backend import setup, ready, car, TOKEN


def sender(monkeypatch, replies):
    requests = []
    real = httpx.Client
    def handler(request):
        requests.append(request)
        item = replies[len(requests) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr('backend.worker.httpx.Client', lambda **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    return TelegramSender(TOKEN), requests


def accepted():
    return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71}})


@pytest.mark.parametrize('description', ['Bad Request: wrong type of the web page content',
                                       'Bad Request: failed to get HTTP URL content',
                                       'Bad Request: unknown request rejection'])
def test_explicit_photo_rejection_sends_text_with_same_car_and_button(monkeypatch, description):
    send, requests = sender(monkeypatch, [httpx.Response(400,
        json={'ok': False, 'error_code': 400, 'description': description}), accepted()])
    result = send(111, car(photo='https://example.com/photo.jpg'))
    assert result['ok'] is True and len(requests) == 2
    first, second = [json.loads(r.content) for r in requests]
    assert requests[0].url.path.endswith('/sendPhoto')
    assert requests[1].url.path.endswith('/sendMessage')
    assert first['chat_id'] == second['chat_id'] == 111
    assert second['text'] == first['caption']
    assert second['reply_markup'] == first['reply_markup']
    assert 'photo' not in second and 'caption' not in second
    assert second['link_preview_options'] == {'is_disabled': True}
    assert result['_delivery_attempts'][0]['accepted'] is False
    assert result['_delivery_attempts'][1]['accepted'] is True


@pytest.mark.parametrize('response', [accepted(), httpx.Response(500, json={'ok': False, 'error_code': 400}),
    httpx.Response(200, json={'ok': True, 'result': None}), httpx.Response(400, json={'error_code': 400}),
    httpx.Response(429, json={'ok': False, 'error_code': 429}),
    httpx.Response(403, json={'ok': False, 'error_code': 403}),
    httpx.Response(401, json={'ok': False, 'error_code': 401}),
    httpx.Response(502, text='invalid JSON'), httpx.ReadTimeout('secret URL'),
    httpx.Response(200, json=[])])
def test_success_ambiguity_or_non_400_never_trigger_fallback(monkeypatch, response):
    send, requests = sender(monkeypatch, [response])
    send(111, car(photo='https://example.com/photo.jpg'))
    assert len(requests) == 1


@pytest.mark.parametrize('second,state', [
    (httpx.ReadTimeout('private-token'), 'uncertain'),
    (httpx.Response(200, json={'ok': True, 'result': None}), 'uncertain'),
    (httpx.Response(400, json={'ok': False, 'error_code': 400}), 'failed'),
    (httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 30}}), 'pending'),
    (accepted(), 'sent'),
])
def test_fallback_result_uses_normal_durable_delivery_state(setup, monkeypatch, second, state):
    engine, settings, _ = setup
    ready(setup)
    ingest(engine, [car(photo='https://example.com/photo.jpg')])
    enqueue(engine)
    send, requests = sender(monkeypatch, [httpx.Response(400, json={'ok': False,
        'error_code': 400, 'description': 'Bad Request: wrong type of the web page content private-secret'}), second])
    assert deliver_one(engine, settings, send) == state
    assert len(requests) == 2
    assert deliver_one(engine, settings, lambda *a: pytest.fail('duplicate send')) == 'empty'
    with Session(engine) as db:
        delivery = db.scalar(select(Delivery))
        report = delivery_diagnostic.receipt(db, delivery.id)
        assert report['state'] == state
        assert report['attempts'][0]['reason'] == 'photo_content_type'
        assert 'private-secret' not in json.dumps(report)
        assert 'private-token' not in json.dumps(report)
        assert db.scalar(select(SourceProbe).where(SourceProbe.id.like('telegram-delivery-result%'))).requests == 0


def test_no_photo_has_no_fallback(monkeypatch):
    send, requests = sender(monkeypatch, [httpx.Response(400, json={'ok': False, 'error_code': 400})])
    send(111, car())
    assert len(requests) == 1 and requests[0].url.path.endswith('/sendMessage')
