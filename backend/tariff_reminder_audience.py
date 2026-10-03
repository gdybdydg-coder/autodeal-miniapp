"""Authoritative, bulk audience for daily first-purchase reminders.

An old /start is never consent. Explicit daily consent is scoped to this
campaign and does not mutate general marketing preferences or vehicle access.
"""
from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .billing_models import MarketingConsent, TariffReminderPreference
from .models import User
from .purchase_stats import excluded_user_ids
from .subscription_promotion import EligibilityUnavailable, blocked_clause

EXPLICIT_MARKETING_SOURCE = "explicit_marketing_button_v1"
GLOBAL_OPT_OUT_SOURCE = "explicit_opt_out"


def query(settings, now):
    """Return one database query shared by queue construction and final checks."""
    try:
        exclusions = excluded_user_ids(settings)
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
    return select(User.id).where(
        User.ready.is_(True), User.id.not_in(exclusions), permission,
        not_(blocked_clause(User.id, now)),
    )


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
