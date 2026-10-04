"""Exact official request bodies; fixture HTTP only, no provider/native claims."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from backend import ria_ai_price as ai, ria_market_range as native
from backend.auto_ria import RiaError


@pytest.mark.parametrize("period,params", [
    (168, {"omniId": "40517476"}), (90, {"omniId": "40517476"}),
    (168, {"categoryId": "1", "brandId": "62", "modelId": "60014",
           "bodyId": "8", "fuelId": "2", "gearBoxId": "1",
           "year": {"gte": "2005", "lte": "2005"},
           "mileage": {"gte": "345", "lte": "345"},
           "engineVolume": {"gte": "1.9", "lte": "1.9"}}),
])
def test_bounded_transport_retains_actual_parameter_and_never_native_proof(monkeypatch, period, params):
    events, calls = [], []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            assert limit == ai.MAX_BYTES + 1
            return json.dumps({"statisticData": [{"type": "avgPrice", "price": {"USD": 7012},
                "avgValueRange": .05, "quantityAdv": 170}],
                "similarCars": [{"VIN": "private-vin", "seller": "private-seller"}]}).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            url = urlparse(request.full_url)
            assert url.netloc == "developers.ria.com" and url.path == "/auto/ai-avarage-price/"
            assert parse_qs(url.query) == {"api_key": ["private-key"], "user_id": ["42"]}
            assert request.method == "POST" and timeout == 20
            assert json.loads(request.data) == {"langId": 4, "period": period, "params": params}
            return Response()
    monkeypatch.setattr(ai, "build_opener", lambda *_: Opener())
    result = ai.fetch_observation("private-key", "42", "40517476",
        period_parameter=period, params=params, attempt_telemetry=events.append)
    assert len(calls) == 1 and events == [{"event": "transport_started"},
                                         {"event": "http_response", "http_status": 200}]
    assert (result["lower_usd"], result["upper_usd"]) == (6661, 7362)
    assert result["average_usd"] == 7012 and result["quantity"] == 170
    assert result["period_parameter"] == period and "period_hours" not in result
    assert result["basis"] == ai.API_BASIS and not native.range_valid(result, "40517476", result["observed_at"])
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("period,params", [(True, {"omniId": "40517476"}),
    (365, {"omniId": "40517476"}), (90, {"omniId": "999"}),
    (168, {"VIN": "private"}), (168, {})])
def test_invalid_diagnostic_request_never_opens_transport(monkeypatch, period, params):
    monkeypatch.setattr(ai, "build_opener", lambda *_: pytest.fail("transport"))
    with pytest.raises(RiaError, match="ai_not_configured"):
        ai.fetch_observation("private-key", "42", "40517476", period_parameter=period, params=params)


@pytest.mark.parametrize("fail", [False, True])
def test_explicit_startup_comparison_runs_once_and_failure_preserves_app(tmp_path, monkeypatch, caplog, fail):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from backend.app import Settings, create_app
    from backend import ria_source_comparison
    calls = []
    def check(engine, settings):
        calls.append(settings.ria_source_comparison_listing_id)
        if fail:
            raise ValueError("private-exception-details")
    monkeypatch.setattr(ria_source_comparison, "check_once", check)
    url = "sqlite:///" + str(tmp_path / "comparison.db")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    settings = Settings(url, "test-only", "test-only-secret-01234567890123456789",
                        ria_source_comparison_listing_id="40517476")
    try:
        with TestClient(create_app(settings, engine, paid_source_only=True)) as client:
            assert client.get("/health").status_code == 200  # App lifecycle only; no delivery claim.
        assert calls == ["40517476"]
        assert "private-exception-details" not in caplog.text
        if fail:
            assert "source comparison unavailable (ValueError)" in caplog.text
    finally:
        engine.dispose()
