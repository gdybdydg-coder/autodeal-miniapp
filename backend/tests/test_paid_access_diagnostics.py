"""Read-only comparison of real approval writers and paid-source eligibility.

Temporary SQLite and fixture accounts only; diagnostics never grant access or
construct a provider/Telegram transport.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import paid_source_access
from backend.billing_models import Entitlement
from backend.manual_payment_models import PaymentRequest
from backend.models import MonitorMembership, Search, SourceBudget, User
from backend.source_pipeline_health import snapshot
from backend.tests.test_monitor import p
from backend.tests.test_paid_source_boundaries import confirm, owner_settings
from backend.tests.test_paid_sources_production import approve, strict


@pytest.mark.parametrize("legacy", [False, True])
def test_both_owner_confirmation_formats_match_current_source_access_without_io(p, monkeypatch, legacy):
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    confirm(p, settings, "fixture-111", legacy=legacy)
    with Session(p.engine) as db:
        before = db.get(SourceBudget, "auto_ria").total
        result = snapshot(db, settings, p.clock[0])
        assert db.get(SourceBudget, "auto_ria").total == before
        assert not db.new and not db.dirty and not db.deleted
    audit = result["source_access_audit"]
    assert result["current_paid_clients"] == result["ready_enabled_searches"] == 1
    assert audit["current_owner_approval_with_access_clients"] == 1
    assert audit["owner_approval_with_access_blocked_by_guard_clients"] == 0
    assert audit["current_purchase_without_owner_audit_clients"] == 0
    assert audit["paid_ready_searches_without_current_membership"] == 0
    assert "fixture-111" not in json.dumps(result) and "user_id" not in json.dumps(result)
    assert not p.calls and not p.sent


@pytest.mark.parametrize("change,key", [
    ("pending_request", "owner_approval_with_access_blocked_by_guard_clients"),
    ("expired_request", "owner_approval_with_access_blocked_by_guard_clients"),
    ("future_request", "owner_approval_with_access_blocked_by_guard_clients"),
    ("revoked_access", "current_owner_approval_without_access_clients"),
    ("stale_membership", "paid_ready_searches_without_current_membership"),
    ("missing_membership", "paid_ready_searches_without_current_membership"),
    ("stop", "current_paid_not_ready_clients"),
    ("disabled_search", "current_paid_without_enabled_search_clients"),
    ("configured_exclusion", "current_paid_excluded_config_only_clients"),
])
def test_private_audit_distinguishes_mismatches_revocations_stop_and_explicit_exclusions(p, monkeypatch, change, key):
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    confirm(p, settings, "fixture-111")
    with Session(p.engine) as db:
        if change == "pending_request": db.get(PaymentRequest, "fixture-111").state = "review"
        elif change == "expired_request": db.get(PaymentRequest, "fixture-111").expires_at = p.clock[0]
        elif change == "future_request": db.get(PaymentRequest, "fixture-111").updated_at = p.clock[0] + 3600
        elif change == "revoked_access": db.get(Entitlement, 111).expires_at = p.clock[0]
        elif change == "stale_membership": db.get(MonitorMembership, 1).epoch = "retired-fixture-epoch"
        elif change == "missing_membership": db.delete(db.get(MonitorMembership, 1))
        elif change == "stop": db.get(User, 111).ready = False
        elif change == "disabled_search": db.get(Search, 1).enabled = False
        db.commit()
    if change in {"configured_exclusion", "admin_exclusion"}:
        from dataclasses import replace
        settings = replace(settings, **({"stats_excluded_user_ids": "111"} if change == "configured_exclusion"
                                        else {"admin_telegram_id": 111}))
        paid_source_access.configure(p.engine, settings)
    with Session(p.engine) as db:
        audit = snapshot(db, settings, p.clock[0])["source_access_audit"]
        assert audit[key] == 1
        assert not db.new and not db.dirty and not db.deleted
    assert not p.calls and not p.sent


def test_reused_session_does_not_cache_another_sessions_payment_revocation(p):
    strict(p); approve(p)
    with Session(p.engine) as reader:
        assert paid_source_access.allowed(reader, 111, p.clock[0])
        reader.get(Entitlement, 111)
        with Session(p.engine) as writer:
            writer.get(PaymentRequest, "fixture-111").expires_at = p.clock[0]
            writer.commit()
        assert not paid_source_access.allowed(reader, 111, p.clock[0])
    assert not p.calls and not p.sent


def test_private_admin_audit_reports_ordinary_paid_access_without_granting_or_io(p, monkeypatch):
    from dataclasses import replace
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    confirm(p, settings, "fixture-111")
    settings = replace(settings, admin_telegram_id=111, stats_excluded_user_ids="111")
    paid_source_access.configure(p.engine, settings)
    with Session(p.engine) as db:
        before = db.get(SourceBudget, "auto_ria").total
        result = snapshot(db, settings, p.clock[0])
        assert not db.new and not db.dirty and not db.deleted
        assert db.get(SourceBudget, "auto_ria").total == before
    account = result["paid_cohort_details"]["accounts"][0]
    assert account["account"] == "administrator"
    assert account["source_access_allowed"] and account["owner_approval_recorded"]
    assert account["planned_searches"] == account["searches_enabled"] == 1
    assert account["purchase_confirmed_at"] < account["purchase_expires_at"]
    assert result["source_access_audit"]["current_paid_excluded_admin_clients"] == 0
    encoded = json.dumps(result)
    assert "fixture-111" not in encoded and "user_id" not in encoded
    assert "Volkswagen" not in encoded and not p.calls and not p.sent


def test_private_delivery_trace_uses_saved_event_time_and_excludes_old_admin_copies(p, monkeypatch):
    from backend.models import Delivery, DeliveryTiming, SourceProbe
    from backend.tests.test_monitor import drain, wake
    strict(p)
    settings = owner_settings(p, monkeypatch)
    approve(p, state="review", purchase_until=0, access_until=0)
    confirm(p, settings, "fixture-111")
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery))
        timing = db.get(DeliveryTiming, delivery.id)
        result = snapshot(db, settings, p.clock[0])
        trace = result["paid_cohort_details"]["accounts"][0]["recent_ordinary_deliveries"][0]
        assert trace["source_id"] == "124" and trace["state"] == "sent"
        assert trace["owner_approval_covers_acceptance"] is True
        assert trace["saved_access_event_covers_acceptance"] is True
        assert trace["discovered_at"] <= trace["evaluated_at"] <= trace["queued_at"] <= trace["accepted_at"]
        db.add(SourceProbe(id="owner-car-copy-v1-" + str(delivery.id), status="sent",
            checked_at=p.clock[0], result={"private_snapshot": "do_not_log"}))
        db.commit()
        assert not snapshot(db, settings, p.clock[0])["paid_cohort_details"]["accounts"][0]["recent_ordinary_deliveries"]
        assert snapshot(db, settings, p.clock[0])["delivery"]["last_client_accepted_at"] is None
        db.get(SourceProbe, "owner-car-copy-v1-" + str(delivery.id)).status = "ordinary_reclaimed"
        db.commit()
        assert snapshot(db, settings, p.clock[0])["delivery"]["last_client_accepted_at"] == timing.accepted_at
