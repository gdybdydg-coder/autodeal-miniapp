import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend import stars_test
from backend.app import Settings, create_app
from backend.models import Base, StarsTestOrder, User, Search, SourceBudget, SourceProbe
from backend.tests.test_backend import TOKEN, SECRET, command, subscribe


@pytest.fixture
def setup(tmp_path):
    engine = create_engine("sqlite:///"+str(tmp_path/"stars.db"), connect_args={"check_same_thread": False})
    settings = Settings("unused", TOKEN, SECRET, True, True)
    with TestClient(create_app(settings, engine)) as api:
        yield engine, settings, api
    engine.dispose()


def invoice(api, update=10):
    return command(api, "/paytest_confirm", uid=stars_test.OWNER, update=update).json()


def paid(payload, charge="charge-1"):
    return {"invoice_payload": payload, "currency": "XTR", "total_amount": 1,
            "telegram_payment_charge_id": charge}


def post(api, payment=None, *, checkout=None, uid=stars_test.OWNER, refund=None, update=20):
    event = {"update_id": update}
    if checkout is not None:
        event["pre_checkout_query"] = {"id": "checkout-1", "from": {"id": uid}, **checkout}
    else:
        event["message"] = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"},
                            "date": int(time.time())}
        event["message"]["refunded_payment" if refund is not None else "successful_payment"] = refund or payment
    return api.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, json=event)


def test_private_terms_invoice_and_duplicate_command(setup):
    engine, settings, api = setup
    terms = command(api, "/paytest", uid=stars_test.OWNER).json()
    assert "реальне списання" in terms["text"] and "/paytest_confirm" in terms["text"]
    inv = invoice(api)
    assert inv["method"] == "sendInvoice" and inv["currency"] == "XTR"
    assert inv["prices"] == [{"label": "Тестовий доступ", "amount": 1}]
    assert inv["start_parameter"] and "subscription_period" not in inv
    assert invoice(api) == {"ok": True}
    with Session(engine) as db:
        assert len(db.scalars(select(StarsTestOrder)).all()) == 1
        assert db.scalar(select(StarsTestOrder)).paid_until is None


@pytest.mark.parametrize("cmd", sorted(stars_test.COMMANDS))
def test_non_owner_cannot_see_or_use_payment(setup, cmd):
    engine, settings, api = setup
    assert command(api, cmd, uid=123).json() == {"ok": True}
    with Session(engine) as db:
        assert not db.scalars(select(StarsTestOrder)).all()


def test_private_chat_and_webhook_authentication(setup):
    engine, settings, api = setup
    event = {"update_id": 1, "message": {"from": {"id": stars_test.OWNER},
             "chat": {"id": -1, "type": "group"}, "text": "/paytest_confirm", "date": int(time.time())}}
    assert api.post("/telegram/webhook", json=event).status_code == 403
    assert api.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, json=event).json() == {"ok": True}


@pytest.mark.parametrize("field,value", [("currency", "UAH"), ("total_amount", 2),
    ("total_amount", True), ("invoice_payload", "fake")])
def test_tampered_checkout_and_payment(setup, field, value):
    engine, settings, api = setup
    data = paid(invoice(api)["payload"])
    data[field] = value
    assert post(api, checkout=data).json()["ok"] is False
    assert post(api, data).json() == {"ok": True}
    with Session(engine) as db:
        assert db.scalar(select(StarsTestOrder)).state == "pending"


def test_forwarded_invoice_other_payer_denied(setup):
    engine, settings, api = setup
    data = paid(invoice(api)["payload"])
    assert post(api, checkout=data, uid=123).json()["ok"] is False
    assert post(api, data, uid=123).json() == {"ok": True}


def test_expired_checkout_but_delayed_paid_event_recorded(setup):
    engine, settings, api = setup
    data = paid(invoice(api)["payload"])
    with Session(engine) as db:
        row = db.scalar(select(StarsTestOrder)); row.created_at -= 1000; db.commit()
    assert post(api, checkout=data).json()["ok"] is False
    assert "Оплату 1 ⭐ підтверджено" in post(api, data).json()["text"]


