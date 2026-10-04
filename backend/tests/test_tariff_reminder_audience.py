"""Daily advertising eligibility on isolated ledgers, with no external I/O."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend import tariff_reminder_audience as audience
from backend.billing_models import (
    AccessEvent, BillingOrder, CampaignRecipient, Entitlement, MarketingConsent,
    TariffReminderPreference,
)
from backend.manual_payment_models import (
    BankCredit, ManualBase, PaymentAudit, PaymentRequest, ReceiptExpectation,
)
from backend.models import Base, StarsTestOrder, User
from backend.subscription_promotion import EligibilityUnavailable

NOW = 1791010800
ADMIN = 987654321
UID = 10001


@pytest.fixture
def ledger(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "reminder-audience.sqlite"))
    Base.metadata.create_all(engine)
    ManualBase.metadata.create_all(engine)
    settings = SimpleNamespace(admin_telegram_id=ADMIN, stats_excluded_user_ids="")
    with Session(engine) as db, db.begin():
        db.add(User(id=UID, ready=True))
        db.add(MarketingConsent(user_id=UID, allowed=True, blocked=False,
            update_id=1, at=NOW-100, source=audience.EXPLICIT_MARKETING_SOURCE))
    yield engine, settings
    engine.dispose()


def eligible(ledger):
    with Session(ledger[0]) as db:
        return audience.eligible(db, ledger[1], UID, NOW)


def preference(ledger, enabled, at=NOW-50):
    with Session(ledger[0]) as db, db.begin():
        db.merge(TariffReminderPreference(user_id=UID, enabled=enabled, update_id=2, at=at))


def test_verified_consent_and_all_bulk_views_agree(ledger):
    with Session(ledger[0]) as db:
        assert audience.eligible(db, ledger[1], UID, NOW)
        assert audience.ids(db, ledger[1], NOW) == [UID]
        assert audience.count(db, ledger[1], NOW) == 1


@pytest.mark.parametrize("source", ["start", "fixture", "migration", "", "explicit_opt_out"])
def test_allowed_flag_with_unverified_origin_is_not_permission(ledger, source):
    with Session(ledger[0]) as db, db.begin():
        db.get(MarketingConsent, UID).source = source
    assert not eligible(ledger)


def test_start_alone_or_missing_consent_is_not_permission(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.delete(db.get(MarketingConsent, UID))
    assert not eligible(ledger)


def test_explicit_daily_consent_can_be_category_only(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.delete(db.get(MarketingConsent, UID))
    preference(ledger, True)
    assert eligible(ledger)
    with Session(ledger[0]) as db:
        assert db.get(MarketingConsent, UID) is None


def test_daily_opt_out_overrides_general_marketing_opt_in(ledger):
    preference(ledger, False)
    assert not eligible(ledger)
    with Session(ledger[0]) as db:
        assert db.get(MarketingConsent, UID).allowed is True
        assert db.get(User, UID).ready is True


@pytest.mark.parametrize("update_id, expected", [(-1, False), (1, True), (2, False), (3, False)])
def test_daily_permission_respects_later_or_equal_global_opt_out(ledger, update_id, expected):
    preference(ledger, True, NOW-50)
    with Session(ledger[0]) as db, db.begin():
        consent = db.get(MarketingConsent, UID)
        consent.allowed, consent.source, consent.at = False, audience.GLOBAL_OPT_OUT_SOURCE, NOW
        consent.update_id = update_id
    assert eligible(ledger) is expected


def test_delayed_daily_opt_in_19_cannot_override_global_refusal_20(ledger):
    with Session(ledger[0]) as db, db.begin():
        consent = db.get(MarketingConsent, UID)
        consent.allowed, consent.source, consent.update_id, consent.at = False, audience.GLOBAL_OPT_OUT_SOURCE, 20, NOW-10
        db.add(TariffReminderPreference(user_id=UID, enabled=True, update_id=19, at=NOW))
    assert not eligible(ledger)


def test_delayed_global_refusal_20_does_not_override_daily_opt_in_21(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.add(TariffReminderPreference(user_id=UID, enabled=True, update_id=21, at=NOW-10))
        consent = db.get(MarketingConsent, UID)
        consent.allowed, consent.source, consent.update_id, consent.at = False, audience.GLOBAL_OPT_OUT_SOURCE, 20, NOW
    assert eligible(ledger)


def test_daily_permission_without_verified_telegram_update_fails_closed(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.add(TariffReminderPreference(user_id=UID, enabled=True, update_id=-1, at=NOW))
    assert not eligible(ledger)


def test_general_permission_without_verified_telegram_update_fails_closed(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.get(MarketingConsent, UID).update_id = -1
    assert not eligible(ledger)


def test_general_reoptin_does_not_cancel_daily_specific_refusal(ledger):
    preference(ledger, False)
    with Session(ledger[0]) as db, db.begin():
        db.get(MarketingConsent, UID).at = NOW
    assert not eligible(ledger)


@pytest.mark.parametrize("daily", [False, True])
def test_blocked_flag_or_stop_always_excludes(ledger, daily):
    if daily:
        preference(ledger, True)
    with Session(ledger[0]) as db, db.begin():
        db.get(MarketingConsent, UID).blocked = True
    assert not eligible(ledger)
    with Session(ledger[0]) as db, db.begin():
        db.get(MarketingConsent, UID).blocked = False
        db.get(User, UID).ready = False
    assert not eligible(ledger)


@pytest.mark.parametrize("history", [
    "approved", "approval_audit", "bank_credit", "expired_manual_paid", "active_gift",
    "expired_ambiguous_entitlement", "review", "clarification", "awaiting_image",
    "awaiting_unbound_image", "pending_order", "former_paid_order", "former_refunded_order",
    "paid_test_order", "prior_403",
])
def test_purchase_access_or_payment_in_progress_excludes(ledger, history):
    with Session(ledger[0]) as db, db.begin():
        if history in ("approved", "approval_audit", "bank_credit", "review", "clarification"):
            state = history if history in ("approved", "review", "clarification") else "rejected"
            db.add(PaymentRequest(id="AD-FIXTURE", user_id=UID, state=state,
                amount_minor=25000, currency="UAH", days=30, created_at=NOW-1000, updated_at=NOW))
            if history == "approval_audit":
                db.add(PaymentAudit(id="AUDIT-FIXTURE", request_id="AD-FIXTURE", actor=ADMIN,
                    action="approved", at=NOW-100, revision=1, after_expiry=NOW-1))
            if history == "bank_credit":
                db.add(BankCredit(bank_key="BANK-FIXTURE", request_id="AD-FIXTURE", user_id=UID,
                    amount_minor=25000, actor=ADMIN, at=NOW-100, before_expiry=0, after_expiry=NOW-1))
        elif history in ("expired_manual_paid", "active_gift"):
            db.add(AccessEvent(id="ACCESS-FIXTURE", user_id=UID, actor=ADMIN,
                kind="manual_paid" if history == "expired_manual_paid" else "gift",
                at=NOW-1000, expires_at=NOW-1 if history == "expired_manual_paid" else NOW+1,
                reason="Isolated fixture"))
        elif history == "expired_ambiguous_entitlement":
            db.add(Entitlement(user_id=UID, expires_at=NOW-1, updated_at=NOW-100))
        elif history in ("awaiting_image", "awaiting_unbound_image"):
            db.add(ReceiptExpectation(user_id=UID, request_id="AD-FIXTURE" if history == "awaiting_image" else None,
                active=True, awaiting_image=True, started_at=NOW-100, updated_at=NOW))
        elif history in ("pending_order", "former_paid_order", "former_refunded_order"):
            state = {"pending_order":"pending", "former_paid_order":"paid", "former_refunded_order":"refunded"}[history]
            db.add(BillingOrder(id="ORDER-FIXTURE", user_id=UID, update_id=50, amount=1,
                terms_version="fixture", created_at=NOW-1000, state=state, expires_at=NOW-1))
        elif history == "paid_test_order":
            db.add(StarsTestOrder(id="TEST-FIXTURE", user_id=UID, command_update=51,
                created_at=NOW-1000, state="paid", charge_id="CHARGE-FIXTURE", paid_until=NOW-1))
        elif history == "prior_403":
            db.add(CampaignRecipient(campaign_id="OLD-FIXTURE", user_id=UID, state="failed",
                attempted_at=NOW-100, retry_at=0, error="403"))
    expired_history = {"approval_audit", "bank_credit", "expired_manual_paid",
                       "expired_ambiguous_entitlement", "former_paid_order", "former_refunded_order"}
    assert eligible(ledger) is (history in expired_history)


@pytest.mark.parametrize("state", ["created", "rejected"])
def test_nonpayment_terminal_or_created_request_is_not_a_purchase(ledger, state):
    with Session(ledger[0]) as db, db.begin():
        db.add(PaymentRequest(id="AD-FIXTURE", user_id=UID, state=state,
            amount_minor=25000, currency="UAH", days=30, created_at=NOW-100, updated_at=NOW))
    assert eligible(ledger)


def test_three_clients_two_buyers_only_third_qualifies(ledger):
    with Session(ledger[0]) as db, db.begin():
        for uid in (UID+1, UID+2):
            db.add(User(id=uid, ready=True))
            db.add(MarketingConsent(user_id=uid, allowed=True, blocked=False, update_id=1,
                at=NOW-100, source=audience.EXPLICIT_MARKETING_SOURCE))
        for index, uid in enumerate((UID, UID+1)):
            db.add(PaymentRequest(id="AD-BUYER-"+str(index), user_id=uid, state="approved",
                amount_minor=25000, currency="UAH", days=30, created_at=NOW-1000, updated_at=NOW-100))
    with Session(ledger[0]) as db:
        assert audience.ids(db, ledger[1], NOW) == [UID+2]
        assert audience.count(db, ledger[1], NOW) == 1


def test_daily_preferences_and_global_refusals_are_correlated_per_user(ledger):
    with Session(ledger[0]) as db, db.begin():
        for uid in (UID+1, UID+2, UID+3):
            db.add(User(id=uid, ready=True))
            db.add(TariffReminderPreference(user_id=uid, enabled=True, update_id=2, at=NOW-50))
        db.add(MarketingConsent(user_id=UID+1, allowed=False, blocked=False, update_id=1,
            at=NOW-100, source=audience.GLOBAL_OPT_OUT_SOURCE))
        db.add(MarketingConsent(user_id=UID+2, allowed=False, blocked=False, update_id=3,
            at=NOW-10, source=audience.GLOBAL_OPT_OUT_SOURCE))
        db.add(MarketingConsent(user_id=UID+3, allowed=True, blocked=True, update_id=1,
            at=NOW-100, source=audience.EXPLICIT_MARKETING_SOURCE))
    with Session(ledger[0]) as db:
        assert audience.ids(db, ledger[1], NOW) == [UID, UID+1]
        assert audience.count(db, ledger[1], NOW) == 2


def test_empty_client_cohort_is_an_authentic_zero(ledger):
    with Session(ledger[0]) as db, db.begin():
        db.delete(db.get(User, UID))
    with Session(ledger[0]) as db:
        assert audience.ids(db, ledger[1], NOW) == []
        assert audience.count(db, ledger[1], NOW) == 0
        assert not audience.eligible(db, ledger[1], UID, NOW)


def test_verified_config_accounts_are_excluded(ledger):
    engine, settings = ledger
    settings.stats_excluded_user_ids = str(UID)
    assert not eligible(ledger)
    settings.stats_excluded_user_ids = ""
    settings.admin_telegram_id = UID
    assert eligible(ledger)  # Role alone neither grants access nor excludes a client.


def test_invalid_account_exclusion_config_fails_closed(ledger):
    ledger[1].stats_excluded_user_ids = "unverified-name"
    with Session(ledger[0]) as db:
        for operation in (audience.count, audience.ids):
            with pytest.raises(EligibilityUnavailable):
                operation(db, ledger[1], NOW)
        with pytest.raises(EligibilityUnavailable):
            audience.eligible(db, ledger[1], UID, NOW)


def test_missing_payment_table_is_unavailable_not_zero(ledger):
    PaymentRequest.__table__.drop(ledger[0])
    with Session(ledger[0]) as db:
        for operation in (audience.count, audience.ids):
            with pytest.raises(EligibilityUnavailable):
                operation(db, ledger[1], NOW)
        with pytest.raises(EligibilityUnavailable):
            audience.eligible(db, ledger[1], UID, NOW)


def test_bulk_audience_count_and_final_recheck_each_use_one_statement(ledger):
    statements = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(ledger[0], "before_cursor_execute", observe)
    try:
        with Session(ledger[0]) as db:
            assert audience.ids(db, ledger[1], NOW) == [UID]
            assert len(statements) == 1
            assert audience.count(db, ledger[1], NOW) == 1
            assert len(statements) == 2
            assert audience.eligible(db, ledger[1], UID, NOW)
            assert len(statements) == 3
    finally:
        event.remove(ledger[0], "before_cursor_execute", observe)


def test_final_check_reads_changed_consent_not_identity_cache(ledger):
    with Session(ledger[0]) as stale:
        cached = stale.get(MarketingConsent, UID)
        assert cached.allowed is True
        assert audience.eligible(stale, ledger[1], UID, NOW)
        with Session(ledger[0]) as updated, updated.begin():
            updated.get(MarketingConsent, UID).allowed = False
        assert cached.allowed is True
        assert not audience.eligible(stale, ledger[1], UID, NOW)
