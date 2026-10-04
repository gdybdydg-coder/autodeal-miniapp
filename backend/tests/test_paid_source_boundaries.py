"""Isolated end-to-end paid activation and actual HTTP-boundary regression tests."""
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from backend import api_attempt_audit, billing, manual_payments, paid_source_access
from backend.billing_models import BillingControl, Entitlement
from backend.manual_payment_models import PaymentRequest
from backend.models import Delivery, DeliveryTiming, Listing, MonitorMembership, MonitorWatch, Search, User
from backend.monitor import active_members
from backend.worker import TelegramSender, deliver_one, enqueue
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import strict, approve
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_api_attempt_accounting import source, opener
from backend.tests.test_backend import setup, headers
from backend.tests.test_ria_search import fixture_fetch


def owner_settings(p, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "987654321")
    settings = replace(p.settings, admin_telegram_id=987654321, manual_payment_review_enabled=True)
    with Session(p.engine) as db:
        if db.get(BillingControl, billing.CONTROL) is None:
            db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=True, offer={}))
        db.commit()
    return settings


def confirm(p, settings, code, *, legacy=False):
    with Session(p.engine) as db:
        revision = db.get(PaymentRequest, code).revision
    bank = {"account": "fixture-only", "operation": code, "actual_amount_minor": 25000} if legacy else {}
    preview = manual_payments.preview(p.engine, settings, settings.admin_telegram_id,
        code, revision, p.clock[0], bank_verified=True, **bank)
    return manual_payments.confirm(p.engine, settings, settings.admin_telegram_id,
        preview["confirmation"], p.clock[0])


@pytest.mark.parametrize("legacy", [False, True])
def test_first_owner_confirmation_rebases_saved_search_without_restart_or_old_backlog(p, monkeypatch, legacy):
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    quotes = enable(p, monkeypatch)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        original = dict(db.get(Search, 1).filters)
    p.clock[0] += 1800
    p.ads["124"] = p.clock[0] - 60  # Appeared while unpaid; never flood on approval.
    drain(p)
    assert not p.calls and not p.sent
    approved_at = int(p.clock[0])
    confirm(p, settings, "fixture-111", legacy=legacy)
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch != epoch
        assert db.get(MonitorMembership, 1).started_at == approved_at
        assert db.get(Search, 1).filters == original and db.get(Search, 1).enabled
        assert len(active_members(db)) == 1
    p.ads["125"] = p.clock[0] + 1
    wake(p); drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "125")]
    assert quotes == ["125"]


@pytest.mark.parametrize("legacy", [False, True])
def test_paid_renewal_keeps_epoch_filters_and_existing_paid_days(p, monkeypatch, legacy):
    strict(p); approve(p)
    settings = owner_settings(p, monkeypatch)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        start = db.get(MonitorMembership, 1).started_at
        before = db.get(Entitlement, 111).expires_at
        db.add(PaymentRequest(id="renewal-fixture", user_id=111, state="review", amount_minor=25000,
            currency="UAH", days=30, created_at=p.clock[0], updated_at=p.clock[0]))
        db.commit()
    result = confirm(p, settings, "renewal-fixture", legacy=legacy)
    assert result["expires_at"] == before + 30 * 86400
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(MonitorMembership, 1).started_at == start


@pytest.mark.parametrize("stopped", [True, False])
def test_first_confirmation_does_not_enable_stopped_or_disabled_search(p, monkeypatch, stopped):
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    with Session(p.engine) as db:
        if stopped:
            db.get(User, 111).ready = False
        else:
            db.get(Search, 1).enabled = False
        epoch = db.get(MonitorWatch, 1).epoch
        db.commit()
    confirm(p, settings, "fixture-111")
    drain(p)
    assert not p.calls and not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert not active_members(db)


def test_future_confirmation_cannot_start_paid_period_early(p):
    strict(p); approve(p)
    with Session(p.engine) as db:
        db.get(PaymentRequest, "fixture-111").updated_at = p.clock[0] + 3600
        db.commit()
    drain(p)
    assert not p.calls and not p.sent


