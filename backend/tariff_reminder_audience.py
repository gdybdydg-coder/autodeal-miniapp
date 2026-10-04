"""Current-access audience for daily purchase and renewal reminders.

An old /start is never consent. Explicit daily consent is scoped to this
campaign and does not mutate general marketing preferences or vehicle access.
"""
from sqlalchemy import and_, case, exists, func, not_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .billing_models import (AccessEvent, BillingOrder, CampaignRecipient, Entitlement,
                             MarketingConsent, TariffReminderPreference)
from .manual_payment_models import BankCredit, PaymentAudit, PaymentRequest, ReceiptExpectation
from .models import StarsTestOrder, User
from .purchase_stats import excluded_user_ids
from .subscription_promotion import EligibilityUnavailable, PAID_STATES

EXPLICIT_MARKETING_SOURCE = "explicit_marketing_button_v1"
GLOBAL_OPT_OUT_SOURCE = "explicit_opt_out"


def clauses(settings, now):
    """Only current access blocks renewals; consent rules remain unchanged."""
    try:
        # Being an administrator is not evidence of payment or a test account.
        exclusions = excluded_user_ids(settings, admin_uid=0)
    except (TypeError, ValueError, AttributeError):
        raise EligibilityUnavailable("tariff_reminder_exclusions_unavailable") from None

    preference_exists = exists().where(TariffReminderPreference.user_id == User.id)
    explicit_general_consent = exists().where(
        MarketingConsent.user_id == User.id,
        MarketingConsent.allowed.is_(True),
        MarketingConsent.blocked.is_(False),
        MarketingConsent.source == EXPLICIT_MARKETING_SOURCE,
        MarketingConsent.update_id >= 0,
    )
    # Telegram update IDs order the choices independently of webhook processing
    # order. Processing timestamps would let a delayed older opt-in override a
    # newer refusal. Missing provenance on a global refusal fails closed.
    explicit_daily_consent = exists().where(
        TariffReminderPreference.user_id == User.id,
        TariffReminderPreference.enabled.is_(True),
        TariffReminderPreference.update_id >= 0,
        not_(exists().where(
            MarketingConsent.user_id == TariffReminderPreference.user_id,
            or_(MarketingConsent.blocked.is_(True), and_(
                MarketingConsent.allowed.is_(False),
                MarketingConsent.source == GLOBAL_OPT_OUT_SOURCE,
                or_(MarketingConsent.update_id < 0,
                    MarketingConsent.update_id >= TariffReminderPreference.update_id),
            )),
        )),
    )
    permission = or_(
        and_(not_(preference_exists), explicit_general_consent),
        explicit_daily_consent,
    )
    latest_access = (select(AccessEvent.expires_at).where(AccessEvent.user_id == User.id)
        .order_by(AccessEvent.at.desc(), AccessEvent.id.desc()).limit(1)
        .correlate(User).scalar_subquery())
    active = or_(
        exists().where(Entitlement.user_id == User.id, Entitlement.expires_at > now),
        and_(not_(exists().where(Entitlement.user_id == User.id)), latest_access > now),
        exists().where(PaymentRequest.user_id == User.id, PaymentRequest.state == "approved",
                       PaymentRequest.expires_at > now),
        exists().where(PaymentAudit.request_id == PaymentRequest.id,
                       PaymentRequest.user_id == User.id, PaymentAudit.action == "approved",
                       PaymentAudit.after_expiry > now),
        exists().where(BankCredit.user_id == User.id, BankCredit.after_expiry > now),
        exists().where(BillingOrder.user_id == User.id, BillingOrder.state == "paid",
                       BillingOrder.expires_at > now),
    )
    pending = or_(
        exists().where(ReceiptExpectation.user_id == User.id, ReceiptExpectation.active.is_(True)),
        exists().where(PaymentRequest.user_id == User.id,
                       PaymentRequest.state.not_in(("created", "rejected", "approved"))),
        exists().where(PaymentRequest.user_id == User.id, PaymentRequest.state == "approved",
                       PaymentRequest.expires_at.is_(None)),
        exists().where(BillingOrder.user_id == User.id,
                       or_(BillingOrder.state.in_(("pending", "refund_sending", "refund_uncertain")),
                           and_(BillingOrder.state == "paid", BillingOrder.expires_at.is_(None)))),
    )
    blocked = or_(
        exists().where(MarketingConsent.user_id == User.id, MarketingConsent.blocked.is_(True)),
        exists().where(CampaignRecipient.user_id == User.id, CampaignRecipient.state == "failed",
                       CampaignRecipient.error == "403"),
    )
    opted_out = or_(
        exists().where(TariffReminderPreference.user_id == User.id,
                       TariffReminderPreference.enabled.is_(False)),
        and_(not_(permission), exists().where(MarketingConsent.user_id == User.id,
            MarketingConsent.allowed.is_(False), MarketingConsent.source == GLOBAL_OPT_OUT_SOURCE)),
    )
    service = or_(User.id.in_(exclusions), exists().where(StarsTestOrder.user_id == User.id,
        or_(StarsTestOrder.state.in_(PAID_STATES), StarsTestOrder.charge_id.is_not(None))))
    return {"current_access": func.coalesce(active, False), "payment_in_progress": pending,
            "blocked": blocked, "test_or_service": service, "stopped": User.ready.is_not(True),
            "opted_out": opted_out, "missing_explicit_consent": not_(permission)}


