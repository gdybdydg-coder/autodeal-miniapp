import json
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import create_engine

from backend import auto_ria
from backend.models import Base


@pytest.fixture
def engine(tmp_path):
    value = create_engine("sqlite:///" + str(tmp_path / "source.db"))
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def info():
    return {"title": "Volkswagen Golf", "USD": 12500,
            "linkToView": "/auto_volkswagen_golf_123.html",
            "VIN": "not-for-storage", "userId": 999,
            "autoData": {"autoId": 123, "year": 2017, "isSold": False,
                         "active": True, "statusId": 0}}


def test_probe_budget_persists_and_preview_is_allowlisted(engine):
    calls = []
    def fetch(key, method, params):
        calls.append((method, params))
        assert key == "test-only-secret"
        return {"result": {"search_result": {"ids": ["123"]}}} if method == "search" else info()
    auto_ria.probe_once(engine, "test-only-secret", fetch)
    auto_ria.probe_once(engine, "test-only-secret", fetch)
    assert len(calls) == 2
    assert calls[0][1]["status_id"] == 0
    assert calls[0][1]["searchType"] == 4
    result = auto_ria.probe_status(engine, True)
    assert result["status"] == "connected"
    assert result["requests_used"] == 2
    assert result["sample"]["price_usd"] == 12500
    assert result["market_valuation_ready"] is False
    assert "VIN" not in json.dumps(result)
    assert "test-only-secret" not in json.dumps(result)


@pytest.mark.parametrize("response,status", [
    ({"result": {"search_result": {"ids": []}}}, "connected_empty"),
    ({"result": {"search_result": {"ids": ["//evil.example"]}}}, "invalid_response"),
    ({"error": "secret must not be reflected"}, "invalid_response"),
])
def test_empty_or_bad_search_does_not_fetch_details(engine, response, status):
    calls = []
    def fetch(*args):
        calls.append(args)
        return response
    auto_ria.probe_once(engine, "key", fetch)
    assert len(calls) == 1
    assert auto_ria.probe_status(engine, True)["status"] == status


def test_missing_key_and_failures_never_retry(engine):
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError("api_key=do-not-leak")
    auto_ria.probe_once(engine, "", fail)
    assert not calls
    assert auto_ria.probe_status(engine, False)["status"] == "not_configured"
    auto_ria.probe_once(engine, "key", fail)
    auto_ria.probe_once(engine, "key", fail)
    assert len(calls) == 1
    assert auto_ria.probe_status(engine, True)["status"] == "check_failed"


@pytest.mark.parametrize("field,value", [("USD", float("nan")), ("USD", -1),
    ("linkToView", "https://evil.example/123"), ("linkToView", "//evil.example/123")])
def test_invalid_price_and_links_rejected(field, value):
    data = info()
    data[field] = value
    with pytest.raises(auto_ria.RiaError):
        auto_ria.listing_preview(data, "123")


def test_sold_and_wrong_id_rejected():
    data = info()
    data["autoData"]["isSold"] = True
    with pytest.raises(auto_ria.RiaError):
        auto_ria.listing_preview(data, "123")
    with pytest.raises(auto_ria.RiaError):
        auto_ria.listing_preview(info(), "456")


@pytest.mark.parametrize("error,expected", [
    (HTTPError("https://example.test/?api_key=SECRET", 429, "SECRET", {}, None), "quota_exceeded"),
    (HTTPError("https://example.test/?api_key=SECRET", 403, "SECRET", {}, None), "access_denied"),
    (URLError("SECRET"), "connection_error"),
])
def test_network_errors_redacted(monkeypatch, error, expected):
    class Opener:
        def open(self, *args, **kwargs):
            raise error
    monkeypatch.setattr(auto_ria, "build_opener", lambda *args: Opener())
    with pytest.raises(auto_ria.RiaError) as exc:
        auto_ria.fetch_json("SECRET", "search", {})
    assert str(exc.value) == expected
    assert "SECRET" not in str(exc.value)


def test_redirects_refused():
    assert auto_ria.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example") is None
