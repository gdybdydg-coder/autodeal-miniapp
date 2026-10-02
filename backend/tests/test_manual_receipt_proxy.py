"""Synthetic receipt bytes and mocked Telegram transport; no external calls."""
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from backend import manual_receipt_proxy as proxy
from backend.manual_payments import ReviewError


TOKEN = "123456:SYNTHETIC_PRIVATE_TOKEN"
SETTINGS = SimpleNamespace(bot_token=TOKEN)
PNG = b"\x89PNG\r\n\x1a\nsynthetic receipt"


def adapters(*, body=PNG, media="image/png", path="photos/file_123.png", size=None,
             status=200, headers=None, chunks=None):
    calls = []

    def request(token, method, payload, *, timeout):
        calls.append(("metadata", token, method, payload, timeout))
        return {"ok": True, "result": {"file_path": path, "file_size": size}}

    @contextmanager
    def fetcher(url, *, timeout, follow_redirects):
        calls.append(("download", url, timeout, follow_redirects))
        def iter_bytes(*, chunk_size):
            assert chunk_size <= 64 * 1024
            yield from chunks if chunks is not None else [body]
        yield SimpleNamespace(status_code=status,
                              headers={"content-type": media, **(headers or {})},
                              iter_bytes=iter_bytes)

    return request, fetcher, calls


@pytest.mark.parametrize("body,media,kind", [
    (PNG, "image/png", "photo"),
    (b"\xff\xd8\xffsynthetic", "image/jpeg", "photo"),
    (b"RIFF\x10\x00\x00\x00WEBPsynthetic", "image/webp", "document"),
    (b"%PDF-1.7\nsynthetic", "application/pdf", "document"),
])
def test_receipt_returns_bytes_and_canonical_media_type(body, media, kind):
    request, fetcher, calls = adapters(body=body, media=media + "; charset=binary")
    assert proxy.fetch_receipt(SETTINGS, "stored_full_size_file", kind,
                               request=request, fetcher=fetcher) == (body, media)
    assert calls[0] == ("metadata", TOKEN, "getFile", {"file_id": "stored_full_size_file"}, 5)
    assert calls[1] == ("download", f"https://api.telegram.org/file/bot{TOKEN}/photos/file_123.png", 10, False)


@pytest.mark.parametrize("body,kind,expected", [
    (PNG, "photo", "image/png"),
    (b"\xff\xd8\xffsynthetic", "document", "image/jpeg"),
    (b"RIFF\x10\x00\x00\x00WEBPsynthetic", "document", "image/webp"),
    (b"%PDF-1.7\nsynthetic", "document", "application/pdf"),
])
def test_generic_binary_mime_uses_only_validated_receipt_magic(body, kind, expected):
    request, fetcher, _ = adapters(body=body, media="application/octet-stream")
    assert proxy.fetch_receipt(SETTINGS, "file", kind, request=request, fetcher=fetcher) == (body, expected)


@pytest.mark.parametrize("path", [None, "", "/photos/image.png", "../image.png",
    "photos/../image.png", "photos/./image.png", "photos//image.png",
    "https://example.org/image.png", "photos\\image.png", "photos/image.png?token=x",
    "photos/image.png#fragment", "photos/%2e%2e/image.png", "photos/image\n.png"])
def test_invalid_download_paths_rejected_before_fetch(path):
    request, fetcher, calls = adapters(path=path)
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, "file", "photo", request=request, fetcher=fetcher)
    assert (error.value.code, error.value.status) == ("receipt_unavailable", 502)
    assert len(calls) == 1


@pytest.mark.parametrize("file_id", [None, "", " ", 123, "x" * 513])
def test_missing_receipt_does_not_call_telegram(file_id):
    request, fetcher, calls = adapters()
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, file_id, "photo", request=request, fetcher=fetcher)
    assert (error.value.code, error.value.status) == ("receipt_missing", 404)
    assert calls == []


@pytest.mark.parametrize("response", [None, [], {}, {"ok": False}, {"ok": True},
    {"ok": True, "result": []}, {"ok": True, "result": {}}])
