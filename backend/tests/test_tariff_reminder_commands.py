"""Offline checks for private reminder controls and authorization boundaries."""
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import tariff_reminder_commands as commands, telegram_setup
from backend.app import Settings

NOW = 1791033600
ADMIN = 987654321
CLIENT = 111


def event(*, uid=CLIENT, text="/reminders", update_id=1, callback=None, stamp=NOW):
    msg = {"chat": {"id": uid, "type": "private"}, "from": {"id": uid},
           "date": stamp, "text": text}
    if callback is not None:
        return {"update_id": update_id, "callback_query": {"id": "fixture-callback",
            "from": {"id": uid}, "message": msg, "data": callback}}
    return {"update_id": update_id, "message": msg}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(ADMIN))
    engine = create_engine("sqlite:///" + str(tmp_path / "commands.db"))
    settings = Settings("unused", "fixture-token", "x" * 40, admin_telegram_id=ADMIN,
                        manual_payment_review_enabled=True)
    calls = []
    transport = []
    data = {"id": "daily-first-purchase-v1", "enabled": True,
            "next_run_at": NOW+86400, "next_run_kyiv": "2026-10-04T09:00:00+03:00",
            "eligible_recipients": 29, "heartbeat": NOW,
            "text": "🚘 AutoDeal\n💳 250 грн / 30 днів",
            "buttons": ["💳 Переглянути тариф", "🔕 Не нагадувати"], "last_result": {}}

    def snapshot(db, actual_settings, now):
        calls.append(("snapshot", actual_settings, now))
        return deepcopy(data)

    def set_enabled(actual_engine, actual_settings, actor, enabled, now, *, update_id=None):
        calls.append(("set_enabled", actual_engine, actual_settings, actor, enabled, now, update_id))
        data["enabled"] = enabled
        return deepcopy(data)

    def preference(actual_engine, actual_settings, uid, enabled, update_id, now):
        calls.append(("set_preference", actual_engine, actual_settings, uid, enabled, update_id, now))
        return enabled

    api = SimpleNamespace(snapshot=snapshot, set_enabled=set_enabled, set_preference=preference)
    monkeypatch.setattr(commands, "_api", lambda: api)

    def request(token, method, payload, **kwargs):
        transport.append((method, payload))
        return {"ok": True}

    yield SimpleNamespace(engine=engine, settings=settings, calls=calls, data=data,
                         api=api, transport=transport, request=request)
    engine.dispose()


def handle(setup, value, *, settings=None, request=None):
    return commands.handle(setup.engine, settings or setup.settings, value,
                           request=request or setup.request, now=NOW)


