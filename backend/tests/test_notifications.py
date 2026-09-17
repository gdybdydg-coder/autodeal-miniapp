import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import telegram_setup
from backend.app import Settings, create_app
from backend.models import Base, MonitorControl, SourceProbe, TelegramTest
from backend.tests.test_backend import TOKEN, SECRET, command, headers, subscribe


@pytest.fixture
def db(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "notifications.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("current,username,expected,set_called", [
    ("", telegram_setup.BOT_USERNAME, "configured", True),
    (telegram_setup.WEBHOOK_URL, telegram_setup.BOT_USERNAME, "configured", True),
    ("https://other.example/webhook", telegram_setup.BOT_USERNAME, "existing_webhook_conflict", False),
    ("", "different_bot", "wrong_bot", False),
])
def test_webhook_setup_checks_identity_and_existing_destination(db, current, username, expected, set_called):
    calls = []
    def request(token, method, payload):
        assert token == TOKEN
        calls.append(method)
        if method == "getMe":
            return {"ok": True, "result": {"username": username}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": current}}
        assert method == "setWebhook"
        assert payload["url"] == telegram_setup.WEBHOOK_URL
        assert payload["secret_token"] == SECRET
        assert payload["drop_pending_updates"] is False and payload["allowed_updates"] == ["message"]
        return {"ok": True, "result": True}
    settings = Settings("unused", TOKEN, SECRET, configure_webhook=True)
    telegram_setup.configure(db, settings, request)
    assert telegram_setup.webhook_status(db) == {"status": expected}
    assert ("setWebhook" in calls) == set_called


def test_webhook_disabled_and_failed_checks_never_register(db):
    settings = Settings("unused", TOKEN, SECRET)
    telegram_setup.configure(db, settings, lambda *_: pytest.fail("network"))
    assert telegram_setup.webhook_status(db)["status"] == "not_configured"
    telegram_setup.configure(db, replace(settings, configure_webhook=True), lambda *_: {"uncertain": True})
    assert telegram_setup.webhook_status(db)["status"] == "unavailable"


@pytest.mark.parametrize("old_url,expected", [
    (telegram_setup.APP_URL + "?v=7", "configured"),
    ("https://different.example/app", "existing_menu_conflict"),
])
def test_menu_release_changes_only_our_launch_url_once(db, old_url, expected):
    settings = Settings("unused", TOKEN, SECRET, miniapp_release="test-19")
    calls = []
    current = {"type": "web_app", "text": "Old", "web_app": {"url": old_url}}
    def request(token, method, payload):
        nonlocal current
        calls.append(method)
        assert method in {"getMe", "getChatMenuButton", "setChatMenuButton"}
        if method == "getMe":
            return {"ok": True, "result": {"username": telegram_setup.BOT_USERNAME}}
        if method == "setChatMenuButton":
            assert payload["menu_button"]["web_app"]["url"] == telegram_setup.APP_URL + "?v=test-19"
            current = payload["menu_button"]
            return {"ok": True, "result": True}
        return {"ok": True, "result": current}
    telegram_setup.configure_menu(db, settings, request)
    assert telegram_setup.menu_status(db, "test-19")["status"] == expected
    count = len(calls)
    telegram_setup.configure_menu(db, settings, request)
    assert len(calls) == count and not settings.live and not settings.configure_webhook
    assert ("setChatMenuButton" in calls) == (expected == "configured")


@pytest.fixture
def api(db, monkeypatch):
    async def idle(engine, settings, stop):
        await stop.wait()
    monkeypatch.setattr("backend.app.monitor.run", idle)
    # This test exercises API consent/delivery; provider probes are covered separately.
    monkeypatch.setattr("backend.app.probe_once", lambda *_: None)
    monkeypatch.setattr("backend.app.verify_search_once", lambda *_: None)
    settings = Settings("unused", TOKEN, SECRET, True, True,
                        auto_ria_api_key="test-only", monitor_enabled=True)
    with TestClient(create_app(settings, db)) as client:
        with Session(db) as session:
            session.get(MonitorControl, "pilot").heartbeat = time.time()
            session.add(SourceProbe(id=telegram_setup.PROBE_ID, status="configured",
                                    checked_at=time.time(), requests=0, result={}))
            session.commit()
        yield client


def test_verified_private_start_test_and_pilot_slot_required(db, api, monkeypatch):
    sent = []
    def send(token, uid):
        sent.append(uid)
        return {"ok": True, "result": {"message_id": 7}}
    monkeypatch.setattr(telegram_setup, "send_test", send)
    assert api.post("/api/notifications/test").status_code == 401
    assert api.post("/api/notifications/test", headers=headers()).status_code == 409
    command(api, "/start")
    assert subscribe(api).json()["detail"] == "Send a test notification first"
    assert api.post("/api/notifications/test", headers=headers()).json() == {"state": "sent"}
    assert api.post("/api/notifications/test", headers=headers()).status_code == 429
    assert sent == [111]
    assert api.get("/api/notifications/status", headers=headers()).json()["test_sent"] is True
    first = subscribe(api, brand="Volkswagen").json()["id"]
    assert subscribe(api, brand="BMW").json()["detail"] == "Pilot allows one active search"
    command(api, "/start", uid=222)
    assert api.post("/api/notifications/test", headers=headers(222)).json()["state"] == "sent"
    assert subscribe(api, uid=222).json()["detail"] == "Pilot allows one active search"
    assert api.patch(f"/api/subscriptions/{first}", headers=headers(222), json={"enabled": False}).status_code == 404
    assert api.patch(f"/api/subscriptions/{first}", headers=headers(), json={"enabled": False}).status_code == 200
    assert subscribe(api, uid=222).status_code == 200


def test_ambiguous_test_is_not_retried_or_counted_as_ready(db, api, monkeypatch):
    command(api, "/start")
    calls = []
    monkeypatch.setattr(telegram_setup, "send_test", lambda *_: calls.append(1) or {"uncertain": True})
    assert api.post("/api/notifications/test", headers=headers()).json() == {"state": "uncertain"}
    assert api.post("/api/notifications/test", headers=headers()).status_code == 429
    assert calls == [1]
    assert subscribe(api).status_code == 409
    with Session(db) as session:
        assert session.get(TelegramTest, 111).state == "uncertain"


def test_dead_monitor_does_not_allow_new_activation(db, api):
    command(api, "/start")
    with Session(db) as session:
        session.get(MonitorControl, "pilot").heartbeat = 0
        session.commit()
    assert api.get("/api/notifications/status", headers=headers()).json()["available"] is False
    assert subscribe(api).status_code == 503
