"""A paid owner uses the same source policy as clients, never an admin grant."""
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import billing, paid_source_access, purchase_stats
from backend.billing_models import BillingControl, Entitlement
from backend.models import Search, StarsTestOrder, SubscriptionPreview, User
from backend.monitor import active_members
from backend.stars_test import OWNER as PILOT_OWNER
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import approve, strict
from backend.tests.test_paid_source_boundaries import confirm
from backend.tests.test_ria_ai_price import enable


def configure_owner(p, uid, *, configured_exclusion=False):
    settings = replace(p.settings, admin_telegram_id=uid,
        stats_excluded_user_ids=str(uid) if configured_exclusion else "")
    paid_source_access.configure(p.engine, settings)
    if uid != 111:
        add_search(p, sid=2, uid=uid)
    return settings


@pytest.mark.parametrize("uid,configured_exclusion", [(111, False), (PILOT_OWNER, False),
                                                     (PILOT_OWNER, True)])
def test_confirmed_paid_owner_is_a_current_member_while_stats_still_exclude_owner(p, uid, configured_exclusion):
    strict(p)
    settings = configure_owner(p, uid, configured_exclusion=configured_exclusion)
    approve(p, uid)
    with Session(p.engine) as db:
        assert paid_source_access.allowed(db, uid, p.clock[0])
        assert [search.user_id for search, _, _ in active_members(db)] == [uid]
        assert paid_source_access.any_paid(db, p.clock[0])
        assert purchase_stats.counts(db, settings)["buyers"] == 0
    assert not p.calls and not p.sent


@pytest.mark.parametrize("kind", ["none", "gift", "review", "expired", "revoked", "future",
                                  "stars_test", "subscription_preview"])
def test_admin_role_and_pilot_namespaces_never_grant_source_access(p, kind):
    strict(p)
    uid = PILOT_OWNER
    configure_owner(p, uid)
    if kind in {"review", "expired", "revoked", "future"}:
        approve(p, uid, state="review" if kind == "review" else "approved",
            purchase_until=p.clock[0] if kind == "expired" else None,
            access_until=p.clock[0] if kind == "revoked" else None)
    with Session(p.engine) as db:
        if kind == "gift":
            db.add(Entitlement(user_id=uid, expires_at=p.clock[0]+3600, updated_at=p.clock[0]))
        elif kind == "future":
            from backend.manual_payment_models import PaymentRequest
            db.get(PaymentRequest, f"fixture-{uid}").updated_at = p.clock[0] + 3600
        elif kind == "stars_test":
            db.add(StarsTestOrder(id="fixture-stars", user_id=uid, command_update=44,
                created_at=p.clock[0], state="paid", paid_until=p.clock[0]+3600))
        elif kind == "subscription_preview":
            db.add(SubscriptionPreview(user_id=uid, updated_at=p.clock[0],
                state={"expires_at": p.clock[0]+3600, "orders": [{"status": "approved"}]}))
        db.commit()
        assert not paid_source_access.allowed(db, uid, p.clock[0])
        assert not active_members(db)
        assert not paid_source_access.any_paid(db, p.clock[0])
    drain(p)
    assert not p.calls and not p.sent


@pytest.mark.parametrize("legacy", [False, True])
def test_owner_confirmation_updates_source_access_without_restart_or_new_schema_fields(p, monkeypatch, legacy):
    strict(p)
    settings = configure_owner(p, PILOT_OWNER)
    settings = replace(settings, manual_payment_review_enabled=True)
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(PILOT_OWNER))
    with Session(p.engine) as db:
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=True, offer={}))
        db.commit()
    approve(p, PILOT_OWNER, state="review", purchase_until=0, access_until=0)
    with Session(p.engine) as reader:
        assert not paid_source_access.allowed(reader, PILOT_OWNER, p.clock[0])
        confirm(p, settings, f"fixture-{PILOT_OWNER}", legacy=legacy)
        assert paid_source_access.allowed(reader, PILOT_OWNER, p.clock[0])
        assert [search.user_id for search, _, _ in active_members(reader)] == [PILOT_OWNER]
        from backend.manual_payment_models import PaymentRequest
        row = reader.scalar(select(PaymentRequest).where(PaymentRequest.user_id == PILOT_OWNER))
        assert row.receipt_file_id is None and row.terms_version is None
    assert not p.calls and not p.sent


def test_non_admin_explicit_service_accounts_remain_excluded_even_with_approved_records(p):
    strict(p)
    settings = replace(p.settings, admin_telegram_id=987654321, stats_excluded_user_ids="111")
    paid_source_access.configure(p.engine, settings)
    add_search(p, sid=2, uid=PILOT_OWNER)
    approve(p, 111); approve(p, PILOT_OWNER)
    with Session(p.engine) as db:
        assert not paid_source_access.allowed(db, 111, p.clock[0])
        assert not paid_source_access.allowed(db, PILOT_OWNER, p.clock[0])
        assert not active_members(db)
    drain(p)
    assert not p.calls and not p.sent


@pytest.mark.parametrize("legacy", [False, True])
def test_genuinely_paid_owner_renewal_keeps_epoch_and_pending_claim(p, monkeypatch, legacy):
    from backend.manual_payment_models import PaymentRequest
    from backend.models import Delivery, Listing, MonitorMembership, MonitorWatch
    strict(p)
    settings = configure_owner(p, PILOT_OWNER)
    settings = replace(settings, manual_payment_review_enabled=True)
    monkeypatch.setenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", str(PILOT_OWNER))
    approve(p, PILOT_OWNER)
    with Session(p.engine) as db:
        db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=True, offer={}))
        epoch = db.get(MonitorWatch, 2).epoch
        start = db.get(MonitorMembership, 2).started_at
        before = db.get(Entitlement, PILOT_OWNER).expires_at
        db.add(PaymentRequest(id="renewal-owner", user_id=PILOT_OWNER, state="review",
            amount_minor=25000, currency="UAH", days=30, created_at=p.clock[0], updated_at=p.clock[0]))
        listing = Listing(source="auto_ria", source_id="owner-renewal", car={})
        db.add(listing); db.flush()
        pending = Delivery(user_id=PILOT_OWNER, listing_id=listing.id, state="pending")
        db.add(pending); db.flush()
        delivery_id = pending.id
        db.commit()
    assert confirm(p, settings, "renewal-owner", legacy=legacy)["expires_at"] == before + 30 * 86400
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 2).epoch == epoch
        assert db.get(MonitorMembership, 2).started_at == start
        assert db.get(Delivery, delivery_id).state == "pending"
    assert not p.calls and not p.sent


def test_unpaid_first_does_not_stop_two_later_paid_clients_or_add_source_calls(p, monkeypatch):
    strict(p)
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    approve(p, 222); approve(p, 333)
    quotes = enable(p, monkeypatch)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    assert quotes == ["124"]
    assert sum(path == "info" and params["auto_id"] == "124" for path, params in p.calls) == 1
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(222, "124"), (333, "124")]
    with Session(p.engine) as db:
        assert db.get(Search, 1).enabled and db.get(User, 111).ready