def test_missing_metadata_is_sanitized(response):
    _, fetcher, calls = adapters()
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, "file", "photo",
                           request=lambda *a, **kw: response, fetcher=fetcher)
    assert str(error.value) == "receipt_unavailable"
    assert calls == []


@pytest.mark.parametrize("kwargs", [
    {"size": proxy.MAX_RECEIPT_BYTES + 1},
    {"headers": {"content-length": str(proxy.MAX_RECEIPT_BYTES + 1)}},
    {"chunks": [PNG, b"x" * proxy.MAX_RECEIPT_BYTES]},
])
def test_body_size_limit_applies_to_metadata_headers_and_stream(kwargs):
    request, fetcher, _ = adapters(**kwargs)
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, "file", "photo", request=request, fetcher=fetcher)
    assert (error.value.code, error.value.status) == ("receipt_too_large", 413)


def test_exact_size_limit_is_allowed_and_chunked():
    body = PNG + b"x" * (proxy.MAX_RECEIPT_BYTES - len(PNG))
    request, fetcher, _ = adapters(chunks=[body[:100], body[100:]], size=len(body))
    actual, media = proxy.fetch_receipt(SETTINGS, "file", "photo", request=request, fetcher=fetcher)
    assert actual == body and media == "image/png"


@pytest.mark.parametrize("body,media,kind", [
    (b"", "image/png", "photo"),
    (b"<html>not an image</html>", "image/png", "photo"),
    (PNG, "text/html", "photo"),
    (PNG, "image/svg+xml", "photo"),
    (PNG, "", "photo"),
    (PNG, "image/jpeg", "photo"),
    (b"%PDF-1.7\nsynthetic", "application/pdf", "photo"),
    (b"%PDF-1.7\nsynthetic", "application/octet-stream", "photo"),
    (b"<html>not an image</html>", "application/octet-stream", "document"),
])
def test_only_matching_image_types_or_legacy_document_pdf_are_accepted(body, media, kind):
    request, fetcher, _ = adapters(body=body, media=media)
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, "file", kind, request=request, fetcher=fetcher)
    assert (error.value.code, error.value.status) == ("receipt_type_invalid", 415)


@pytest.mark.parametrize("status", [301, 302, 307, 404, 500])
def test_redirects_and_failed_downloads_are_rejected(status):
    request, fetcher, _ = adapters(status=status)
    with pytest.raises(ReviewError, match="^receipt_unavailable$"):
        proxy.fetch_receipt(SETTINGS, "file", "photo", request=request, fetcher=fetcher)


@pytest.mark.parametrize("step", ["metadata", "download", "stream"])
def test_transport_errors_cannot_expose_token_or_download_url(step, caplog):
    url = f"https://api.telegram.org/file/bot{TOKEN}/photos/file.png"
    request, fetcher, _ = adapters()
    def fail(*args, **kwargs):
        raise httpx.ConnectError(url)
    if step == "metadata":
        request = fail
    elif step == "download":
        fetcher = fail
    else:
        @contextmanager
        def fetcher(*args, **kwargs):
            yield SimpleNamespace(status_code=200, headers={"content-type": "image/png"}, iter_bytes=fail)
    with pytest.raises(ReviewError) as error:
        proxy.fetch_receipt(SETTINGS, "file", "photo", request=request, fetcher=fetcher)
    assert str(error.value) == "receipt_unavailable"
    assert error.value.__suppress_context__
    assert TOKEN not in caplog.text and url not in caplog.text


def test_default_fetcher_uses_streaming_client_and_suppresses_request_logging(monkeypatch, caplog):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    original_client = httpx.Client
    def client(*, timeout, follow_redirects):
        assert timeout == 10 and follow_redirects is False
        return original_client(timeout=timeout, follow_redirects=follow_redirects,
                               transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy.httpx, "Client", client)
    request, _, _ = adapters()
    with caplog.at_level("DEBUG"):
        assert proxy.fetch_receipt(SETTINGS, "file", "photo", request=request) == (PNG, "image/png")
    assert len(seen) == 1 and seen[0].method == "GET"
    assert TOKEN not in caplog.text and "api.telegram.org/file/" not in caplog.text