def test_admin_view_has_current_count_schedule_preview_and_controls(setup):
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    assert "2026-10-04T09:00:00+03:00" in result["text"]
    assert "Придатних отримувачів зараз: 29" in result["text"]
    assert "09:00–09:30" in result["text"]
    assert setup.data["text"] in result["text"]
    assert result["protect_content"] is True
    assert result["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == commands.PREFIX + "admin:off"
    assert setup.transport == []


def test_admin_view_keeps_previous_day_summary_after_today_runs(setup):
    setup.data["last_result"] = {"date_kyiv": "2026-10-04", "selected": 0,
        "sent": 0, "excluded": 0, "errors": 0, "uncertain": 0}
    setup.data["history"] = {"records": [
        {"date_kyiv": "2026-10-03", "run_observed": True, "selected": 3,
         "queued": 3, "telegram_accepted": 2, "errors": 0, "uncertain": 1},
        {"date_kyiv": "2026-10-04", "run_observed": True, "selected": 0}]}
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    assert "Останній запуск 2026-10-04: обрано 0" in result["text"]
    assert ("Попередній день 2026-10-03: обрано 3, поставлено в чергу 3, "
            "Telegram: 2, помилки 0, невизначено 1") in result["text"]
    assert len(result["text"]) < 4000
    assert setup.transport == []


@pytest.mark.parametrize("status,expected", [
    ("before_first_planned_run", "запуск ще не був запланований."),
    ("no_saved_campaign", "збереженого запуску немає; кількість відправлень невідома."),
])
def test_admin_previous_day_missing_history_is_not_reported_as_zero(setup, status, expected):
    setup.data["history"] = {"records": [
        {"date_kyiv": "2026-10-03", "run_observed": False, "status": status},
        {"date_kyiv": "2026-10-04", "run_observed": True}]}
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    assert "Попередній день 2026-10-03: " + expected in result["text"]
    assert "Telegram: 0" not in result["text"]


@pytest.mark.parametrize("verb,enabled", [("on", True), ("off", False)])
def test_admin_toggle_checks_actor_and_returns_updated_state(setup, verb, enabled):
    result = handle(setup, event(uid=ADMIN, callback=commands.PREFIX + "admin:" + verb))
    assert setup.calls == [("set_enabled", setup.engine, setup.settings, ADMIN, enabled, NOW, 1)]
    assert ("✅ Увімкнено" if enabled else "🔕 Вимкнено") in result["text"]
    assert [call[0] for call in setup.transport] == ["answerCallbackQuery"]


def test_admin_command_can_disable_without_an_immediate_broadcast(setup):
    handle(setup, event(uid=ADMIN, text="/tariff_reminders off"))
    assert setup.calls[0][0] == "set_enabled" and setup.calls[0][4] is False
    assert setup.transport == []


@pytest.mark.parametrize("callback", [None, commands.PREFIX+"admin:view", commands.PREFIX+"admin:on", commands.PREFIX+"admin:off"])
def test_other_client_has_no_admin_read_or_write_or_database_access(setup, monkeypatch, callback):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unauthorized control reached the database")
    monkeypatch.setattr(commands, "Session", forbidden)
    result = handle(setup, event(text="/tariff_reminders", callback=callback))
    assert result == {"ok": True}
    assert setup.calls == [] and setup.transport == []


def test_protected_owner_configuration_is_checked_for_every_call(setup, monkeypatch):
    handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    setup.calls.clear()
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "123")
    result = handle(setup, event(uid=ADMIN, callback=commands.PREFIX+"admin:off"))
    assert result == {"ok": True} and setup.calls == []


