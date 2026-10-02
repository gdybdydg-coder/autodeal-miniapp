"""Bounded receipt downloads for the authenticated owner review route.

The caller must authorize the owner and resolve the payment request before
calling this module. Telegram download URLs and tokens never leave this helper.
"""
from contextlib import contextmanager
import logging
import re

import httpx

from . import telegram_setup
from .manual_payments import ReviewError


MAX_RECEIPT_BYTES = 10 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


def _file_path(value):
    # A strict ASCII allowlist also rejects percent-encoded traversal, schemes,
    # fragments, query strings, controls and backslashes before URL creation.
    if (not isinstance(value, str) or not value or len(value) > 1024
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ReviewError("receipt_unavailable", 502)
    return value


def _check_size(value):
    if value is None:
        return
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit() or len(value) > 20:
            raise ReviewError("receipt_unavailable", 502)
        value = int(value)
    if type(value) is not int or value < 0:
        raise ReviewError("receipt_unavailable", 502)
    if value > MAX_RECEIPT_BYTES:
        raise ReviewError("receipt_too_large", 413)


def _media_type(body):
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    return None


@contextmanager
def _stream(url, *, timeout, follow_redirects):
    # HTTPX's informational request logging contains the token-bearing URL.
    # Match the Telegram adapter's logging policy before making the request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client:
        with client.stream("GET", url) as response:
            yield response


def fetch_receipt(settings, file_id, kind, *, request=None, fetcher=None):
    """Return validated receipt bytes and MIME type, or a sanitized ReviewError.

    ``request`` follows ``telegram_setup.call``. ``fetcher`` is an optional
    context-manager factory accepting a URL, timeout and follow_redirects and
    yielding a response with status_code, headers and iter_bytes(chunk_size=).
    Neither dependency is invoked until the stored receipt reference validates.
    """
    if not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 512:
        raise ReviewError("receipt_missing", 404)
    if kind not in ("photo", "document"):
        raise ReviewError("receipt_type_invalid", 415)
    token = getattr(settings, "bot_token", None)
    if not isinstance(token, str) or not token:
        raise ReviewError("receipt_unavailable", 503)

    try:
        metadata = (request or telegram_setup.call)(
            token, "getFile", {"file_id": file_id}, timeout=5)
        if not isinstance(metadata, dict) or metadata.get("ok") is not True:
            raise ReviewError("receipt_unavailable", 502)
        details = metadata.get("result")
        if not isinstance(details, dict):
            raise ReviewError("receipt_unavailable", 502)
        path = _file_path(details.get("file_path"))
        _check_size(details.get("file_size"))

        with (fetcher or _stream)(f"https://api.telegram.org/file/bot{token}/{path}",
                                  timeout=10, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ReviewError("receipt_unavailable", 502)
            _check_size(response.headers.get("content-length"))
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in _MEDIA_TYPES and content_type != "application/octet-stream":
                raise ReviewError("receipt_type_invalid", 415)
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=_CHUNK_BYTES):
                if len(body) + len(chunk) > MAX_RECEIPT_BYTES:
                    raise ReviewError("receipt_too_large", 413)
                body.extend(chunk)
            detected = _media_type(body)
            if (detected not in _MEDIA_TYPES
                    or (content_type != "application/octet-stream" and detected != content_type)
                    or (detected == "application/pdf" and kind != "document")):
                raise ReviewError("receipt_type_invalid", 415)
            return bytes(body), detected
    except ReviewError:
        raise
    except Exception:
        # Transport and parser exceptions can contain a token-bearing URL.
        raise ReviewError("receipt_unavailable", 502) from None
