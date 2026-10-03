"""Daily first-purchase reminders on the existing server/payment outbox.

No car-source calls, Telegram polling, new service, or catch-up broadcast.
BillingControl is the shared lock for owner confirmations, audience changes,
schedule changes, daily queue construction, and the final bounded send.
"""
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, billing_campaign, manual_checkout, manual_payments
from . import subscription_promotion as promotion
from . import tariff_reminder_audience as audience
from .billing_models import (BillingCampaign, BillingNotice, CampaignRecipient,
                             TariffReminderPreference, TariffReminderSchedule)
from .manual_payment_models import PaymentNotice

ID = "daily-first-purchase-v1"
PREFIX = ID + ":"
ZONE = ZoneInfo("Europe/Kyiv")
WINDOW_MINUTES = 30
SEND_INTERVAL = 2
LEASE = 60
LOG = logging.getLogger("uvicorn.error")
TEXT = (
    "🚘 Менше ручного пошуку — більше уваги до цікавих авто!\n\n"
    "AutoDeal шукає вигідні оголошення AUTO.RIA за твоїми фільтрами "
    "та надсилає їх у Telegram.\n\n"
    "💳 Підписка — 250 грн на 30 днів.\n"
    "Доступ активується після перевірки оплати адміністратором.\n\n"
    "Налаштуй пошук під себе та переглянь умови підписки 👇"
)
BUTTONS = [[{"text": "💳 Переглянути тариф", "callback_data": manual_checkout.PREFIX + "view"}],
           [{"text": "🔕 Не нагадувати", "callback_data": "ad-reminder:off"}]]


def next_morning(now):
    """Strictly future local 09:00; recompute each calendar day, including DST."""
    local = datetime.fromtimestamp(now, ZONE)
    day = local.date()
    candidate = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=9)
    if candidate.timestamp() <= now:
        candidate = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE).replace(hour=9)
    return candidate.timestamp()


def bounds(now):
    local = datetime.fromtimestamp(now, ZONE)
    start = datetime.combine(local.date(), datetime.min.time(), ZONE).replace(hour=9)
    return start.timestamp(), (start + timedelta(minutes=WINDOW_MINUTES)).timestamp()


def configured(settings):
    return (settings.manual_payment_review_enabled and settings.manual_payment_notices_enabled
            and billing.verified_admin(settings) and bool(settings.bot_token))


def readiness(db, settings):
    if not configured(settings):
        return False
    ctrl = billing.control(db)
    if not ctrl or not ctrl.enforce:
        return False
    try:
        manual_checkout.require_sales(db)
        return True
    except manual_payments.ReviewError:
        return False


def initialize(engine, settings, now=None):
    """Install once; neither restarting nor deploying again re-enables an opt-out."""
    if not configured(settings):
        return "not_installed"
    now = time.time() if now is None else now
    with Session(engine) as db, db.begin():
        billing.control(db, lock=True)
        row = db.get(TariffReminderSchedule, ID, populate_existing=True)
        if row is None:
            future = next_morning(now)
            enabled = readiness(db, settings)
            row = TariffReminderSchedule(id=ID, enabled=enabled, installed_at=now,
                first_run_at=future, next_run_at=future,
                last_result={} if enabled else {"status": "checkout_unavailable"})
            db.add(row)
        return "enabled" if row.enabled else "disabled"


def counts(db, campaign_id):
    return dict(db.execute(select(CampaignRecipient.state, func.count()).where(
        CampaignRecipient.campaign_id == campaign_id).group_by(CampaignRecipient.state)).all())


def result(db, campaign, now):
    states = counts(db, campaign.id)
    return {"campaign_id": campaign.id, "date_kyiv": campaign.id.removeprefix(PREFIX),
            "status": campaign.status, "selected": campaign.audience.get("selected", 0),
            "sent": states.get("sent", 0), "excluded": states.get("excluded", 0),
            "temporary_errors": states.get("retry", 0), "permanent_errors": states.get("failed", 0),
            "errors": states.get("retry", 0) + states.get("failed", 0),
            "uncertain": states.get("uncertain", 0), "expired": states.get("expired", 0),
            "pending": states.get("pending", 0) + states.get("sending", 0),
            "checked_at": now, "telegram_confirmation_is_read_receipt": False}