def test_reminder_prompt_does_not_opt_in_and_explains_frequency(setup):
    result = handle(setup, event())
    assert "щодня о 09:00 за Києвом" in result["text"]
    assert setup.calls == [] and setup.transport == []
    assert result["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == commands.PREFIX+"on"


def test_user_opt_out_has_exact_confirmation_and_only_changes_category(setup):
    result = handle(setup, event(callback=commands.PREFIX+"off", update_id=91))
    assert result["text"] == "🔕 Нагадування про придбання тарифу вимкнено"
    assert setup.calls == [("set_preference", setup.engine, setup.settings, CLIENT, False, 91, NOW)]
    assert [call[0] for call in setup.transport] == ["answerCallbackQuery"]


def test_user_explicit_button_enables_category_and_old_message_button_stays_usable(setup):
    result = handle(setup, event(callback=commands.PREFIX+"on", stamp=NOW-86400))
    assert setup.calls[0][0] == "set_preference" and setup.calls[0][4] is True
    assert "09:00 за Києвом" in result["text"]


def test_failed_callback_ack_does_not_discard_opt_out(setup):
    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic acknowledgment timeout")
    result = handle(setup, event(callback=commands.PREFIX+"off"), request=unavailable)
    assert result["text"] == commands.OFF_CONFIRMATION
    assert setup.calls[0][0] == "set_preference"


def test_replayed_old_action_reports_actual_persisted_preference(setup):
    setup.api.set_preference = lambda *args: False
    result = handle(setup, event(callback=commands.PREFIX+"on"))
    assert result["text"] == commands.OFF_CONFIRMATION


@pytest.mark.parametrize("text", ["/start", "/start subscribe", "/stats", "/stop", "/marketing", "/subscription"])
def test_existing_commands_do_not_change_reminder_preferences(setup, text):
    assert handle(setup, event(text=text)) is None
    assert setup.calls == [] and setup.transport == []


@pytest.mark.parametrize("changed", [
    {"update_id": True}, {"update_id": -1}, {"update_id": 2**63},
    {"update_id": "1"}, {"stamp": NOW-301}, {"stamp": NOW+1},
])
def test_invalid_or_stale_command_is_not_executed(setup, changed):
    assert handle(setup, event(uid=ADMIN, text="/tariff_reminders off", **changed)) == {"ok": True}
    assert setup.calls == []


@pytest.mark.parametrize("kind", ["group", "actor_chat_mismatch", "bot", "boolean_uid", "foreign_mention", "malformed_callback", "empty_callback_id", "unknown_action"])
def test_invalid_event_never_reads_or_changes_controls(setup, kind):
    value = event(uid=ADMIN, text="/tariff_reminders", callback=commands.PREFIX+"admin:off")
    cb = value["callback_query"]
    if kind == "group": cb["message"]["chat"]["type"] = "supergroup"
    elif kind == "actor_chat_mismatch": cb["message"]["chat"]["id"] = CLIENT
    elif kind == "bot": cb["from"]["is_bot"] = True
    elif kind == "boolean_uid": cb["from"]["id"] = True
    elif kind == "foreign_mention": cb["message"]["text"] = "/tariff_reminders@other_bot"
    elif kind == "malformed_callback": value["callback_query"] = "invalid"
    elif kind == "empty_callback_id": cb["id"] = ""
    elif kind == "unknown_action": cb["data"] = commands.PREFIX+"admin:arbitrary"
    assert handle(setup, value) in (None, {"ok": True})
    assert setup.calls == [] and setup.transport == []


def test_known_bot_mention_is_supported(setup):
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders@"+telegram_setup.BOT_USERNAME))
    assert "Придатних отримувачів зараз: 29" in result["text"]


def test_database_error_has_no_false_statistics_or_success_confirmation(setup):
    def fail(*args, **kwargs):
        raise OperationalError("synthetic", {}, Exception("private connection detail"))
    setup.api.snapshot = fail
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    assert result["text"] == commands.UNAVAILABLE
    assert "0" not in result["text"] and "private" not in result["text"]
    setup.api.set_preference = fail
    result = handle(setup, event(callback=commands.PREFIX+"off"))
    assert result["text"] == commands.UNAVAILABLE


def test_last_summary_only_renders_aggregate_fields(setup):
    setup.data["last_result"] = {"selected": 3, "sent": 1, "excluded": 1,
                                 "temporary_errors": 0, "permanent_errors": 0,
                                 "uncertain": 1, "user_id": "private-user", "receipt": "private-receipt"}
    result = handle(setup, event(uid=ADMIN, text="/tariff_reminders"))
    assert "обрано 3, надіслано 1, виключено 1, помилки 0, невизначено 1" in result["text"]
    assert "private-user" not in result["text"] and "private-receipt" not in result["text"]


@pytest.fixture
def webhook(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import billing
    from backend.app import create_app
    from backend.billing_models import BillingControl
    from backend.manual_payment_models import ManualBase
    from backend.models import Base, Search, User

    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(ADMIN))
    monkeypatch.setattr(telegram_setup, "call", lambda *args, **kwargs: {"ok": True})
    engine = create_engine("sqlite:///" + str(tmp_path / "webhook.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    ManualBase.metadata.create_all(engine)
    settings = Settings("unused", "fixture-token", "x"*40, admin_telegram_id=ADMIN,
                        manual_payment_review_enabled=True)
    with Session(engine) as db, db.begin():
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=True, offer={}))
        db.add(User(id=CLIENT, ready=True))
        db.add(Search(user_id=CLIENT, fingerprint="fixture", name="Saved filter",
                      filters={"brand": "BMW"}, enabled=True, after_listing=123))
    client = TestClient(create_app(settings, engine))
    yield engine, settings, client
    client.close()
    engine.dispose()


def post(webhook, value, *, secret=None):
    _, settings, client = webhook
    return client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token":
        settings.webhook_secret if secret is None else secret}, json=value)


