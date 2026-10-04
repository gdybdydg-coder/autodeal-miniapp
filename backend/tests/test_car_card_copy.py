"""The requested copy-only change never alters source evidence or values."""
import json
from types import SimpleNamespace

import pytest
from pydantic import HttpUrl

from backend.tests.test_ria_market_range import fixture, priced_car
from backend.worker import TelegramSender


@pytest.mark.parametrize("route", ["text", "photo", "fallback"])
def test_car_card_has_no_api_disclaimer_in_any_transport(monkeypatch, route):
    candidate, quote = fixture(3500, 4707, 5202)
    car = priced_car(candidate, quote)
    car = car.model_copy(update={"photo": None if route == "text" else HttpUrl("https://example.test/car.jpg")})
    before = car.model_dump(mode="json")
    text, buttons = TelegramSender.card(car, historical_at=candidate["observed_at"])
    assert "може відрізня" not in text
    assert "Оцінка API" not in text and "Оцінка АПІ" not in text
    assert "📊 Ринкова ціна: ≈ $4 471,65" in text
    assert "🔥 Вигода: 21,7%" in text
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            method = url.rsplit("/", 1)[-1]
            payload = kwargs.get("data") or kwargs["json"]
            rendered = payload.get("caption") or payload["text"]
            markup = payload["reply_markup"]
            assert (json.loads(markup) if isinstance(markup, str) else markup) == buttons
            assert rendered == text
            calls.append(method)
            data = ({"ok": False, "error_code": 400} if route == "fallback" and method == "sendPhoto"
                    else {"ok": True, "result": {"message_id": 99}})
            return SimpleNamespace(status_code=400 if data["ok"] is False else 200, json=lambda: data)
    monkeypatch.setattr("backend.worker.httpx.Client", Client)
    sender = TelegramSender("synthetic-test")
    monkeypatch.setattr(sender.photos, "load", lambda url: b"synthetic-photo")
    assert sender(123, car)["ok"] is True
    assert calls == (["sendPhoto", "sendMessage"] if route == "fallback"
                     else ["sendMessage"] if route == "text" else ["sendPhoto"])
    assert car.model_dump(mode="json") == before