def test_paid_status_restart_dedupe_and_no_search_mutations(setup):
    engine, settings, api = setup
    command(api, "/start", uid=stars_test.OWNER, update=1)
    sid = subscribe(api, uid=stars_test.OWNER).json()["id"]
    command(api, "/stop", uid=stars_test.OWNER, update=2)
    with Session(engine) as db:
        before_budget = [(r.id, r.total, r.calls) for r in db.scalars(select(SourceBudget))]
        before_user = (db.get(User, stars_test.OWNER).last_update, db.get(User, stars_test.OWNER).ready)
    data = paid(invoice(api)["payload"])
    assert post(api, checkout=data).json()["ok"] is True
    with Session(engine) as db:
        assert db.scalar(select(StarsTestOrder)).state == "pending"
    assert "Оплату 1 ⭐ підтверджено" in post(api, data).json()["text"]
    assert post(api, data, update=21).json() == {"ok": True}
    # A new handler/session sees the persisted order, not an in-memory cache.
    assert stars_test.handle(engine, settings, {"message": {"from": {"id": stars_test.OWNER},
        "chat": {"id": stars_test.OWNER, "type": "private"}, "text": "/payment"}})["text"].startswith("✅ Активний")
    assert "уже оплачений" in invoice(api, update=11)["text"]
    with Session(engine) as db:
        assert db.get(Search, sid).enabled is False
        assert (db.get(User, stars_test.OWNER).last_update, db.get(User, stars_test.OWNER).ready) == before_user
        assert [(r.id, r.total, r.calls) for r in db.scalars(select(SourceBudget))] == before_budget


@pytest.mark.parametrize("response,state", [({"ok": True, "result": True}, "refunded"),
                                          ({"uncertain": True}, "refund_uncertain")])
def test_refund_claim_and_no_retry(setup, monkeypatch, response, state):
    engine, settings, api = setup
    data = paid(invoice(api)["payload"])
    post(api, data)
    calls = []
    def request(token, method, payload, **kw):
        assert method == "refundStarPayment" and payload["user_id"] == stars_test.OWNER
        calls.append(payload)
        return response
    monkeypatch.setattr(stars_test.telegram_setup, "call", request)
    command(api, "/refundtest", uid=stars_test.OWNER, update=30)
    command(api, "/refundtest", uid=stars_test.OWNER, update=30)
    assert len(calls) == 1
    with Session(engine) as db:
        assert db.scalar(select(StarsTestOrder)).state == state
    post(api, refund=data)
    with Session(engine) as db:
        assert db.scalar(select(StarsTestOrder)).state == "refunded"
    assert post(api, data).json() == {"ok": True}


def test_reused_charge_and_stale_invoice_command(setup):
    engine, settings, api = setup
    first = paid(invoice(api)["payload"])
    second = paid(invoice(api, update=11)["payload"])
    post(api, first)
    assert post(api, second).json() == {"ok": True}
    with Session(engine) as db:
        assert len(db.scalars(select(StarsTestOrder).where(StarsTestOrder.state == "paid")).all()) == 1
    assert command(api, "/paytest_confirm", uid=stars_test.OWNER, date=int(time.time())-400).json() == {"ok": True}


def test_menu_is_private_and_installed_once(setup):
    engine, settings, api = setup
    from dataclasses import replace
    calls = []
    with Session(engine) as db:
        db.merge(SourceProbe(id="telegram-webhook-v1", status="configured", checked_at=0, requests=0, result={}))
        db.commit()
    def request(token, method, payload):
        assert method == "setMyCommands"
        assert payload["scope"] == {"type": "chat", "chat_id": stars_test.OWNER}
        assert any(c["command"] == "paytest" for c in payload["commands"])
        calls.append(payload)
        return {"ok": True, "result": True}
    stars_test.configure(engine, replace(settings, configure_webhook=True), request)
    stars_test.configure(engine, replace(settings, configure_webhook=True), request)
    assert len(calls) == 1