def test_real_webhook_opt_out_persists_without_changing_paid_access_or_filters(webhook):
    from backend import billing
    from backend.billing_models import Entitlement, MarketingConsent, TariffReminderPreference
    from backend.models import Search, User
    engine, _, _ = webhook
    now = time.time()
    with Session(engine) as db, db.begin():
        db.add(Entitlement(user_id=CLIENT, expires_at=now+86400, updated_at=now))
        db.add(MarketingConsent(user_id=CLIENT, allowed=True, blocked=False, update_id=3,
            at=now-60, source="explicit_marketing_button_v1"))
    response = post(webhook, event(callback=commands.PREFIX+"off", update_id=11))
    assert response.status_code == 200 and response.json()["text"] == commands.OFF_CONFIRMATION
    with Session(engine) as db:
        assert db.get(TariffReminderPreference, CLIENT).enabled is False
        assert db.get(MarketingConsent, CLIENT).allowed is True
        assert db.get(MarketingConsent, CLIENT).update_id == 3
        assert billing.allowed(db, CLIENT, now) is True
        assert db.get(User, CLIENT).ready is True
        search = db.scalar(select(Search).where(Search.user_id == CLIENT))
        assert search.enabled is True and search.filters == {"brand": "BMW"} and search.after_listing == 123
    # A fresh DB session and replaying an older 'on' action preserve the refusal.
    result = post(webhook, event(callback=commands.PREFIX+"on", update_id=10))
    assert result.json()["text"] == commands.OFF_CONFIRMATION
    with Session(engine) as db:
        assert db.get(TariffReminderPreference, CLIENT).enabled is False


def test_real_webhook_requires_secret_before_preference_write(webhook):
    from backend.billing_models import TariffReminderPreference
    response = post(webhook, event(callback=commands.PREFIX+"off"), secret="invalid")
    assert response.status_code == 403
    with Session(webhook[0]) as db:
        assert db.get(TariffReminderPreference, CLIENT) is None


def test_real_admin_view_and_disable_are_guarded_and_replay_safe(webhook):
    from backend import billing, tariff_reminders
    from backend.billing_models import TariffReminderSchedule
    engine, _, _ = webhook
    now = time.time()
    future = tariff_reminders.next_morning(now)
    with Session(engine) as db, db.begin():
        db.add(TariffReminderSchedule(id=tariff_reminders.ID, enabled=True,
            installed_at=now, first_run_at=future, next_run_at=future))
    view = post(webhook, event(uid=ADMIN, text="/tariff_reminders", stamp=int(now)))
    assert view.status_code == 200 and "Придатних отримувачів зараз: 0" in view.json()["text"]
    denied = post(webhook, event(callback=commands.PREFIX+"admin:off"))
    assert denied.json() == {"ok": True}
    with Session(engine) as db:
        assert db.get(TariffReminderSchedule, tariff_reminders.ID).enabled is True
    disabled = post(webhook, event(uid=ADMIN, callback=commands.PREFIX+"admin:off", update_id=20))
    assert "🔕 Вимкнено" in disabled.json()["text"]
    replay = post(webhook, event(uid=ADMIN, callback=commands.PREFIX+"admin:on", update_id=19))
    assert "🔕 Вимкнено" in replay.json()["text"]
    with Session(engine) as db:
        assert db.get(TariffReminderSchedule, tariff_reminders.ID).enabled is False
        ctrl = billing.control(db)
        assert ctrl.sales is False and ctrl.enforce is True and ctrl.offer == {}
