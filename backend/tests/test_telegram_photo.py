from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from backend.telegram_photo import PhotoLoader, safe_url, from_details, MAX_INPUT
from backend.worker import TelegramSender
from backend.tests.test_backend import car

URL = 'https://cdn0.riastatic.com/photosnew/auto/photo/skoda_fabia__659038815m.jpg'


def picture(fmt='JPEG', size=(800, 600)):
    out = BytesIO()
    Image.new('RGB', size, 'blue').save(out, format=fmt)
    return out.getvalue()


def parts(request):
    msg = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode() + request.content)
    return {p.get_param('name', header='content-disposition'): p.get_payload(decode=True)
            for p in msg.iter_parts()}


def mock_client(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr('httpx.Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def accepted():
    return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71, 'photo': [{'file_id': 'test'}]}})


def test_source_image_is_uploaded_as_real_jpeg_and_shared_between_subscribers(monkeypatch):
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == 'GET': return httpx.Response(200, content=picture('WEBP'))
        fields = parts(req)
        assert fields['photo'].startswith(b'\xff\xd8')
        assert Image.open(BytesIO(fields['photo'])).format == 'JPEG'
        assert 'Відкрити оголошення' in fields['reply_markup'].decode()
        assert req.url.path.endswith('/sendPhoto')
        return accepted()
    mock_client(monkeypatch, handler)
    sender = TelegramSender('test')
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda uid: sender(uid, car(photo=URL)), [111, 222, 333, 444]))
    assert all(r['ok'] for r in result)
    assert sum(r.method == 'GET' for r in requests) == 1
    assert sum(r.method == 'POST' for r in requests) == 4
    assert {parts(r)['chat_id'] for r in requests if r.method == 'POST'} == {b'111', b'222', b'333', b'444'}


@pytest.mark.parametrize('url', ['http://localhost/photo.jpg', 'https://127.0.0.1/a.jpg',
    'https://cdn0.riastatic.com.evil.com/photosnew/auto/photo/x.jpg',
    'https://cdn0.riastatic.com@evil.com/photosnew/auto/photo/x.jpg',
    URL + '?token=secret', URL + '#fragment', URL.replace('https:', 'file:'),
    URL.replace('com/', 'com:123/'), 'https://cdn0.riastatic.com/private/secret.jpg'])
def test_untrusted_photo_never_downloaded(monkeypatch, url):
    mock_client(monkeypatch, lambda _: pytest.fail('untrusted network'))
    assert PhotoLoader().load(url) is None


def test_legacy_http_and_alternative_provider_photo_fields():
    assert safe_url(URL.replace('https:', 'http:')) == URL
    assert from_details({'photoData': {'seoLinkM': None, 'seoLinkF': URL}}) == URL
    assert from_details({'photoData': {'seoLinkM': URL.removeprefix('https:')}}) == URL
    assert from_details({'photoData': []}) is None


@pytest.mark.parametrize('response', [httpx.Response(200, text='not an image'),
    httpx.Response(200, content=b'x'*(MAX_INPUT + 1)),
    httpx.Response(200, headers={'content-length': str(MAX_INPUT + 1)}, content=b'x'),
    httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'}),
    httpx.Response(200, content=picture(size=(2000, 10))), httpx.Response(404)])
def test_bad_source_photo_is_bounded_and_does_not_crash(monkeypatch, response):
    requests = []
    mock_client(monkeypatch, lambda req: requests.append(req) or response)
    assert PhotoLoader().load(URL) is None
    assert len(requests) <= 2
    assert all(r.url.host in {"cdn0.riastatic.com", "cdn.riastatic.com"} for r in requests)


@pytest.mark.parametrize('reply', [httpx.ReadTimeout('secret'), httpx.Response(502),
    httpx.Response(429, json={'ok': False, 'error_code': 429}),
    httpx.Response(403, json={'ok': False, 'error_code': 403}),
    httpx.Response(200, json={'ok': True, 'result': None})])
def test_upload_ambiguity_and_non_400_never_send_text_or_retry(monkeypatch, reply):
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == 'GET': return httpx.Response(200, content=picture())
        if isinstance(reply, Exception): raise reply
        return reply
    mock_client(monkeypatch, handler)
    TelegramSender('test')(111, car(photo=URL))
    assert len(requests) == 2


def test_explicit_upload_rejection_still_delivers_text(monkeypatch):
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == 'GET': return httpx.Response(200, content=picture())
        if req.url.path.endswith('/sendPhoto'):
            return httpx.Response(400, json={'ok': False, 'error_code': 400})
        assert req.url.path.endswith('/sendMessage')
        return accepted()
    mock_client(monkeypatch, handler)
    assert TelegramSender('test')(111, car(photo=URL))['ok'] is True
    assert len(requests) == 3


def test_edit_adds_photo_to_exact_existing_message_without_sending(monkeypatch):
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == 'GET': return httpx.Response(200, content=picture())
        assert req.url.path.endswith('/editMessageMedia')
        fields = parts(req)
        assert fields['message_id'] == b'71' and fields['chat_id'] == b'111'
        assert json.loads(fields['media'])['media'] == 'attach://photo'
        return accepted()
    mock_client(monkeypatch, handler)
    assert TelegramSender('test').add_photo(111, 71, car(photo=URL))['result']['photo']
    assert len(requests) == 2


def test_photo_node_failure_uses_same_path_on_shared_official_cdn(monkeypatch):
    requests = []
    def handler(req):
        requests.append(req)
        assert req.url.path == '/photosnew/auto/photo/skoda_fabia__659038815m.jpg'
        if req.url.host == 'cdn0.riastatic.com': return httpx.Response(503)
        assert req.url.host == 'cdn.riastatic.com'
        return httpx.Response(200, content=picture())
    mock_client(monkeypatch, handler)
    assert PhotoLoader().load(URL).startswith(b'\xff\xd8')
    assert len(requests) == 2


def test_historical_photo_edit_preserves_original_caption_but_new_alert_requires_fresh_quote(monkeypatch):
    from backend.tests.test_ria_market_range import fixture, priced_car
    import time
    candidate, quote = fixture()
    original = priced_car(candidate, quote).model_copy(update={'photo': URL})
    accepted_at = time.time()
    caption, markup = TelegramSender.card(original)
    monkeypatch.setattr(time, 'time', lambda: accepted_at + 1200)
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == 'GET': return httpx.Response(200, content=picture())
        assert req.url.path.endswith('/editMessageMedia')
        assert json.loads(parts(req)['media'])['caption'] == caption
        return accepted()
    mock_client(monkeypatch, handler)
    sender = TelegramSender('test')
    assert sender.add_photo(111, 71, original, historical_at=accepted_at)['ok']
    count = len(requests)
    with pytest.raises(ValueError, match='invalid_provider_range_evidence'):
        sender(111, original)
    assert len(requests) == count
