"""Synthetic owner receipt delivery; no external sends or real account details."""
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from backend import billing, manual_checkout as checkout
from backend import manual_repeat_preflight as preflight
from backend.billing_models import Entitlement
from backend.manual_payment_models import BankCredit, PaymentRequest, PaymentNotice
from backend.models import SourceProbe
from backend.tests.test_manual_checkout import bank
from backend.tests.test_manual_payments import review, ADMIN, UID, NOW, count


@pytest.fixture
def prepared(bank, monkeypatch):
    engine, settings, _ = bank
    monkeypatch.setenv(preflight.ENV, preflight.CAMPAIGN)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
    row = checkout.create(engine, settings, ADMIN, NOW, checkout.TERMS_VERSION)
    return bank, row


def test_exact_marker_required_and_no_implicit_send(prepared, monkeypatch):
    bank, row = prepared
    for value in (None, "true", "manual-card-launch-20261002-v1"):
        if value is None:
            monkeypatch.delenv(preflight.ENV, raising=False)
        else:
            monkeypatch.setenv(preflight.ENV, value)
        assert preflight.run_once(*bank[:2], lambda *a, **k: pytest.fail("must not send"), NOW) == "disabled"
    assert count(bank[0], SourceProbe) == 0


@pytest.mark.parametrize("request_state", ["created", "clarification", "review"])
def test_owner_only_exact_requisites_once_and_no_payment_mutation(prepared, monkeypatch, request_state):
    bank, row = prepared
    engine, settings, _ = bank
    with Session(engine) as db, db.begin():
        db.get(PaymentRequest, row["code"]).state = request_state
    expected = checkout.details(engine, settings, ADMIN, row["code"])
    calls = []
    def accepted(token, method, payload, **kwargs):
        calls.append((method, payload, kwargs))
        with Session(engine) as db:
            assert db.get(SourceProbe, preflight.PROBE_ID).status == "sending"
        return {"ok": True, "result": {"message_id": 42}}
    assert preflight.run_once(engine, settings, accepted, NOW) == "accepted"
    assert preflight.run_once(engine, settings, accepted, NOW+1) == "accepted"
    assert len(calls) == 1
    method, payload, kwargs = calls[0]
    assert method == "sendMessage" and payload["chat_id"] == ADMIN
    assert kwargs == {"timeout": 5} and payload["allow_paid_broadcast"] is False
    assert payload["reply_markup"] == expected["reply_markup"]
    assert payload["text"].endswith(expected["text"])
    assert payload["protect_content"] is True
    assert "method" not in payload
    copies = [b["copy_text"]["text"] for r in payload["reply_markup"]["inline_keyboard"] for b in r if "copy_text" in b]
    assert "4242424242424242" in copies and any(v.startswith("UA") for v in copies)
    monkeypatch.delenv(preflight.ENV)
    with Session(engine) as db:
        assert preflight.ready(db)
        assert db.get(PaymentRequest, row["code"]).state == request_state
        assert not billing.expiry(db, ADMIN)
        stored = db.get(SourceProbe, preflight.PROBE_ID).result
        assert set(stored) == {"marker", "campaign", "release", "code"}
    assert count(engine, PaymentRequest) == 1
    assert count(engine, Entitlement) == count(engine, BankCredit) == count(engine, PaymentNotice) == 0


@pytest.mark.parametrize("response", [None, {}, {"ok": True}, {"ok": True, "result": {"message_id": True}},
    {"ok": False, "error_code": 500}])
def test_uncertain_never_retried(prepared, response):
    bank, _ = prepared
    calls = []
    def send(*args, **kwargs):
        calls.append(1)
        return response
    assert preflight.run_once(*bank[:2], send, NOW) == "uncertain"
    assert preflight.run_once(*bank[:2], send, NOW+120) == "uncertain"
    assert len(calls) == 1
    with Session(bank[0]) as db:
        assert not preflight.ready(db)


def test_transport_exception_never_retried(prepared):
    bank, _ = prepared
    def fail(*args, **kwargs):
        raise TimeoutError("synthetic")
    assert preflight.run_once(*bank[:2], fail, NOW) == "uncertain"
    assert preflight.run_once(*bank[:2], lambda *a, **k: pytest.fail("must not retry"), NOW+60) == "uncertain"


def test_definite_rejection_never_retried(prepared):
    bank, _ = prepared
    assert preflight.run_once(*bank[:2], lambda *a, **k: {"ok": False, "error_code": 403}, NOW) == "failed"
    assert preflight.run_once(*bank[:2], lambda *a, **k: pytest.fail("must not retry"), NOW+60) == "failed"


