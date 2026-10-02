"""Purchase history counts use isolated ledgers; no real money or external I/O."""
from dataclasses import replace
import json
import logging
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text as sql_text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import billing, bot_commands, manual_payments as payments, purchase_stats
from backend.app import Settings, create_app
from backend.billing_models import AccessEvent, BillingControl, Entitlement
from backend.manual_payment_models import PaymentRequest
from backend.models import Base, Search, StarsTestOrder, SubscriptionPreview, TelegramTest, User
from backend.tests.test_backend import SECRET, TOKEN, command


ADMIN = 987654321
FIRST_CLIENT = 10001
NOW = 1790922000


@pytest.fixture(params=["sqlite"] + (["postgresql"] if os.getenv("AUTODEAL_TEST_POSTGRES_URL") else []))
def ledger(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(ADMIN))
    monkeypatch.delenv("SUBSCRIPTION_LAUNCH_PREPARED", raising=False)
    monkeypatch.delenv("MANUAL_PAYMENT_PUBLIC_ENABLED", raising=False)
    admin_engine = schema = None
    if request.param == "postgresql":
        url = make_url(os.environ["AUTODEAL_TEST_POSTGRES_URL"])
        # Only the same dedicated localhost fixture accepted by payment tests.
        # DATABASE_URL and arbitrary remote/production accounts are never read.
        if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1"
                or url.database != "autodeal_manual_fixture" or url.username != "autodeal_fixture"
                or url.password != "fixture-only" or url.query or not url.port):
            pytest.fail("Expected the isolated localhost manual-payment fixture database")
        admin_engine = create_engine(url, connect_args={"connect_timeout": 5})
        schema = "purchase_stats_fixture_" + uuid.uuid4().hex
        with admin_engine.begin() as connection:
            connection.execute(sql_text('CREATE SCHEMA "' + schema + '"'))
        url = url.update_query_dict({"options": "-csearch_path=" + schema + " -cstatement_timeout=15000"})
        engine = create_engine(url, connect_args={"connect_timeout": 5})
    else:
        url = "sqlite:///" + str(tmp_path / "purchase-statistics.sqlite")
        engine = create_engine(url, connect_args={"check_same_thread": False})
    settings = Settings(url, TOKEN, SECRET, admin_telegram_id=ADMIN,
                        manual_payment_review_enabled=True)
    Base.metadata.create_all(engine)
    payments.initialize(engine, settings)
    with Session(engine) as db, db.begin():
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=False, offer={}))
        db.add(User(id=ADMIN, ready=False))
    yield engine, settings
    engine.dispose()
    if admin_engine is not None:
        with admin_engine.begin() as connection:
            connection.execute(sql_text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin_engine.dispose()


def clients(ledger, number):
    with Session(ledger[0]) as db, db.begin():
        db.add_all(User(id=FIRST_CLIENT + i, ready=bool(i % 2)) for i in range(number))


def review_request(ledger, uid, now=NOW):
    engine, settings = ledger
    request = payments.create_request(engine, settings, uid, now, name="Test")
    return payments.report_paid(engine, settings, uid, request["code"], now + 1,
                                receipt_file_id="SYNTHETIC-RECEIPT", receipt_kind="photo")


def approve(ledger, uid, now=NOW, *, legacy=False, request=None):
    engine, settings = ledger
    request = request or review_request(ledger, uid, now)
    evidence = dict(bank_verified=True)
    if legacy:
        evidence.update(account="FIXTURE-ACCOUNT", operation="CREDIT-" + request["code"],
                        actual_amount_minor=25000)
    preview = payments.preview(engine, settings, ADMIN, request["code"], request["revision"],
                               now + 2, **evidence)
    result = payments.confirm(engine, settings, ADMIN, preview["confirmation"], now + 3)
    return request, preview, result


def counts(ledger):
    with Session(ledger[0]) as db:
        return purchase_stats.counts(db, ledger[1])


def text(ledger):
    with Session(ledger[0]) as db:
        return bot_commands.stats_text(db, ADMIN, ADMIN, settings=ledger[1])


def test_165_clients_14_distinct_buyers_and_preserved_stats(ledger):
    clients(ledger, 165)
    for i in range(14):
        approve(ledger, FIRST_CLIENT + i, legacy=bool(i % 2))
    assert counts(ledger) == {"total": 165, "buyers": 14, "not_purchased": 151}
    output = text(ledger)
    assert "💳 Підписки\n✅ Купили тариф: 14/165\n⏳ Ще не купили: 151" in output
    assert "Усього клієнтів: 165" in output and "Підключені до бота:" in output
    assert "Активних пошуків:" in output and "Користувачів з активним пошуком:" in output
    assert "Черги зараз" in output and "Затримки за останні 24 год" in output
    assert "Квота AUTO.RIA" in output or "Облік квоти AUTO.RIA" in output
    assert "SYNTHETIC-RECEIPT" not in output and "FIXTURE-ACCOUNT" not in output
    assert str(FIRST_CLIENT) not in output
    assert len(output.encode("utf-16-le")) // 2 < 4096


def test_first_approval_is_visible_next_call_and_repeat_is_idempotent(ledger):
    clients(ledger, 165)
    for i in range(14):
        approve(ledger, FIRST_CLIENT + i)
    waiting = review_request(ledger, FIRST_CLIENT + 14)
    assert counts(ledger) == {"total": 165, "buyers": 14, "not_purchased": 151}
    assert "14/165" in text(ledger)
    _, preview, _ = approve(ledger, FIRST_CLIENT + 14, request=waiting)
    assert counts(ledger) == {"total": 165, "buyers": 15, "not_purchased": 150}
    assert "✅ Купили тариф: 15/165\n⏳ Ще не купили: 150" in text(ledger)
    assert payments.confirm(ledger[0], ledger[1], ADMIN, preview["confirmation"], NOW + 4)["replayed"]
    assert counts(ledger)["buyers"] == 15


def test_one_buyer_with_three_renewals_counts_once(ledger):
    clients(ledger, 1)
    for renewal in range(4):
        approve(ledger, FIRST_CLIENT, now=NOW + 10 * renewal, legacy=renewal == 0)
    assert counts(ledger) == {"total": 1, "buyers": 1, "not_purchased": 0}


@pytest.mark.parametrize("state", ["created", "review", "clarification", "rejected", "cancelled"])
def test_unconfirmed_screenshot_and_rejected_requests_are_not_purchases(ledger, state):
    clients(ledger, 1)
    with Session(ledger[0]) as db, db.begin():
        db.add(PaymentRequest(id="not-approved", user_id=FIRST_CLIENT, name="Real client",
                              state=state, created_at=NOW, updated_at=NOW,
                              receipt_file_id="SYNTHETIC-RECEIPT", receipt_kind="photo"))
    assert counts(ledger) == {"total": 1, "buyers": 0, "not_purchased": 1}


def test_expired_buyer_and_stopped_clients_without_searches_remain_in_population(ledger):
    clients(ledger, 2)
    approve(ledger, FIRST_CLIENT, now=1000)
    with Session(ledger[0]) as db, db.begin():
        db.get(User, FIRST_CLIENT).ready = False
        db.get(User, FIRST_CLIENT + 1).ready = False
        assert db.get(Entitlement, FIRST_CLIENT).expires_at < time.time()
        assert db.scalar(select(Search.id)) is None
    assert counts(ledger) == {"total": 2, "buyers": 1, "not_purchased": 1}


def test_gift_active_entitlement_and_stars_test_do_not_count_as_purchases(ledger):
    clients(ledger, 3)
    with Session(ledger[0]) as db, db.begin():
        db.add_all([
            Entitlement(user_id=FIRST_CLIENT, expires_at=NOW + 10**8, updated_at=NOW),
            AccessEvent(id="gift", user_id=FIRST_CLIENT, actor=ADMIN, kind="gift", at=NOW,
                        expires_at=NOW + 10**8, reason="Owner gift"),
            Entitlement(user_id=FIRST_CLIENT + 1, expires_at=NOW + 10**8, updated_at=NOW),
            StarsTestOrder(id="synthetic-test", user_id=FIRST_CLIENT + 2, command_update=1,
                           created_at=NOW, state="paid", paid_until=NOW + 10**8,
                           charge_id="SYNTHETIC-TEST-CHARGE"),
            SubscriptionPreview(user_id=FIRST_CLIENT + 2, updated_at=NOW,
                                state={"approved": True, "paid": True}),
        ])
    assert counts(ledger) == {"total": 3, "buyers": 0, "not_purchased": 3}


def test_no_clients_renders_zero_over_zero(ledger):
    assert counts(ledger) == {"total": 0, "buyers": 0, "not_purchased": 0}
    assert "✅ Купили тариф: 0/0\n⏳ Ще не купили: 0" in text(ledger)


def test_configured_admin_is_excluded_but_name_and_notification_test_are_not(ledger):
    clients(ledger, 1)
    approve(ledger, ADMIN)
    approve(ledger, FIRST_CLIENT)
    with Session(ledger[0]) as db, db.begin():
        db.add(TelegramTest(user_id=FIRST_CLIENT, state="sent", attempted_at=NOW))
    assert counts(ledger) == {"total": 1, "buyers": 1, "not_purchased": 0}


def test_historical_approved_request_counts_but_orphan_access_event_does_not(ledger):
    clients(ledger, 2)
    with Session(ledger[0]) as db, db.begin():
        db.add(PaymentRequest(id="historical", user_id=FIRST_CLIENT, name="Historical buyer",
                              state="approved", created_at=NOW, updated_at=NOW))
        db.add(AccessEvent(id="manual:orphan", user_id=FIRST_CLIENT + 1, actor=ADMIN,
                           kind="manual_paid", at=NOW, expires_at=NOW + 86400,
                           reason="Unlinked event"))
    assert counts(ledger) == {"total": 2, "buyers": 1, "not_purchased": 1}


@pytest.mark.parametrize("invalid", [{"amount_minor": 0}, {"amount_minor": -25000},
                                     {"currency": "XTR"}, {"days": 0}])
def test_noncommercial_approved_rows_are_not_purchases(ledger, invalid):
    clients(ledger, 1)
    with Session(ledger[0]) as db, db.begin():
        db.add(PaymentRequest(id="not-commercial", user_id=FIRST_CLIENT,
                              state="approved", created_at=NOW, updated_at=NOW, **invalid))
    assert counts(ledger) == {"total": 1, "buyers": 0, "not_purchased": 1}


def test_explicit_config_exclusions_apply_to_both_population_and_buyers(ledger):
    clients(ledger, 3)
    approve(ledger, FIRST_CLIENT)
    approve(ledger, FIRST_CLIENT + 1)
    settings = replace(ledger[1], stats_excluded_user_ids=f"{FIRST_CLIENT},{FIRST_CLIENT + 2}")
    assert counts((ledger[0], settings)) == {"total": 1, "buyers": 1, "not_purchased": 0}


def test_invalid_exclusion_configuration_does_not_silently_change_population(ledger):
    clients(ledger, 2)
    settings = replace(ledger[1], stats_excluded_user_ids="not-a-verified-id")
    with pytest.raises(ValueError):
        counts((ledger[0], settings))
    output = text((ledger[0], settings))
    assert "Не вдалося отримати статистику" in output
    assert "Купили тариф:" not in output and "Ще не купили:" not in output


def test_missing_payment_table_reports_unavailable_instead_of_unpaid(ledger):
    clients(ledger, 1)
    PaymentRequest.__table__.drop(ledger[0])
    output = text(ledger)
    assert "Не вдалося отримати статистику" in output
    assert "Купили тариф:" not in output and "Ще не купили:" not in output


def test_non_admin_is_rejected_before_any_statistics_query(ledger, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A non-admin queried private purchase statistics")
    monkeypatch.setattr(purchase_stats, "counts", forbidden)
    with Session(ledger[0]) as db:
        assert "лише адміністратору" in bot_commands.stats_text(
            db, FIRST_CLIENT, ADMIN, settings=ledger[1])


def test_webhook_verifies_admin_on_each_stats_call(ledger, monkeypatch):
    from backend import telegram_setup
    clients(ledger, 1)
    sent = []
    monkeypatch.setattr(telegram_setup, "call", lambda token, method, payload, **kw:
                        sent.append(payload) or {"ok": True, "result": {"message_id": len(sent)}})
    with TestClient(create_app(ledger[1], ledger[0])) as api:
        assert command(api, "/stats", uid=ADMIN, update=100).status_code == 200
        rejected = command(api, "/stats", uid=FIRST_CLIENT, update=101)
        assert rejected.status_code == 200
        assert command(api, "/stats", uid=ADMIN, update=102).status_code == 200
    assert len(sent) == 2
    assert all("💳 Підписки" in response["text"] for response in sent)
    assert "лише адміністратору" in rejected.json()["text"]
    assert "Купили тариф" not in rejected.json()["text"]


@pytest.mark.parametrize("case", ["unsigned", "group", "foreign-chat", "bot", "other-bot-mention"])
def test_stats_rejects_untrusted_transport_or_nonprivate_sender(ledger, monkeypatch, case):
    from backend import telegram_setup
    with TestClient(create_app(ledger[1], ledger[0])) as api:
        monkeypatch.setattr(purchase_stats, "counts", lambda *a, **kw:
                            pytest.fail("Untrusted stats request queried purchase data"))
        monkeypatch.setattr(telegram_setup, "call", lambda *a, **kw:
                            pytest.fail("Untrusted stats request attempted Telegram transport"))
        message = {"from": {"id": ADMIN}, "chat": {"id": ADMIN, "type": "private"},
                   "text": "/stats", "date": int(time.time())}
        if case == "group":
            message["chat"]["type"] = "group"
        elif case == "foreign-chat":
            message["chat"]["id"] = FIRST_CLIENT
        elif case == "bot":
            message["from"]["is_bot"] = True
        elif case == "other-bot-mention":
            message["text"] = "/stats@different_bot"
        headers = {} if case == "unsigned" else {"X-Telegram-Bot-Api-Secret-Token": SECRET}
        response = api.post("/telegram/webhook", headers=headers,
                            json={"update_id": 500, "message": message})
    assert response.status_code == (403 if case == "unsigned" else 200)
    assert "Купили тариф" not in response.text and "Ще не купили" not in response.text


def test_database_outage_returns_error_to_admin_without_reading_payments(ledger, monkeypatch):
    from backend import telegram_setup
    monkeypatch.setattr(telegram_setup, "call", lambda *a, **kw:
                        pytest.fail("DB outage reply should use the webhook response"))
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            raise OperationalError("PRIVATE-SQL", {"receipt": "PRIVATE-RECEIPT"}, None)
    with TestClient(create_app(ledger[1], ledger[0])) as api:
        event.listen(ledger[0], "before_cursor_execute", fail)
        try:
            response = command(api, "/stats", uid=ADMIN, update=100)
            outsider = command(api, "/stats", uid=FIRST_CLIENT, update=101)
        finally:
            event.remove(ledger[0], "before_cursor_execute", fail)
    assert response.status_code == 200
    assert response.json()["text"] == bot_commands.STATS_UNAVAILABLE
    assert "Купили тариф:" not in response.text and "PRIVATE" not in response.text
    assert "лише адміністратору" in outsider.json()["text"]


def test_database_error_does_not_become_zero_purchases_or_leak_sql(ledger, monkeypatch):
    def fail(*args, **kwargs):
        raise OperationalError("PRIVATE-SQL", {"receipt": "PRIVATE-RECEIPT"}, None)
    monkeypatch.setattr(purchase_stats, "counts", fail)
    output = text(ledger)
    assert "Не вдалося отримати статистику" in output
    assert "Купили тариф: 0" not in output and "Ще не купили: 0" not in output
    assert "PRIVATE" not in output


def test_query_count_is_bounded_and_read_only(ledger):
    clients(ledger, 165)
    for i in range(14):
        approve(ledger, FIRST_CLIENT + i)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(ledger[0], "before_cursor_execute", capture)
    try:
        assert counts(ledger)["buyers"] == 14
    finally:
        event.remove(ledger[0], "before_cursor_execute", capture)
    assert 1 <= len(statements) <= 4
    assert set(statements) <= {"SELECT", "WITH"}


def test_deployment_snapshot_contains_only_real_aggregates_and_rendered_block(ledger, caplog):
    clients(ledger, 3)
    approve(ledger, FIRST_CLIENT)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        purchase_stats.log_snapshot(*ledger)
    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith("Admin purchase stats "))
    snapshot = json.loads(line.removeprefix("Admin purchase stats "))
    assert {key: snapshot[key] for key in ("total", "buyers", "not_purchased")} == {
        "total": 3, "buyers": 1, "not_purchased": 2}
    assert snapshot["checked_at"] and "✅ Купили тариф: 1/3" in snapshot["text"]
    assert "SYNTHETIC" not in line and "FIXTURE" not in line and str(FIRST_CLIENT) not in line


def test_snapshot_database_failure_logs_no_receipts_or_false_counts(ledger, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise OperationalError("PRIVATE-SQL", {"receipt": "PRIVATE-RECEIPT"}, None)
    monkeypatch.setattr(purchase_stats, "counts", fail)
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        purchase_stats.log_snapshot(*ledger)
    assert "Admin purchase stats unavailable" in caplog.text
    assert "PRIVATE" not in caplog.text and '"buyers"' not in caplog.text
