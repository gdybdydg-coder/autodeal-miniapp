"""Hold deployment must not start the app, migrate, spend quota or send anything."""
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setenv("RADAR_CUTOVER_HOLD", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Maintenance called normal startup or an external dependency")

    monkeypatch.setattr(app_module, "create_app", forbidden)
    monkeypatch.setattr(app_module.Settings, "env", forbidden)
    monkeypatch.setattr(app_module, "create_engine", forbidden)
    monkeypatch.setattr(app_module.monitor, "run", forbidden)
    monkeypatch.setattr(app_module.telegram_setup, "call", forbidden)
    monkeypatch.setattr(app_module.RiaSearch, "acquire", forbidden)
    from backend import radar_migration
    monkeypatch.setattr(radar_migration, "migrate", forbidden)
    with TestClient(app_module.factory()) as client:
        yield client


def test_hold_health_exposes_only_configuration_presence(held, monkeypatch):
    for key in ("MONITOR_ENABLED", "DELIVERY_ENABLED", "SOURCE_READY", "RIA_AI_PRICE_ENABLED"):
        monkeypatch.setenv(key, "true")
    monkeypatch.setenv("AUTO_RIA_API_KEY", "private-provider-key")
    monkeypatch.setenv("AUTO_RIA_USER_ID", "987654321")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    response = held.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "maintenance": True,
        "monitor_enabled": True, "delivery_enabled": True, "source_ready": True,
        "ria_ai_price_enabled": True, "auto_ria_api_key_configured": True,
        "auto_ria_user_id_configured": True, "release": "a" * 40}
    assert response.headers["cache-control"] == "no-store"
    assert "private-provider-key" not in response.text and "987654321" not in response.text


def test_hold_health_does_not_claim_unconfigured_dependencies(held, monkeypatch):
    for key in ("MONITOR_ENABLED", "DELIVERY_ENABLED", "SOURCE_READY", "RIA_AI_PRICE_ENABLED",
                "AUTO_RIA_API_KEY", "AUTO_RIA_USER_ID", "RENDER_GIT_COMMIT"):
        monkeypatch.delenv(key, raising=False)
    body = held.get("/health").json()
    assert body.pop("ok") is True and body.pop("maintenance") is True
    assert all(value is False for value in body.values())


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/docs"), ("GET", "/openapi.json"),
    ("GET", "/api/radars"), ("POST", "/api/radars"),
    ("POST", "/api/subscriptions"), ("PUT", "/api/subscriptions/1"),
    ("PATCH", "/api/subscriptions/1"), ("DELETE", "/api/subscriptions/1"),
    ("GET", "/api/catalog"), ("POST", "/api/cars/search"),
    ("POST", "/telegram/webhook"), ("GET", "/api/billing/status"),
    ("OPTIONS", "/api/radars"), ("POST", "/health"),
])
def test_hold_rejects_all_non_health_requests_including_webhook(held, method, path):
    response = held.request(method, path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Оновлюємо застосунок. Спробуйте ще раз за хвилину."}
    assert response.headers["retry-after"] == "60"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("value", [None, "false"])
def test_without_hold_factory_uses_unchanged_normal_startup(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RADAR_CUTOVER_HOLD", raising=False)
    else:
        monkeypatch.setenv("RADAR_CUTOVER_HOLD", value)
    settings, application, calls = object(), object(), []
    monkeypatch.setattr(app_module.Settings, "env", lambda: settings)
    def ordinary(actual, **kwargs):
        calls.append((actual, kwargs))
        return application
    monkeypatch.setattr(app_module, "create_app", ordinary)
    assert app_module.factory() is application
    assert calls == [(settings, {"paid_source_only": True})]
