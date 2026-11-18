"""Offline gifts, signed confirmation retries and unchanged financial history."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from backend import billing, radar_catalog, radar_service, subscription_grants as gifts
from backend.app import Settings
from backend.billing_models import AccessEvent, BillingControl, BillingNotice, BillingOrder, Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import Base, Delivery, StarsTestOrder, User
from backend.radar_migration import migrate
from backend.radar_models import Radar


@pytest.fixture
def grant(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "900")
    monkeypatch.setattr(radar_catalog, "SEED_PATH", tmp_path / "no-catalog-seed.json")
    engine = create_engine("sqlite:///" + str(tmp_path / "grants.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    ManualBase.metadata.create_all(engine)
    settings = Settings(str(engine.url), "fake-token", "s" * 32, admin_telegram_id=900)
    with Session(engine) as db, db.begin():
        db.add_all([User(id=111, ready=True), User(id=222, ready=False), User(id=900, ready=True)])
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=True, offer={}))
    yield SimpleNamespace(engine=engine, settings=settings, now=1800000000.0)
    engine.dispose()


def preview(grant, **changes):
    args = {"telegram_id": 111, "days": 30, "reason": "Подарунок від адміністратора", "now": grant.now}
    args.update(changes)
    return gifts.preview(grant.engine, grant.settings, 900, **args)


def confirm(grant, data, **changes):
    args = {"now": grant.now + 1}
    args.update(changes)
    return gifts.confirm(grant.engine, grant.settings, 900, data["confirmation_token"], **args)


def counts(db):
    return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (BillingOrder, PaymentRequest, BillingNotice, Delivery, AccessEvent, Entitlement)}


def test_preview_is_read_only_and_grant_works_while_sales_are_closed(grant):
    with Session(grant.engine) as db:
        before = counts(db)
        control_version = db.get(BillingControl, billing.CONTROL).version
    data = preview(grant)
    assert data["current_expires_at"] == 0 and data["proposed_expires_at"] == grant.now + 30 * gifts.DAY
    assert data["valid_until"] == grant.now + 300 and data["user_ready"] is True
    assert data["radar_resume_required"] is True
    with Session(grant.engine) as db:
        assert counts(db) == before
        assert db.get(BillingControl, billing.CONTROL).version == control_version
    applied = confirm(grant, data)
    assert applied["expires_at"] == grant.now + 1 + 30 * gifts.DAY
    assert applied["replayed"] is False and applied["current_expires_at"] == 0
    with Session(grant.engine) as db:
        after = counts(db)
        assert after == {**before, "billing_access_events": 1, "billing_entitlements": 1}
        row = db.get(AccessEvent, gifts.PREFIX + data["action_id"])
        assert (row.user_id, row.actor, row.kind, row.reason) == (111, 900, "gift", data["reason"])
        assert row.expires_at == db.get(Entitlement, 111).expires_at == applied["expires_at"]
        assert db.get(User, 111).ready is True
        assert db.get(BillingControl, billing.CONTROL).sales is False
        assert db.get(BillingControl, billing.CONTROL).enforce is True


@pytest.mark.parametrize("days", [1, 30, 3650])
def test_duration_extends_existing_access_instead_of_shortening_it(grant, days):
    existing = grant.now + 14 * gifts.DAY
    with Session(grant.engine) as db, db.begin():
        db.add(Entitlement(user_id=111, expires_at=existing, updated_at=grant.now))
    data = preview(grant, days=days)
    result = confirm(grant, data)
    assert result["expires_at"] == existing + days * gifts.DAY


def test_paid_pilot_expiry_is_preserved_when_gift_extends_it(grant):
    before = grant.now + 45 * gifts.DAY
    with Session(grant.engine) as db, db.begin():
        db.add(StarsTestOrder(id="gift-test-stars", user_id=111, command_update=45,
            created_at=grant.now, state="paid", paid_until=before))
    data = preview(grant)
    assert data["current_expires_at"] == before
    assert confirm(grant, data)["expires_at"] == before + 30 * gifts.DAY
    with Session(grant.engine) as db:
        assert db.get(StarsTestOrder, "gift-test-stars").paid_until == before


def test_retry_after_confirmation_expiry_returns_same_audit_and_does_not_extend(grant):
    data = preview(grant)
    first = confirm(grant, data)
    for now in (grant.now + 2, grant.now + 300, grant.now + 100 * gifts.DAY):
        retried = confirm(grant, data, now=now)
        assert retried == {**first, "replayed": True}
    with Session(grant.engine) as db:
        assert db.scalar(select(func.count()).select_from(AccessEvent)) == 1
        assert db.get(Entitlement, 111).expires_at == first["expires_at"]


@pytest.mark.parametrize("now_offset", [-1, 300, 301])
def test_unapplied_token_outside_its_window_is_rejected_without_grant(grant, now_offset):
    data = preview(grant)
    with pytest.raises(gifts.GrantError) as failure:
        confirm(grant, data, now=grant.now + now_offset)
    assert failure.value.code == "confirmation_expired"
    with Session(grant.engine) as db:
        assert db.get(Entitlement, 111) is None
        assert db.scalar(select(func.count()).select_from(AccessEvent)) == 0


def test_access_change_between_preview_and_confirmation_requires_fresh_preview(grant):
    data = preview(grant)
    with Session(grant.engine) as db, db.begin():
        db.add(Entitlement(user_id=111, expires_at=grant.now + gifts.DAY, updated_at=grant.now + .5))
    with pytest.raises(gifts.GrantError) as failure:
        confirm(grant, data)
    assert failure.value.status == 409 and failure.value.code == "access_changed"
    with Session(grant.engine) as db:
        assert db.get(Entitlement, 111).expires_at == grant.now + gifts.DAY
        assert db.scalar(select(func.count()).select_from(AccessEvent)) == 0


@pytest.mark.parametrize("field,value,code", [
    ("telegram_id", True, "invalid_telegram_id"), ("telegram_id", 0, "invalid_telegram_id"),
    ("telegram_id", -1, "invalid_telegram_id"), ("telegram_id", 2**52, "invalid_telegram_id"),
    ("days", True, "invalid_days"), ("days", 1.5, "invalid_days"), ("days", 0, "invalid_days"),
    ("days", 3651, "invalid_days"), ("reason", " ", "invalid_reason"),
    ("reason", "x" * 501, "invalid_reason"), ("reason", "x\x00y", "invalid_reason"),
])
def test_backend_validates_grants(grant, field, value, code):
    with pytest.raises(gifts.GrantError) as failure:
        preview(grant, **{field: value})
    assert failure.value.code == code


def test_unknown_target_never_creates_a_phantom_account(grant):
    with pytest.raises(gifts.GrantError) as failure:
        preview(grant, telegram_id=333)
    assert failure.value.status == 404
    with Session(grant.engine) as db:
        assert db.get(User, 333) is None and db.get(Entitlement, 333) is None


def test_gift_does_not_mark_an_unstarted_user_ready(grant):
    data = preview(grant, telegram_id=222)
    assert data["user_ready"] is False
    confirm(grant, data)
    with Session(grant.engine) as db:
        assert db.get(User, 222).ready is False


@pytest.mark.parametrize("actor", [111, 222, True])
def test_only_verified_administrator_can_preview_confirm_or_read_history(grant, actor):
    token = preview(grant)["confirmation_token"]
    operations = [lambda: gifts.preview(grant.engine, grant.settings, actor, 111, 30, "Gift", grant.now),
                  lambda: gifts.confirm(grant.engine, grant.settings, actor, token, grant.now),
                  lambda: gifts.history(grant.engine, grant.settings, actor)]
    for operation in operations:
        with pytest.raises(gifts.GrantError) as failure:
            operation()
        assert failure.value.status == 403


def test_admin_configuration_requires_both_matching_ids(grant, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "999")
    with pytest.raises(gifts.GrantError) as failure:
        preview(grant)
    assert failure.value.code == "admin_configuration_unavailable"


@pytest.mark.parametrize("change", ["signature", "payload", "wrong_key", "garbage"])
def test_tampered_confirmation_cannot_grant_access(grant, change):
    data = preview(grant)
    token = data["confirmation_token"]
    settings = grant.settings
    if change == "signature": token = token[:-1] + ("a" if token[-1] != "a" else "b")
    elif change == "payload": token = ("a" if token[0] != "a" else "b") + token[1:]
    elif change == "wrong_key": settings = replace(settings, webhook_secret="z" * 32)
    else: token = "not-a-confirmation"
    with pytest.raises(gifts.GrantError) as failure:
        gifts.confirm(grant.engine, settings, 900, token, grant.now)
    assert failure.value.code == "invalid_confirmation"
    with Session(grant.engine) as db:
        assert db.get(Entitlement, 111) is None


def test_audit_insert_failure_rolls_back_entitlement_and_all_changes(grant):
    data = preview(grant)
    def reject_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO billing_access_events"):
            raise RuntimeError("synthetic audit failure")
    event.listen(grant.engine, "before_cursor_execute", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit"):
            confirm(grant, data)
    finally:
        event.remove(grant.engine, "before_cursor_execute", reject_audit)
    with Session(grant.engine) as db:
        assert db.get(Entitlement, 111) is None
        assert db.scalar(select(func.count()).select_from(AccessEvent)) == 0
        assert db.get(BillingControl, billing.CONTROL).version == 0


def radar_setup(grant, active_until):
    migrate(grant.engine)
    with Session(grant.engine) as db, db.begin():
        radar_catalog.import_catalog(db, {"brands": [{"id": 9, "name": "BMW", "models": []}],
                                         "regions": [{"id": 10, "name": "Київська"}]})
        payload = radar_service.RadarCreate(name="Збережений радар", brands=[{"brand_id": 9, "model_ids": []}],
            price_category=3, region_ids=[], year_from=2000, year_to=2026, min_profit=10)
        created = radar_service.create(db, 111, payload)
        row = db.get(Radar, created["id"])
        row.status, row.active_until = "active", active_until
        db.add(Entitlement(user_id=111, expires_at=active_until, updated_at=grant.now - 100))
        return row.id, row.revision


@pytest.mark.parametrize("expired", [True, False])
def test_grant_preserves_lapse_pause_and_extends_only_uninterrupted_radar(grant, expired):
    before = grant.now - 1 if expired else grant.now + 1000
    rid, revision = radar_setup(grant, before)
    data = preview(grant)
    result = confirm(grant, data)
    with Session(grant.engine) as db:
        row = db.get(Radar, rid)
        assert row.name == "Збережений радар" and row.slot == 1 and row.price_category == 3
        if expired:
            assert row.status == "subscription_expired" and row.active_until == 0
            assert row.revision == revision + 1
        else:
            assert row.status == "active" and row.active_until == result["expires_at"]
            assert row.revision == revision
        assert db.scalar(select(func.count()).select_from(Radar)) == 1


def test_manually_paused_radar_remains_paused_after_gift(grant):
    rid, _ = radar_setup(grant, grant.now + 1000)
    with Session(grant.engine) as db, db.begin():
        row = db.get(Radar, rid)
        row.status, row.active_until = "paused", 0
    confirm(grant, preview(grant))
    with Session(grant.engine) as db:
        assert db.get(Radar, rid).status == "paused" and db.get(Radar, rid).active_until == 0


def test_gifted_user_can_manually_resume_without_a_fabricated_payment(grant):
    from backend import paid_source_access
    from backend.monitor import active_members
    rid, _ = radar_setup(grant, grant.now - 1)
    confirm(grant, preview(grant))
    with Session(grant.engine) as db, db.begin():
        row = db.get(Radar, rid)
        assert row.status == "subscription_expired"
        assert paid_source_access.allowed(db, 111, grant.now + 2)
        assert db.scalar(select(func.count()).select_from(PaymentRequest)) == 0
        restored = radar_service.set_status(db, 111, rid,
            radar_service.RadarStatus(active=True, revision=row.revision))
        assert restored["status"] == "active"
    with Session(grant.engine) as db:
        assert len(active_members(db)) == 1
        assert db.get(Radar, rid).slot == 1
        assert db.scalar(select(func.count()).select_from(AccessEvent)) == 1


def test_history_is_bounded_and_filtered_without_exposing_payment_records(grant):
    for index, uid in enumerate((111, 222, 111)):
        grant.now += 2
        confirm(grant, preview(grant, telegram_id=uid, reason="Подарунок " + str(index)))
    result = gifts.history(grant.engine, grant.settings, 900, telegram_id=111, limit=1)
    assert result["limit"] == 1 and len(result["items"]) == 1
    assert result["items"][0]["reason"] == "Подарунок 2"
    assert set(result["items"][0]) == {"action_id", "telegram_id", "actor", "at", "expires_at", "reason"}
    with pytest.raises(gifts.GrantError):
        gifts.history(grant.engine, grant.settings, 900, limit=51)


def api(grant):
    application = FastAPI()
    def identity(x_test_uid: int = Header(default=900)):
        return x_test_uid
    gifts.install(application, grant.engine, grant.settings, identity)
    return TestClient(application)


def test_http_contract_confirms_and_retries_without_fake_money_rows(grant):
    with api(grant) as client:
        response = client.post("/api/admin/subscription-grants/preview",
            json={"telegram_id": 111, "days": 30, "reason": "Тестовий подарунок"})
        assert response.status_code == 200
        token = response.json()["confirmation_token"]
        body = {"confirmation_token": token}
        first = client.post("/api/admin/subscription-grants/confirm", json=body)
        retry = client.post("/api/admin/subscription-grants/confirm", json=body)
        assert first.status_code == retry.status_code == 200
        assert retry.json() == {**first.json(), "replayed": True}
        assert len(client.get("/api/admin/subscription-grants?telegram_id=111&limit=50").json()["items"]) == 1
        denied = client.get("/api/admin/subscription-grants", headers={"x-test-uid": "111"})
        assert denied.status_code == 403 and denied.json()["code"] == "admin_only"


@pytest.mark.parametrize("changes", [{"days": True}, {"days": "30"}, {"telegram_id": True},
    {"actor": 900}, {"expires_at": 9999999999}, {"reason": ""}])
def test_http_rejects_coerced_numbers_and_unexpected_fields(grant, changes):
    with api(grant) as client:
        response = client.post("/api/admin/subscription-grants/preview", json={
            "telegram_id": 111, "days": 30, "reason": "Gift", **changes})
        assert response.status_code == 422


def test_http_storage_failure_returns_safe_retry_instruction(grant, monkeypatch):
    from sqlalchemy.exc import OperationalError
    def broken(*args, **kwargs):
        raise OperationalError("private SQL", {"secret": "do-not-echo"}, Exception())
    monkeypatch.setattr(gifts, "confirm", broken)
    with api(grant) as client:
        response = client.post("/api/admin/subscription-grants/confirm", json={"confirmation_token": "placeholder"})
        assert response.status_code == 503 and response.json()["code"] == "grant_storage_unavailable"
        assert "secret" not in response.text and "private SQL" not in response.text