def query(settings, now):
    """One authoritative scalar query at queue time and immediately before send."""
    return select(User.id).where(*(not_(condition) for condition in clauses(settings, now).values()))


def former_buyer_clause(uid):
    """Historical purchase chooses renewal copy; it never grants current access."""
    return or_(
        exists().where(PaymentRequest.user_id == uid, PaymentRequest.state == "approved",
                       PaymentRequest.amount_minor > 0, PaymentRequest.days > 0),
        exists().where(PaymentAudit.request_id == PaymentRequest.id,
                       PaymentRequest.user_id == uid, PaymentAudit.action == "approved"),
        exists().where(BankCredit.user_id == uid, BankCredit.amount_minor > 0),
        exists().where(AccessEvent.user_id == uid, AccessEvent.kind.in_(("paid", "manual_paid"))),
        exists().where(BillingOrder.user_id == uid,
                       or_(BillingOrder.state.in_(PAID_STATES), BillingOrder.charge_id.is_not(None))),
    )


def former_buyer(db, uid):
    try:
        return bool(db.scalar(select(former_buyer_clause(uid))))
    except SQLAlchemyError:
        raise EligibilityUnavailable("tariff_reminder_history_unavailable") from None


def summary(db, settings, now):
    """Aggregate exclusions with one primary reason per user; no identities."""
    try:
        reason = case(*[(condition, name) for name, condition in clauses(settings, now).items()],
                      else_="eligible").label("reason")
        grouped = select(reason, former_buyer_clause(User.id).label("renewal")).select_from(User).subquery()
        rows = db.execute(select(grouped.c.reason, grouped.c.renewal, func.count())
                          .group_by(grouped.c.reason, grouped.c.renewal)).all()
        reasons, total, eligible_total, renewals = {}, 0, 0, 0
        for name, renewal, count in rows:
            total += count
            if name == "eligible":
                eligible_total += count
                renewals += count if renewal else 0
            else:
                reasons[name] = reasons.get(name, 0) + count
        return {"total": total, "eligible": eligible_total, "excluded": total-eligible_total,
                "excluded_by_primary_reason": reasons,
                "eligible_copy": {"purchase": eligible_total-renewals, "renewal": renewals},
                "basis": "current_access_and_unchanged_explicit_consent", "observed_at": now}
    except SQLAlchemyError:
        raise EligibilityUnavailable("tariff_reminder_audience_unavailable") from None


def eligible(db, settings, uid, now):
    """Read scalars afresh; never trust a previously loaded User/consent object."""
    try:
        return db.scalar(query(settings, now).where(User.id == uid)) is not None
    except SQLAlchemyError:
        raise EligibilityUnavailable("tariff_reminder_audience_unavailable") from None


def ids(db, settings, now):
    """Fetch the full eligible cohort without a per-user query."""
    try:
        return list(db.scalars(query(settings, now).order_by(User.id)))
    except SQLAlchemyError:
        raise EligibilityUnavailable("tariff_reminder_audience_unavailable") from None


def count(db, settings, now):
    """Database failure is an unavailable count, never a fabricated zero."""
    try:
        return db.scalar(select(func.count()).select_from(query(settings, now).subquery()))
    except SQLAlchemyError:
        raise EligibilityUnavailable("tariff_reminder_audience_unavailable") from None