def snapshot(db, settings, now):
    row = db.get(TariffReminderSchedule, ID, populate_existing=True)
    current = db.get(BillingCampaign, row.last_campaign_id) if row and row.last_campaign_id else None
    upcoming = row.next_run_at if row and row.enabled else None
    return {"id": ID, "installed": row is not None, "enabled": bool(row and row.enabled),
            "timezone": "Europe/Kyiv", "morning_window": "09:00–09:30",
            "next_run_at": upcoming,
            "next_run_kyiv": datetime.fromtimestamp(upcoming, ZONE).isoformat() if upcoming else None,
            "first_run_at": row.first_run_at if row else None,
            "eligible_recipients": audience.count(db, settings, now),
            "text": TEXT, "buttons": [b[0]["text"] for b in BUTTONS],
            "last_result": result(db, current, now) if current else (row.last_result if row else {}),
            "heartbeat": row.heartbeat if row else None,
            "checkout_ready": readiness(db, settings),
            "execution": "existing backend manual-payment notice executor"}


def finish(db, schedule, campaign, now, status, reason):
    # A durable sending claim with no persisted result is uncertain, even when
    # the process crashed before transport. Safety takes precedence over replay.
    db.execute(update(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.state == "sending").values(state="uncertain", error="unfinished_send_claim"))
    db.execute(update(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.state.in_(("pending", "retry"))).values(
            state="excluded" if status == "cancelled" else "expired", error=reason))
    campaign.status = status
    db.flush()
    schedule.last_result = result(db, campaign, now)


def set_enabled(engine, settings, actor, enabled, now, *, update_id=None):
    manual_payments.owner(settings, actor)  # Before querying any audience.
    if type(enabled) is not bool:
        raise manual_payments.ReviewError("invalid_enabled", 422)
    if update_id is not None and (type(update_id) is not int or not 0 <= update_id < 2**63):
        raise manual_payments.ReviewError("invalid_update_id", 422)
    with Session(engine) as db, db.begin():
        billing.control(db, lock=True)
        row = db.get(TariffReminderSchedule, ID, populate_existing=True)
        if row is None:
            raise manual_payments.ReviewError("reminder_schedule_not_installed", 503)
        if update_id is not None and update_id <= row.admin_update_id:
            return snapshot(db, settings, now)
        if enabled and not readiness(db, settings):
            raise manual_payments.ReviewError("reminder_checkout_unavailable", 503)
        if enabled and not row.enabled:
            row.next_run_at = next_morning(now)
        row.enabled = enabled
        if update_id is not None:
            row.admin_update_id = update_id
        if not enabled:
            for campaign in db.scalars(select(BillingCampaign).where(
                    BillingCampaign.id.startswith(PREFIX), BillingCampaign.status == "running")):
                finish(db, row, campaign, now, "cancelled", "campaign_disabled")
    # An unavailable audience count must not roll back an administrator's stop.
    with Session(engine) as db:
        return snapshot(db, settings, now)


def set_preference(engine, settings, uid, enabled, update_id, now):
    if type(uid) is not int or not 0 < uid < 2**52 or type(enabled) is not bool:
        raise ValueError("Invalid reminder preference")
    if type(update_id) is not int or not 0 <= update_id < 2**63:
        raise ValueError("Invalid preference update")
    with Session(engine) as db, db.begin():
        billing.control(db, lock=True)
        row = db.get(TariffReminderPreference, uid, populate_existing=True)
        if row is None:
            row = TariffReminderPreference(user_id=uid, enabled=enabled, update_id=update_id, at=now)
            db.add(row)
        elif update_id > row.update_id:
            row.enabled, row.update_id, row.at = enabled, update_id, now
        return row.enabled


def high_priority(db, now):
    return (billing_campaign.high_priority(db, now)
            or bool(db.scalar(select(PaymentNotice.id).where(
                PaymentNotice.state.in_(("pending", "retry", "sending"))).limit(1)))
            or bool(db.scalar(select(BillingNotice.id).where(or_(
                BillingNotice.kind.in_(("support", "support_reply")), BillingNotice.id.startswith("refund-review:")),
                BillingNotice.state.in_(("pending", "retry", "sending"))).limit(1))))


def dispatch_guard(db, settings, campaign, now):
    schedule = db.get(TariffReminderSchedule, ID, populate_existing=True)
    return bool(schedule and schedule.enabled and readiness(db, settings)
                and campaign.not_before <= now < campaign.deadline
                and campaign.id == PREFIX + datetime.fromtimestamp(now, ZONE).date().isoformat()
                and not high_priority(db, now))


def record_pace(db, campaign, item, now):
    # Persist the global post-response pause while still holding the same lock
    # as transport; another worker cannot claim during a gap before backoff.
    campaign.next_send = max(campaign.next_send, item.retry_at, now + SEND_INTERVAL)


