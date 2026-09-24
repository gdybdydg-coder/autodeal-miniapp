import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend import bot_commands, telegram_setup
from backend.app import Settings, create_app
from backend.models import BotReply, MonitorControl, Search, SourceProbe, User
from backend.tests.test_backend import TOKEN, SECRET, command, headers, subscribe


@pytest.fixture
def setup(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "commands.db"),
                           connect_args={"check_same_thread": False})
    settings = Settings("unused", TOKEN, SECRET, True, True, miniapp_release="onboarding")
    with TestClient(create_app(settings, engine)) as api:
        yield engine, settings, api
    engine.dispose()


def accepted(calls):
    def send(token, method, payload):
        assert token == TOKEN and method == "sendMessage"
        calls.append(payload)
        return {"ok": True, "result": {"message_id": len(calls)}}
    return send


def test_start_reply_confirms_connection_without_enabling_search_or_spending_source_calls(setup):
    engine, settings, api = setup
    saved = subscribe(api, enabled=False).json()
    calls = []
    assert command(api, "/start").status_code == 200
    assert api.get("/api/notifications/status", headers=headers()).json()["test_sent"] is False
    assert bot_commands.deliver_one(engine, settings, accepted(calls)) == "sent"
    status = api.get("/api/notifications/status", headers=headers()).json()
    assert status["test_sent"] is True and status["telegram_ready"] is True
    with Session(engine) as db:
        assert db.get(Search, saved["id"]).enabled is False
    assert calls[0]["chat_id"] == 111
    assert "Вітаємо" in calls[0]["text"]
    assert calls[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].endswith("?v=onboarding")
    command(api, "/start")  # Telegram webhook retry.
    assert bot_commands.deliver_one(engine, settings, accepted(calls)) == "idle"
    assert len(calls) == 1


@pytest.mark.parametrize("response,expected", [({"uncertain": True}, "uncertain"),
    ({"ok": False, "error_code": 403}, "failed"),
    ({"ok": True, "result": {"message_id": True}}, "uncertain")])
def test_failed_or_uncertain_welcome_never_grants_delivery_or_retries(setup, response, expected):
    engine, settings, api = setup
    command(api, "/start")
    assert bot_commands.deliver_one(engine, settings, lambda *_: response) == expected
    assert api.get("/api/notifications/status", headers=headers()).json()["test_sent"] is False
    assert bot_commands.deliver_one(engine, settings, lambda *_: pytest.fail("retry")) == "idle"


def test_stop_cancels_queued_start_and_confirms_without_deleting_filters(setup):
    engine, settings, api = setup
    command(api, "/start")
    sid = subscribe(api).json()["id"]
    command(api, "/stop", update=2)
    calls = []
    assert bot_commands.deliver_one(engine, settings, accepted(calls)) == "cancelled"
    assert bot_commands.deliver_one(engine, settings, accepted(calls)) == "sent"
    assert "зупинено" in calls[0]["text"]
    with Session(engine) as db:
        assert db.get(User, 111).ready is False
        assert db.get(Search, sid).enabled is False
    command(api, "/start", update=3)
    bot_commands.deliver_one(engine, settings, accepted(calls))
    with Session(engine) as db:
        assert db.get(Search, sid).enabled is False


def test_help_is_read_only_for_consent_and_wrong_bot_mentions_are_ignored(setup):
    engine, settings, api = setup
    command(api, "/start@another_bot")
    assert bot_commands.deliver_one(engine, settings, lambda *_: pytest.fail("wrong bot")) == "idle"
    command(api, "/help")
    calls = []
    assert bot_commands.deliver_one(engine, settings, accepted(calls)) == "sent"
    with Session(engine) as db:
        assert db.get(User, 111).ready is False
    assert "Як користуватися" in calls[0]["text"]


def test_abandoned_sending_and_stale_pending_commands_are_not_replayed(setup):
    engine, settings, api = setup
    command(api, "/start", date=int(time.time()) - 301)
    assert bot_commands.deliver_one(engine, settings, lambda *_: pytest.fail("stale")) == "cancelled"
    command(api, "/start", update=2)
    with Session(engine) as db:
        row = db.scalar(select(BotReply).where(BotReply.state == "pending"))
        row.state = "sending"
        db.commit()
    assert bot_commands.deliver_one(engine, settings, lambda *_: pytest.fail("crash replay")) == "idle"


def test_accepted_welcome_satisfies_monitor_activation_gate_for_its_user_only(setup, monkeypatch):
    engine, settings, api = setup
    command(api, "/start")
    bot_commands.deliver_one(engine, settings, accepted([]))
    async def idle(engine, settings, stop):
        await stop.wait()
    monkeypatch.setattr("backend.app.monitor.run", idle)
    monkeypatch.setattr("backend.app.probe_once", lambda *_: None)
    monkeypatch.setattr("backend.app.verify_search_once", lambda *_: None)
    live = replace(settings, monitor_enabled=True, auto_ria_api_key="fixture")
    with TestClient(create_app(live, engine)) as monitored:
        with Session(engine) as db:
            db.get(MonitorControl, "pilot").heartbeat = time.time()
            db.add(SourceProbe(id=telegram_setup.PROBE_ID, status="configured", checked_at=time.time(), requests=0, result={}))
            db.commit()
        assert subscribe(monitored).status_code == 200
        command(monitored, "/start", uid=222)
        assert subscribe(monitored, uid=222).status_code == 409


def test_command_menu_requires_verified_bot_and_is_idempotent(setup):
    engine, settings, _ = setup
    settings = replace(settings, configure_webhook=True)
    calls = []
    def send(token, method, payload):
        calls.append(method)
        assert method == "setMyCommands"
        assert {c["command"] for c in payload["commands"]} == {"start", "help", "stop", "stats"}
        return {"ok": True, "result": True}
    bot_commands.configure(engine, settings, send)
    assert calls == []
    with Session(engine) as db:
        db.add(SourceProbe(id=telegram_setup.PROBE_ID, status="configured", checked_at=time.time(), requests=0, result={}))
        db.commit()
    bot_commands.configure(engine, settings, send)
    bot_commands.configure(engine, settings, send)
    assert calls == ["setMyCommands"]
