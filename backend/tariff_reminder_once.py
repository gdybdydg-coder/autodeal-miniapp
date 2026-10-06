"""One explicit owner-requested reminder; no change to daily consent or schedule.

Only the protected Render arm token can install this dated campaign. The
durable row is consumed once, including uncertain outcomes and blocked starts.
"""
import os
import secrets
from datetime import datetime

from sqlalchemy import and_, case, exists, func, not_, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, billing_campaign, tariff_reminders as daily
from . import tariff_reminder_audience as audience
from . import subscription_promotion as promotion
from .billing_models import BillingCampaign, CampaignRecipient, MarketingConsent, TariffReminderSchedule
from .models import User

ID = "tariff-reminder-owner-once-20261006:095843"
ARM_ENV = "TARIFF_REMINDER_OWNER_ONCE"
DAY_PREFIX = "tariff-reminder-owner-once-20261006:"
EXPIRES = datetime.fromisoformat("2026-10-06T11:00:00+03:00").timestamp()
WINDOW = 1800


def armed():
    return os.getenv(ARM_ENV) == ID


def checks(settings, now):
    conditions = audience.clauses(settings, now)
    missing_permission = conditions.pop("missing_explicit_consent")
    # This one-time instruction permits clients with no recorded preference.
    # It cannot override any recorded refusal or invent a daily opt-in.
    conditions["recorded_permission_denied"] = and_(missing_permission,
        exists().where(MarketingConsent.user_id == User.id, MarketingConsent.allowed.is_(False)))
    day = datetime.fromtimestamp(now, daily.ZONE).date().isoformat()
    conditions["already_queued_or_attempted_today"] = exists().where(
        CampaignRecipient.user_id == User.id, CampaignRecipient.campaign_id != ID,
        or_(CampaignRecipient.campaign_id == daily.PREFIX+day,
            CampaignRecipient.campaign_id.startswith(DAY_PREFIX)),
        CampaignRecipient.state.in_(("pending", "retry", "sending", "sent", "uncertain")))
    return {"invalid_private_recipient": User.id <= 0, **conditions}


def query(settings, now):
    return select(User.id).where(*(not_(value) for value in checks(settings, now).values()))


def eligible(db, settings, uid, now):
    try:
        return db.scalar(query(settings, now).where(User.id == uid)) is not None
    except SQLAlchemyError:
        raise promotion.EligibilityUnavailable("owner_once_audience_unavailable") from None


def cohort(db, settings, now):
    reason = case(*[(condition, name) for name, condition in checks(settings, now).items()],
                  else_="eligible").label("reason")
    grouped = select(reason).select_from(User).subquery()
    rows = db.execute(select(grouped.c.reason, func.count()).group_by(grouped.c.reason)).all()
    reasons = {name: count for name, count in rows if name != "eligible"}
    return {"total_users": sum(count for _, count in rows),
            "eligible_users": sum(count for name, count in rows if name == "eligible"),
            "excluded_by_primary_reason": reasons,
            "basis": "explicit_owner_once_no_recorded_refusal_current_unpaid", "observed_at": now}


def summary(db):
    campaign = db.get(BillingCampaign, ID)
    if campaign is None:
        return {"id": ID, "armed": armed(), "installed": False, "status": "not_installed"}
    result = daily.result(db, campaign, campaign.heartbeat)
    return {**result, "id": ID, "date_kyiv": "2026-10-06", "installed": True, "armed": armed(),
            "audience_at_selection": campaign.audience.get("summary"),
            "first_telegram_accepted_at": campaign.audience.get("first_telegram_accepted_at"),
            "last_telegram_accepted_at": campaign.audience.get("last_telegram_accepted_at"),
            "deadline": campaign.deadline}


def finish(db, campaign, status, reason):
    db.execute(update(CampaignRecipient).where(CampaignRecipient.campaign_id == ID,
        CampaignRecipient.state == "sending").values(state="uncertain", error="unfinished_send_claim"))
    db.execute(update(CampaignRecipient).where(CampaignRecipient.campaign_id == ID,
        CampaignRecipient.state.in_(("pending", "retry"))).values(
            state="excluded" if status == "cancelled" else "expired", error=reason))
    campaign.status = status