@pytest.mark.parametrize("unpaid_count", [0, 50])
def test_same_paid_stream_has_same_requests_and_receipts_with_many_unpaid(p, monkeypatch, unpaid_count):
    strict(p); approve(p)
    add_search(p, sid=2, uid=222); approve(p, 222)
    for index in range(unpaid_count):
        add_search(p, sid=100+index, uid=1000+index,
            brand="BMW" if index % 2 else "Volkswagen", minDiscount=index % 30)
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    assert sum(path == "search" for path, _ in p.calls) == 3
    assert sum(path == "info" for path, _ in p.calls) == 1
    assert quotes == ["124"]
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "124"), (222, "124")]
    # Identical stream: both recipients have the same stage timings/latency.
    with Session(p.engine) as db:
        timings = list(db.scalars(select(DeliveryTiming).order_by(DeliveryTiming.delivery_id)))
        assert len(timings) == 2 and all(t.accepted_at - t.discovered_at < 1 for t in timings)


@pytest.mark.parametrize("event", ["photo_expiry", "fallback_expiry", "queued_stop", "read_failure"])
def test_real_telegram_boundary_rechecks_after_photo_and_before_text_fallback(p, monkeypatch, event):
    strict(p); approve(p)
    enable(p, monkeypatch); drain(p)
    monkeypatch.setattr(p.runner, "deliver_tick", lambda: None)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    enqueue(p.engine, require_provider_range=p.settings.ria_ai_price_enabled,
            require_confirmed_deal=p.settings.ria_confirmed_deals_only)
    with Session(p.engine) as db:
        listing = db.scalar(select(Listing))
        listing.car = {**listing.car, "photo": "https://cdn0.riastatic.com/photosnew/auto/photo/test.jpg"}
        db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0] + 2
        db.get(Entitlement, 111).expires_at = p.clock[0] + 2
        if event == "queued_stop":
            db.get(User, 111).ready = False
        db.commit()
    requests = []
    def handler(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        p.clock[0] += 3
        return httpx.Response(400, json={"ok": False, "error_code": 400})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    sender = TelegramSender("fixture-token")
    def load(_):
        if event == "photo_expiry":
            p.clock[0] += 3
        return None
    monkeypatch.setattr(sender.photos, "load", load)
    if event == "read_failure":
        previous = paid_source_access.allowed
        checks = [0]
        def read(db, uid, now=None):
            checks[0] += 1
            if checks[0] > 1:
                raise RuntimeError("isolated access read failure")
            return previous(db, uid, now)
        monkeypatch.setattr(paid_source_access, "allowed", read)
    state = deliver_one(p.engine, p.settings, sender)
    assert state == ("pending" if event == "read_failure" else "cancelled")
    assert requests == ([] if event in {"photo_expiry", "queued_stop", "read_failure"} else ["sendPhoto"])
    with Session(p.engine) as db:
        assert db.scalar(select(DeliveryTiming.accepted_at)) is None


def test_text_fallback_obeys_new_negative_access_check(monkeypatch):
    from backend.tests.test_backend import car
    ready, calls = [True], []
    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        ready[0] = False
        return httpx.Response(400, json={"ok": False, "error_code": 400})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    sender = TelegramSender("fixture-token")
    monkeypatch.setattr(sender.photos, "load", lambda _: None)
    result = sender(111, car(photo="https://cdn0.riastatic.com/photosnew/auto/photo/test.jpg"),
                    before_transport=lambda: ready[0])
    assert result["_access_blocked"] and calls == ["sendPhoto"]


def test_unscoped_source_cannot_spend_even_with_another_paid_client(p):
    strict(p); approve(p)
    client = p.runner.search_factory(p.engine, "fixture")
    client.acquire()
    try:
        with pytest.raises(Exception, match="no_eligible_subscription"):
            client.request("search", {}, lambda value: value, force=True)
    finally:
        client.release()
    assert not p.calls


def test_access_read_failure_before_reservation_has_zero_transport_and_budget(p, monkeypatch):
    strict(p); approve(p)
    client = p.runner.search_factory(p.engine, "fixture")
    def unavailable(*_):
        raise OperationalError("isolated failed access read", {}, None)
    client.request_policy = unavailable
    client.acquire()
    try:
        with pytest.raises(OperationalError):
            client.request("info", {"auto_id": "123"}, lambda value: value, force=True)
    finally:
        client.release()
    assert not p.calls
    with Session(p.engine) as db:
        assert db.scalar(select(api_attempt_audit.RiaApiAttempt)) is None


def test_real_outgoing_attempt_context_is_once_per_shared_call_and_cache_is_free(source, monkeypatch):
    client, _ = source
    calls = opener(monkeypatch, __import__("backend.auto_ria", fromlist=["auto_ria"]), [{"ok": True}])
    context = api_attempt_audit.authorization("publication_search", "isolated-shared-group")
    client.request_policy = lambda *_: context
    client.request("search", {}, lambda value: value)
    client.request("search", {}, lambda value: value)
    assert len(calls) == 1
    with Session(client.engine) as db:
        result = api_attempt_audit.summary(db, 1800000000, 1800000001)
        assert result["transport_authorization"] == [{"reason": "publication_search", "attempts": 1, "distinct_groups": 1}]
        assert result["transport_without_authorization_context"] == 0
        assert len(list(db.scalars(select(api_attempt_audit.RiaApiAuthorization)))) == 1


def test_authorization_schema_is_additive_and_old_attempts_are_preserved(source):
    client, _ = source
    client.fetch = lambda *_: {}
    client.request("search", {}, lambda value: value)
    with Session(client.engine) as db:
        previous = db.scalar(select(api_attempt_audit.RiaApiAttempt)).id
    api_attempt_audit.RiaApiAuthorization.__table__.drop(client.engine)
    api_attempt_audit.RiaApiAuthorization.__table__.create(client.engine, checkfirst=True)
    api_attempt_audit.RiaApiAuthorization.__table__.create(client.engine, checkfirst=True)
    ddl = str(CreateTable(api_attempt_audit.RiaApiAuthorization.__table__).compile(dialect=postgresql.dialect()))
    assert "CREATE TABLE ria_api_authorizations" in ddl and "ALTER" not in ddl
    with Session(client.engine) as db:
        assert db.get(api_attempt_audit.RiaApiAttempt, previous).state == "success"
        assert db.scalar(select(api_attempt_audit.RiaApiAuthorization)) is None


def test_forged_user_id_and_stopped_paid_miniapp_cannot_spend(setup, monkeypatch):
    from backend import app as application
    from backend.manual_payment_models import ManualBase
    from backend.ria_search import RiaSearch
    engine, settings, _ = setup
    ManualBase.metadata.create_all(engine)
    calls = []
    monkeypatch.setattr(application, "RiaSearch", lambda e, key: RiaSearch(e, key, fixture_fetch(calls)))
    with Session(engine) as db:
        for uid in (111, 222):
            db.add(User(id=uid, ready=False if uid == 111 else True))
            db.add(Entitlement(user_id=uid, expires_at=9999999999, updated_at=1))
        db.add(PaymentRequest(id="real-fixture", user_id=222, state="approved", amount_minor=25000,
            currency="UAH", days=30, created_at=1, updated_at=1, expires_at=9999999999))
        db.commit()
    with TestClient(application.create_app(replace(settings, auto_ria_api_key="fixture", full_scan_enabled=True), engine,
                                           paid_source_only=True)) as client:
        assert client.post("/api/cars/search?user_id=222", headers=headers(111), json={}).status_code == 402
        forged = {key: value.replace("111", "222") for key, value in headers(111).items()}
        assert client.post("/api/cars/search", headers=forged, json={}).status_code == 401
        with Session(engine) as db:
            db.add(PaymentRequest(id="stopped-fixture", user_id=111, state="approved", amount_minor=25000,
                currency="UAH", days=30, created_at=1, updated_at=1, expires_at=9999999999))
            db.commit()
        assert client.post("/api/cars/search", headers=headers(111), json={}).status_code == 402
    assert not calls


def test_live_diagnostic_observes_normal_work_once_without_an_extra_source_call(p, monkeypatch):
    from backend import source_pipeline_health
    strict(p); approve(p)
    p.runner.settings = replace(p.settings, admin_telegram_id=987654321)
    captured = []
    monkeypatch.setattr(source_pipeline_health, "log_snapshot", lambda *_: captured.append("read-only snapshot"))
    enable(p, monkeypatch)
    # enable replaces settings; keep the verified synthetic owner setting.
    p.runner.settings = replace(p.runner.settings, admin_telegram_id=987654321)
    drain(p)
    assert captured == ["read-only snapshot"]
    before = len(p.calls)
    p.runner.observe_source_work(0)
    p.runner.observe_source_work(1)
    assert captured == ["read-only snapshot"] and len(p.calls) == before


def test_strict_idle_monitor_never_runs_legacy_selected_probe(p, monkeypatch):
    from backend import notification_diagnostic
    strict(p)
    p.runner.settings = replace(p.settings, ria_diagnostic_listing_id="124")
    monkeypatch.setattr(notification_diagnostic, "retry_selected", lambda *_: pytest.fail("legacy paid probe"))
    drain(p)
    assert not p.calls