@pytest.mark.parametrize("owner_state", [None, "approved", "rejected"])
def test_no_open_owner_request_sends_pure_preview_without_creating_one(bank, monkeypatch, owner_state):
    engine, settings, _ = bank
    monkeypatch.setenv(preflight.ENV, preflight.CAMPAIGN)
    checkout.create(engine, settings, UID, NOW, checkout.TERMS_VERSION)
    if owner_state:
        row = checkout.create(engine, settings, ADMIN, NOW, checkout.TERMS_VERSION)
        with Session(engine) as db, db.begin():
            db.get(PaymentRequest, row["code"]).state = owner_state
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
    count_before = count(engine, PaymentRequest)
    calls = []
    def accepted(token, method, payload, **kwargs):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}
    assert preflight.run_once(engine, settings, accepted, NOW) == "accepted"
    assert preflight.run_once(engine, settings, accepted, NOW+1) == "accepted"
    assert len(calls) == 1
    method, payload = calls[0]
    assert method == "sendMessage" and payload["chat_id"] == ADMIN
    expected = checkout.render_requisites(ADMIN, checkout.subscription_preview.receiving_profile())
    assert payload["reply_markup"] == expected["reply_markup"]
    assert payload["text"].endswith(expected["text"])
    buttons = [b for r in payload["reply_markup"]["inline_keyboard"] for b in r]
    assert [b["callback_data"] for b in buttons if "callback_data" in b] == [checkout.PREFIX+"view"]
    assert "4242424242424242" in [b["copy_text"]["text"] for b in buttons if "copy_text" in b]
    assert count(engine, PaymentRequest) == count_before
    assert count(engine, Entitlement) == count(engine, BankCredit) == count(engine, PaymentNotice) == 0
    with Session(engine) as db:
        assert preflight.ready(db)
        assert not billing.expiry(db, ADMIN)


@pytest.mark.parametrize("guard", ["sales", "enforce", "profile"])
def test_unready_checkout_blocks(prepared, monkeypatch, guard):
    bank, row = prepared
    engine, settings, _ = bank
    if guard == "profile":
        monkeypatch.delenv(checkout.subscription_preview.RECIPIENT_ENV)
    else:
        with Session(engine) as db, db.begin():
            setattr(billing.control(db), guard, False)
    assert preflight.run_once(engine, settings, lambda *a, **k: pytest.fail("must not send"), NOW) == "blocked"


def test_unverified_owner_never_sent(prepared):
    bank, _ = prepared
    settings = replace(bank[1], admin_telegram_id=UID)
    assert preflight.run_once(bank[0], settings, lambda *a, **k: pytest.fail("must not send"), NOW) == "blocked"
    assert count(bank[0], SourceProbe) == 0


def test_interrupted_claim_becomes_uncertain_without_retry(prepared):
    bank, _ = prepared
    with Session(bank[0]) as db, db.begin():
        db.add(SourceProbe(id=preflight.PROBE_ID, status="sending", checked_at=NOW,
                           requests=1, result=preflight.proof("claimed")))
    assert preflight.run_once(*bank[:2], lambda *a, **k: pytest.fail("must not retry"), NOW+61) == "uncertain"


def test_ready_requires_exact_campaign_and_marker(prepared):
    bank, _ = prepared
    with Session(bank[0]) as db, db.begin():
        row = SourceProbe(id=preflight.PROBE_ID, status="accepted", checked_at=NOW, requests=1,
                          result={"marker": preflight.MARKER, "campaign": "other"})
        db.add(row)
        db.flush()
        assert not preflight.ready(db)
        row.result = {"marker": "other", "campaign": preflight.CAMPAIGN}
        assert not preflight.ready(db)
        row.result = preflight.proof("accepted")
        assert preflight.ready(db)


@pytest.mark.parametrize("reason", ["owner_request_missing", "owner_request_not_open"])
def test_only_unattempted_owner_order_block_can_resume(bank, monkeypatch, reason):
    engine, settings, _ = bank
    monkeypatch.setenv(preflight.ENV, preflight.CAMPAIGN)
    with Session(engine) as db, db.begin():
        billing.control(db).enforce = True
        db.add(SourceProbe(id=preflight.PROBE_ID, status="blocked", checked_at=NOW,
                           requests=0, result=preflight.proof(reason)))
    calls = []
    def accepted(*args, **kwargs):
        calls.append(1)
        return {"ok": True, "result": {"message_id": 42}}
    assert preflight.run_once(engine, settings, accepted, NOW+1) == "accepted"
    assert preflight.run_once(engine, settings, accepted, NOW+2) == "accepted"
    assert len(calls) == 1 and count(engine, PaymentRequest) == 0


@pytest.mark.parametrize("status,requests,reason", [
    ("blocked", 1, "owner_request_missing"),
    ("blocked", 0, "recipient_configuration_unavailable"),
    ("accepted", 1, "accepted"),
    ("failed", 1, "owner_request_missing"),
    ("uncertain", 1, "owner_request_missing"),
])
def test_unrelated_or_attempted_probe_is_never_rearmed(prepared, status, requests, reason):
    bank, _ = prepared
    with Session(bank[0]) as db, db.begin():
        db.add(SourceProbe(id=preflight.PROBE_ID, status=status, checked_at=NOW,
                           requests=requests, result=preflight.proof(reason)))
    assert preflight.run_once(*bank[:2], lambda *a, **k: pytest.fail("must not send"), NOW+1) == status