def guard(db, settings, campaign, now):
    schedule = db.get(TariffReminderSchedule, daily.ID, populate_existing=True)
    return bool(armed() and campaign.id == ID and schedule and schedule.enabled
                and daily.readiness(db, settings) and campaign.not_before <= now < campaign.deadline
                and now < EXPIRES and not daily.high_priority(db, now))


def record_pace(db, campaign, item, now):
    daily.record_pace(db, campaign, item, now)
    if item.state == "sent" and item.message_id is not None:
        campaign.audience = {**campaign.audience,
            "first_telegram_accepted_at": campaign.audience.get("first_telegram_accepted_at", now),
            "last_telegram_accepted_at": now}


def tick(engine, settings, request, now, *, clock):
    with Session(engine) as db, db.begin():
        if not armed():
            existing = db.get(BillingCampaign, ID)
            if existing is None or existing.status != "running":
                return "disabled"
        billing.control(db, lock=True)
        schedule = db.get(TariffReminderSchedule, daily.ID, populate_existing=True)
        campaign = db.get(BillingCampaign, ID, populate_existing=True)
        if campaign is None:
            if not armed() or now >= EXPIRES or datetime.fromtimestamp(now, daily.ZONE).date().isoformat() != "2026-10-06":
                return "not_armed_for_this_date"
            if not schedule or not schedule.enabled or not daily.readiness(db, settings):
                return "not_ready"
            users = list(db.scalars(query(settings, now).order_by(User.id)))
            campaign = BillingCampaign(id=ID, not_before=now, deadline=min(now+WINDOW, EXPIRES),
                timezone="Europe/Kyiv", status="running", blockers=[], heartbeat=now,
                content={"text": daily.TEXT, "renewal_text": daily.RENEWAL_TEXT, "buttons": daily.BUTTONS},
                audience={"selected": len(users), "at": now, "summary": cohort(db, settings, now),
                    "authorization": "owner_chat_2026-10-06_09:58:43_Europe_Kyiv"})
            db.add(campaign)
            db.add_all([CampaignRecipient(campaign_id=ID, user_id=uid, state="pending") for uid in users])
            db.flush()
        if campaign.status != "running":
            return campaign.status
        campaign.heartbeat = now
        if not armed() or not schedule or not schedule.enabled:
            finish(db, campaign, "cancelled", "owner_campaign_disabled")
            return "cancelled"
        if now >= campaign.deadline:
            finish(db, campaign, "expired", "one_time_window_closed")
            return "expired"
        billing_campaign.recover_uncertain(db, now, campaign_id=ID)
        if not guard(db, settings, campaign, now):
            return "yielding_to_service"
        if now < campaign.next_send:
            return "paced"
        item = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == ID,
            CampaignRecipient.state.in_(("pending", "retry")), CampaignRecipient.retry_at <= now)
            .order_by(CampaignRecipient.user_id).limit(1))
        if item is None:
            outstanding = db.scalar(select(CampaignRecipient.user_id).where(
                CampaignRecipient.campaign_id == ID,
                CampaignRecipient.state.in_(("pending", "retry", "sending"))).limit(1))
            if outstanding is None:
                campaign.status = "complete"
            return campaign.status
        if not eligible(db, settings, item.user_id, now):
            item.state, item.error = "excluded", "recipient_no_longer_eligible"
            return "excluded"
        uid, prior_state, prior_attempted_at = item.user_id, item.state, item.attempted_at
        item.state, item.attempted_at = "sending", now
        campaign.next_send = now+daily.SEND_INTERVAL
        reservation = secrets.token_hex(16)
        campaign.lease_token = reservation
        copy = campaign.content["renewal_text"] if audience.former_buyer(db, uid) else campaign.content["text"]
        payload = {"chat_id": uid, "text": copy, "allow_paid_broadcast": False,
                   "reply_markup": {"inline_keyboard": campaign.content["buttons"]}}
    def reserved_guard(db, active_settings, active_campaign, current):
        return active_campaign.lease_token == reservation and guard(db, active_settings, active_campaign, current)
    return promotion.dispatch_claim(engine, settings, ID, uid, request, payload, now,
        prior_state=prior_state, prior_attempted_at=prior_attempted_at,
        eligibility_check=eligible, dispatch_guard=reserved_guard, clock=clock, result_callback=record_pace)