def tick(engine, settings, request, now=None, *, clock=None):
    """One bounded turn on the existing executor; never performs source API calls."""
    if not callable(request):
        raise ValueError("Explicit reminder transport required")
    clock = clock or (time.time if now is None else lambda: now)
    now = clock()
    with Session(engine) as db, db.begin():
        billing.control(db, lock=True)
        schedule = db.get(TariffReminderSchedule, ID, populate_existing=True)
        if schedule is None:
            return "not_installed"
        schedule.heartbeat = now
        for previous in db.scalars(select(BillingCampaign).where(BillingCampaign.id.startswith(PREFIX),
                BillingCampaign.status == "running", BillingCampaign.deadline <= now)):
            finish(db, schedule, previous, now, "expired", "morning_window_closed")
        if not schedule.enabled:
            return "disabled"
        start, end = bounds(now)
        if now >= schedule.next_run_at:
            # Missed days advance directly to the next future morning. No backfill.
            due_today = datetime.fromtimestamp(schedule.next_run_at, ZONE).date() <= datetime.fromtimestamp(now, ZONE).date()
            if due_today and start <= now < end:
                if not readiness(db, settings):
                    return "checkout_unavailable"
                key = PREFIX + datetime.fromtimestamp(now, ZONE).date().isoformat()
                campaign = db.get(BillingCampaign, key)
                if campaign is None:
                    users = audience.ids(db, settings, now)  # One authoritative bulk query.
                    campaign = BillingCampaign(id=key, not_before=start, deadline=end, timezone="Europe/Kyiv",
                        content={"text": TEXT, "buttons": BUTTONS}, audience={"selected": len(users), "at": now},
                        status="running", blockers=[])
                    db.add(campaign)
                    db.add_all([CampaignRecipient(campaign_id=key, user_id=uid, state="pending") for uid in users])
                schedule.last_campaign_id = key
            schedule.next_run_at = next_morning(now)
        campaign = db.get(BillingCampaign, schedule.last_campaign_id) if schedule.last_campaign_id else None
        if not campaign or campaign.status != "running":
            return "scheduled"
        campaign.heartbeat = now
        billing_campaign.recover_uncertain(db, now, campaign_id=campaign.id)
        if not dispatch_guard(db, settings, campaign, now):
            return "yielding_to_service" if high_priority(db, now) else "deferred"
        if campaign.next_send > now:
            return "paced"
        item = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.state.in_(("pending", "retry")), CampaignRecipient.retry_at <= now)
            .order_by(CampaignRecipient.user_id).limit(1))
        if item is None:
            if not db.scalar(select(CampaignRecipient.user_id).where(CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.state.in_(("pending", "retry", "sending"))).limit(1)):
                finish(db, schedule, campaign, now, "complete", "complete")
                return "complete"
            return "waiting_retry"
        if not audience.eligible(db, settings, item.user_id, now):
            item.state, item.error = "excluded", "recipient_no_longer_eligible"
            return "excluded"
        key, uid = campaign.id, item.user_id
        prior_state, prior_attempted_at = item.state, item.attempted_at
        item.state, item.attempted_at = "sending", now
        campaign.next_send = now + SEND_INTERVAL
        reservation = secrets.token_hex(16)
        campaign.lease_token = reservation
        payload = {"chat_id": uid, "text": campaign.content["text"], "allow_paid_broadcast": False,
                   "reply_markup": {"inline_keyboard": campaign.content["buttons"]}}
        # Commit the claim before any network I/O; another process sees 'sending'.
    # A delayed earlier worker must not bypass another worker's later send or
    # 429 pause. Only the latest durable reservation may start transport.
    def reserved_guard(db, active_settings, active_campaign, current):
        return (active_campaign.lease_token == reservation
                and dispatch_guard(db, active_settings, active_campaign, current))
    return promotion.dispatch_claim(engine, settings, key, uid, request, payload, now,
        prior_state=prior_state, prior_attempted_at=prior_attempted_at,
        eligibility_check=audience.eligible, dispatch_guard=reserved_guard, clock=clock,
        result_callback=record_pace)


def log_progress(engine, settings, outcome):
    """Private aggregate evidence that the actual server executor is ticking."""
    try:
        with Session(engine) as db:
            data = snapshot(db, settings, time.time())
        data.pop("text")
        data.pop("buttons")
        data.update(outcome=outcome, checked_at=datetime.now(timezone.utc).isoformat())
        LOG.info("Daily tariff reminder scheduler %s", json.dumps(data, ensure_ascii=False, sort_keys=True))
    except (SQLAlchemyError, promotion.EligibilityUnavailable):
        LOG.error("Daily tariff reminder scheduler storage unavailable; no audience assumed")
