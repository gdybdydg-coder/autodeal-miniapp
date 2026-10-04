"""Daily renewal regression on synthetic accounts, without outbound I/O."""
import pytest
from sqlalchemy.orm import Session

from backend import tariff_reminder_audience as audience, tariff_reminders as reminders
from backend.billing_models import Entitlement
from backend.manual_payment_models import BankCredit, PaymentAudit, PaymentRequest
from backend.tests.test_tariff_reminders import (
    daily, bank, review, UID, MORNING, BEFORE, accepted, buyer,
)


def test_expired_buyer_is_eligible_again_with_existing_consent(daily):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        buyer(db, UID, MORNING-1)
        db.add(Entitlement(user_id=UID, expires_at=MORNING-1, updated_at=BEFORE))
    with Session(engine) as db:
        assert audience.eligible(db, settings, UID, MORNING)
    calls = []
    assert reminders.tick(engine, settings, accepted(calls), MORNING) == "sent"
    assert len(calls) == 1
    assert "Продовж" in calls[0]["text"]
    assert "250 грн на 30 днів" in calls[0]["text"]
    assert "вперше" not in calls[0]["text"]


@pytest.mark.parametrize("evidence", ["bank_credit", "owner_audit"])
def test_unexpired_saved_confirmation_blocks_even_if_entitlement_row_is_missing(daily, evidence):
    engine, settings, _ = daily
    with Session(engine) as db, db.begin():
        db.add(PaymentRequest(id="APPROVED-HISTORY", user_id=UID, state="rejected",
            created_at=BEFORE, updated_at=BEFORE))
        if evidence == "bank_credit":
            db.add(BankCredit(bank_key="SYNTHETIC-CREDIT", request_id="APPROVED-HISTORY",
                user_id=UID, amount_minor=25000, actor=999, at=BEFORE,
                before_expiry=0, after_expiry=MORNING+100))
        else:
            db.add(PaymentAudit(id="SYNTHETIC-AUDIT", request_id="APPROVED-HISTORY",
                actor=999, action="approved", at=BEFORE, revision=1,
                before_expiry=0, after_expiry=MORNING+100))
    with Session(engine) as db:
        assert not audience.eligible(db, settings, UID, MORNING)
