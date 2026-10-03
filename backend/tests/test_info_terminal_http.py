"""Fixture HTTP outcomes; never invoke a server, provider or production settings."""
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from backend import auto_ria


class Response:
    status = 200
    headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"}

    def __init__(self, body=b'{"fixture": true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, cap):
        return self.body[:cap]


def fail_http(monkeypatch, status, ordered):
    body = BytesIO(b"private response must never enter the audit")

    def open_fixture(request, *, timeout):
        ordered.append("open")
        assert timeout == 8
        raise HTTPError(request.full_url, status, "private URL and message", {}, body)

    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: SimpleNamespace(open=open_fixture))
    return body


@pytest.mark.parametrize("status", [404, 410])
def test_info_not_found_is_operator_review_not_proof_of_deleted_car(monkeypatch, status):
    ordered, audit = [], []
    body = fail_http(monkeypatch, status, ordered)

    def observe(event):
        ordered.append(event["event"])
        audit.append(event)

    with pytest.raises(auto_ria.RiaError) as caught:
        auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"}, attempt_telemetry=observe)
    assert str(caught.value) == "info_endpoint_unavailable"
    assert caught.value.http_status == status
    assert caught.value.request_attempted is True
    assert ordered == ["transport_started", "open", "http_response"]
    assert audit == [{"event": "transport_started"}, {"event": "http_response", "http_status": status}]
    assert body.closed


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("method,params", [
    ("search", {}), ("states", {}), ("categories/1/marks", {}),
    ("categories/1/marks/84/models", {}),
    ("modifications/by/generation/1/body/2/modifications", {}),
])
def test_info_review_does_not_reclassify_other_api_routes(monkeypatch, status, method, params):
    ordered, audit = [], []
    fail_http(monkeypatch, status, ordered)
    with pytest.raises(auto_ria.RiaError) as caught:
        auto_ria.fetch_json("fixture-key", method, params, attempt_telemetry=audit.append)
    assert str(caught.value) == "upstream_error"
    assert caught.value.http_status == status and caught.value.request_attempted is True
    assert ordered == ["open"]
    assert audit == [{"event": "transport_started"}, {"event": "http_response", "http_status": status}]


@pytest.mark.parametrize("status,reason", [(401, "key_rejected"), (403, "access_denied"),
                                        (429, "quota_exceeded"), (500, "upstream_error")])
def test_info_auth_rate_limit_and_server_errors_preserve_existing_codes(monkeypatch, status, reason):
    fail_http(monkeypatch, status, [])
    with pytest.raises(auto_ria.RiaError, match="^" + reason + "$") as caught:
        auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"})
    assert caught.value.http_status == status and caught.value.request_attempted is True


def test_successful_response_observations_keep_original_quota_callback(monkeypatch):
    ordered, audit, quota = [], [], []

    def open_fixture(request, *, timeout):
        ordered.append("open")
        return Response()

    def observe(event):
        ordered.append(event["event"])
        audit.append(event)

    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: SimpleNamespace(open=open_fixture))
    result = auto_ria.fetch_json("fixture-key", "search", {}, quota.append, attempt_telemetry=observe)
    assert result == {"fixture": True}
    assert ordered == ["transport_started", "open", "http_response"]
    assert audit == [{"event": "transport_started"}, {"event": "http_response", "http_status": 200}]
    assert quota == [{"hourly_limit": 5000, "hourly_remaining": 4999}]


@pytest.mark.parametrize("response", [Response(), Response(b"invalid-json"), 404])
def test_audit_callback_failure_does_not_retry_or_change_source_outcome(monkeypatch, response):
    calls, audit_attempts = [], []

    def open_fixture(request, *, timeout):
        calls.append("open")
        if isinstance(response, int):
            raise HTTPError(request.full_url, response, "private-message", {}, BytesIO())
        return response

    def failing_audit(event):
        audit_attempts.append(event)
        raise RuntimeError("private audit failure")

    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: SimpleNamespace(open=open_fixture))
    if response == 404:
        with pytest.raises(auto_ria.RiaError, match="^info_endpoint_unavailable$"):
            auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"}, attempt_telemetry=failing_audit)
    elif response.body == b"invalid-json":
        with pytest.raises(auto_ria.RiaError, match="^invalid_response$") as caught:
            auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"}, attempt_telemetry=failing_audit)
        assert caught.value.http_status == 200
    else:
        assert auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"},
                                  attempt_telemetry=failing_audit) == {"fixture": True}
    assert calls == ["open"] and len(audit_attempts) == 2


def test_transport_failure_is_distinct_from_an_http_response(monkeypatch):
    audit, calls = [], []

    def open_fixture(request, *, timeout):
        calls.append("open")
        raise URLError("private network exception")

    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: SimpleNamespace(open=open_fixture))
    with pytest.raises(auto_ria.RiaError, match="^connection_error$") as caught:
        auto_ria.fetch_json("fixture-key", "info", {"auto_id": "123"}, attempt_telemetry=audit.append)
    assert caught.value.request_attempted is True and caught.value.http_status is None
    assert audit == [{"event": "transport_started"}, {"event": "transport_failed"}]
    assert calls == ["open"]


def test_rejected_method_has_no_transport_attempt_or_audit(monkeypatch):
    audit = []
    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: pytest.fail("Invalid route reached transport"))
    with pytest.raises(auto_ria.RiaError, match="^invalid_method$") as caught:
        auto_ria.fetch_json("fixture-key", "unverified/route", {}, attempt_telemetry=audit.append)
    assert caught.value.request_attempted is False and caught.value.http_status is None
    assert audit == []
