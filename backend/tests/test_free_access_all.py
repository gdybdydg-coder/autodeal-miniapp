"""Owner-enabled free access on the strict production path; no real network."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import billing, manual_checkout, paid_source_access
from backend.app import create_app
from backend.billing_models import BillingControl, Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import Search, User
from backend.monitor import active_members
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import strict
from backend.tests.test_ria_ai_price import enable
from backend.tests.test_backend import setup, headers


def activate(engine, **values):
    with Session(engine) as db:
        row = db.get(BillingControl, billing.CONTROL)
        if row is None:
            row = BillingControl(id=billing.CONTROL)
            db.add(row)
        row.sales = values.get("sales", False)
        row.enforce = values.get("enforce", False)
        row.offer = {billing.FREE_ACCESS_KEY: values.get("flag", True)}
        db.commit()


def test_free_clients_share_discovery_quote_and_delivery_without_fake_payments(p, monkeypatch):
    strict(p)
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    add_search(p, sid=4, uid=444)
    with Session(p.engine) as db:
        db.get(User, 333).ready = False
        db.get(Search, 4).enabled = False
        db.commit()
    activate(p.engine)
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    assert quotes == ["124"]
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "124"), (222, "124")]
    wake(p); drain(p)
    assert len(p.sent) == 2
    with Session(p.engine) as db:
        assert len(active_members(db)) == 2
        assert not list(db.scalars(select(PaymentRequest)))
        assert not list(db.scalars(select(Entitlement)))
        assert not paid_source_access.any_paid(db, p.clock[0])
        assert paid_source_access.any_eligible(db, p.clock[0])
        assert not db.get(User, 333).ready and not db.get(Search, 4).enabled


@pytest.mark.parametrize("values", [dict(flag=False), dict(flag="true"), dict(enforce=True), dict(sales=True)])
def test_free_requires_explicit_boolean_and_closed_sales(p, values):
    strict(p); activate(p.engine, **values)
    drain(p)
    assert not p.calls and not p.sent
    with Session(p.engine) as db:
        assert not billing.free_access_enabled(db)
        assert not paid_source_access.allowed(db, 111, p.clock[0])


@pytest.mark.parametrize("change", ["stop", "revoke_free"])
def test_stop_and_policy_rechecked_between_detail_and_quote(p, monkeypatch, change):
    strict(p); activate(p.engine)
    quotes = enable(p, monkeypatch)
    drain(p)
    original = p.runner.search_factory
    def factory(engine, key):
        source = original(engine, key)
        fetch = source.fetch
        def get(key, path, params):
            result = fetch(key, path, params)
            if path == "info" and params["auto_id"] == "124":
                with Session(engine) as db:
                    if change == "stop":
                        db.get(User, 111).ready = False
                    else:
                        db.get(BillingControl, billing.CONTROL).offer = {}
                    db.commit()
            return result
        source.fetch = get
        return source
    p.runner.search_factory = factory
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    assert not quotes and not p.sent


def test_free_mode_keeps_service_account_exclusion(p):
    strict(p); activate(p.engine)
    paid_source_access.configure(p.engine, replace(p.settings, stats_excluded_user_ids="111"))
    drain(p)
    assert not p.calls and not p.sent


def test_free_status_and_no_payment_prompt_in_production(setup, monkeypatch):
    engine, settings, _ = setup
    ManualBase.metadata.create_all(engine)
    activate(engine)
    api = TestClient(create_app(settings, engine, paid_source_only=True))
    state = api.get("/api/billing/status", headers=headers()).json()
    assert state["access_available"] and not state["paid_access_required"] and not state["sales_enabled"]
    monkeypatch.setattr(manual_checkout, "status", lambda *a: {"free_access_all_users": True})
    for result in (manual_checkout.overview(engine, settings, 111, 1),
                   manual_checkout.terms(engine, settings, 111)):
        assert "безкоштовний" in result["text"]
        assert "250" not in result["text"] and "reply_markup" not in result
    with Session(engine) as db:
        assert "безкоштовний" in billing.card(db, 111, 1)["text"]


def test_operator_rollout_rebases_unpaid_preserves_optouts_receipts_and_is_idempotent(p):
    from backend import free_access_rollout
    from backend.models import MonitorMembership, MonitorWatch, SourceBudget
    strict(p)
    add_search(p, sid=2, uid=222)
    with Session(p.engine) as db:
        db.add(BillingControl(id=billing.CONTROL, sales=True, enforce=True, offer={"method": "bank_manual"}))
        db.get(Search, 2).enabled = False
        old_epoch = db.get(MonitorWatch, 1).epoch
        stopped_epoch = db.get(MonitorWatch, 2).epoch
        total = db.get(SourceBudget, "auto_ria").total
        db.commit()
    settings = replace(p.settings, admin_telegram_id=111)
    result = free_access_rollout.activate(p.engine, settings, actor=111, now=p.clock[0])
    assert result["eligible_searches"] == result["newly_eligible_searches"] == 1
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        assert epoch != old_epoch
        assert db.get(MonitorMembership, 1).started_at == int(p.clock[0])
        assert db.get(MonitorWatch, 2).epoch == stopped_epoch
        assert not db.get(Search, 2).enabled
        assert db.get(SourceBudget, "auto_ria").total == total
    assert free_access_rollout.activate(p.engine, settings, actor=111)["replayed"]
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == epoch
    with pytest.raises(ValueError, match="owner_required"):
        free_access_rollout.activate(p.engine, settings, actor=222)


