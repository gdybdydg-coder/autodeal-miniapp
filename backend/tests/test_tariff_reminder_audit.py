"""Aggregate evidence never fills missing history with fabricated zeros."""
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from backend import tariff_reminder_audience as audience, tariff_reminder_audit as audit
from backend.billing_models import (
    BillingCampaign, CampaignRecipient, Entitlement, MarketingConsent, TariffReminderPreference,
    TariffReminderSchedule,
)
from backend.manual_payment_models import PaymentRequest, ReceiptExpectation
from backend.models import User
from backend.tests.test_tariff_reminder_audience import ledger, UID, NOW
from backend.tests.test_tariff_reminders import daily, bank, review, MORNING, KEY, reminders


def test_summary_reconciles_current_exclusions_and_purchase_renewal(ledger):
    engine, settings = ledger
    with Session(engine) as db, db.begin():
        for offset in range(1, 9):
            uid = UID+offset
            db.add(User(id=uid, ready=offset != 4))
            if offset != 3:
                db.add(MarketingConsent(user_id=uid, allowed=True, blocked=offset == 5,
                    source=audience.EXPLICIT_MARKETING_SOURCE, update_id=1, at=NOW-100))
        db.add(Entitlement(user_id=UID, expires_at=NOW+1, updated_at=NOW-1))
        db.add(PaymentRequest(id="EXPIRED", user_id=UID+2, state="approved",
            expires_at=NOW-1, created_at=NOW-10000, updated_at=NOW-100))
        db.add(PaymentRequest(id="WAITING", user_id=UID+6, state="review",
            created_at=NOW-100, updated_at=NOW-10))
        db.add(TariffReminderPreference(user_id=UID+7, enabled=False, update_id=2, at=NOW))
    settings.stats_excluded_user_ids = str(UID+8)
    with Session(engine) as db:
        data = audience.summary(db, settings, NOW)
        assert audience.ids(db, settings, NOW) == [UID+1, UID+2]
        assert data["total"] == 9 and data["eligible"] == 2 and data["excluded"] == 7
        assert data["eligible_copy"] == {"purchase": 1, "renewal": 1}
        assert data["excluded_by_primary_reason"] == {
            "current_access": 1, "payment_in_progress": 1, "blocked": 1, "test_or_service": 1,
            "stopped": 1, "opted_out": 1, "missing_explicit_consent": 1,
        }


def test_history_is_select_only_and_preserves_missing_receipts_and_runs(daily):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.add(BillingCampaign(id=KEY, not_before=MORNING, deadline=MORNING+1800,
            timezone="Europe/Kyiv", status="complete", blockers=[], content={},
            audience={"selected": 2, "at": MORNING}))
        db.add_all([CampaignRecipient(campaign_id=KEY, user_id=uid, state="sent", message_id=mid)
                    for uid, mid in ((123, 9001), (456, None))])
    statements = []
    def observe(conn, cursor, statement, params, context, many): statements.append(statement)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        with Session(engine) as db:
            data = audit.history(db, db.get(TariffReminderSchedule, reminders.ID), MORNING+90000)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    days = {r["date_kyiv"]: r for r in data["records"]}
    sent = days["2026-10-03"]
    assert sent["run_observed"] and sent["queued"] == 2 and sent["sent"] == 2
    assert sent["telegram_accepted"] == 1 and sent["sent_without_receipt"] == 1
    assert sent["audience_at_selection"] is None
    assert days["2026-10-02"]["status"] == "before_first_planned_run"
    assert days["2026-10-04"]["status"] == "no_saved_campaign"
    assert days["2026-10-04"]["telegram_accepted"] is None
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BillingCampaign)) == 1


def test_stale_incomplete_payments_are_reported_without_mutation(ledger):
    engine, _ = ledger
    with Session(engine) as db, db.begin():
        db.add_all([PaymentRequest(id=state, user_id=UID, state=state,
            created_at=NOW-8*86400, updated_at=NOW-days*86400)
            for state, days in (("created", 8), ("review", 8), ("clarification", 1))])
        db.add(ReceiptExpectation(user_id=UID, active=True, started_at=NOW-8*86400,
                                  updated_at=NOW-8*86400))
    with Session(engine) as db:
        data = audit.pending_payments(db, NOW)
        assert data["requests"]["created"]["stale"] == 1
        assert data["requests"]["review"]["stale"] == 1
        assert data["requests"]["clarification"]["stale"] == 0
        assert data["stale_receipt_expectations"] == 1
        assert audience.eligible(db, ledger[1], UID, NOW) is False
        assert db.get(PaymentRequest, "review").state == "review"
        assert db.get(ReceiptExpectation, UID).active is True
