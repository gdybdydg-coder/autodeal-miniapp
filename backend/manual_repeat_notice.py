"""Explicitly armed, one-time reminder. Never grants access or changes sales.

The original launch is immutable here. Claims are committed before network I/O;
only explicit Telegram rate-limit responses may be retried automatically.
"""
import json
import logging
import os
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import billing, billing_campaign, manual_checkout, manual_launch, manual_payments as m
from . import manual_repeat_preflight
from .billing_models import BillingCampaign, CampaignRecipient, MarketingConsent
from .models import User

CAMPAIGN = "manual-card-reminder-20261002-v1"
ARM_ENV = "MANUAL_PAYMENT_REPEAT_NOTICE"
COPY = ("🔒 <b>AutoDeal: оголошення лише за підпискою</b>\n\n"
        "Без активної оплаченої підписки нові оголошення не надходитимуть.\n\n"
        "💳 <b>250 грн / 30 днів</b>\n"
        "Оплати за реквізитами та натисни «Я оплатив». Після перевірки зарахування власник відкриє доступ.\n\n"
        "Фільтри збережені. Якщо вже оплатив або маєш активну підписку — повторно не сплачуй.")
BUTTON = "💳 Відкрити оплату"
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())


def eligible(db, uid):
    user = db.get(User, uid)
    consent = db.get(MarketingConsent, uid)
    if not user or not user.ready or (consent and (not consent.allowed or consent.blocked)):
        return False
    # A previous explicit 403 is authoritative even when there is no separate
    # consent row. Do not create consent or mutate any previous campaign.
    return db.scalar(select(CampaignRecipient.campaign_id).where(
        CampaignRecipient.user_id == uid, CampaignRecipient.state == "failed",
        CampaignRecipient.error == "403").limit(1)) is None


def readiness(db, settings, now):
    errors = manual_launch.launch_errors(db, settings, now)
    try:
        manual_checkout.require_sales(db)
    except m.ReviewError as exc:
        errors.append(exc.code)
    ctrl = billing.control(db)
    if not ctrl or not ctrl.enforce:
        errors.append("access_enforcement_paused")
    return list(dict.fromkeys(errors))


def _summary(db, row):
    db.flush()
    counts = dict(db.execute(select(CampaignRecipient.state, func.count()).where(
        CampaignRecipient.campaign_id == CAMPAIGN).group_by(CampaignRecipient.state)).all())
    return {"campaign": CAMPAIGN, "status": row.status,
            "recipient_states": counts, "selected": row.audience.get("selected", 0),
            "excluded": row.audience.get("excluded", 0), "blocker_count": len(row.blockers)}


def _log(db, row, event):
    LOG.info("Manual reminder %s %s", event, json.dumps(_summary(db, row), sort_keys=True))


def initialize(engine, settings, now=None):
    if os.getenv(ARM_ENV) != CAMPAIGN:
        return "disabled"
    now = time.time() if now is None else now
    with m.mutation(engine, settings) as db:
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is not None:
            # Presence is the durable one-shot token, including a failed or
            # paused launch. A restart never rebuilds its audience or re-arms it.
            return row.status
        errors = readiness(db, settings, now)
        if not manual_repeat_preflight.ready(db):
            errors.append("owner_requisites_unverified")
        ids = []
        total = db.scalar(select(func.count()).select_from(User)) or 0
        if not errors:
            ids = [uid for uid in db.scalars(select(User.id)) if eligible(db, uid)]
        row = BillingCampaign(id=CAMPAIGN, not_before=now, deadline=now+86400,
            timezone="Europe/Kyiv", content={"text": COPY, "button": BUTTON,
                "callback_data": manual_checkout.PREFIX+"view"},
            audience={"rule": "owner_authorized_one_time_service_reminder",
                "selected_at": now, "selected": len(ids), "excluded": total-len(ids),
                "exclude": ["stopped", "blocked", "explicit_opt_out", "prior_403"]},
            status="blocked" if errors else "running", blockers=errors, heartbeat=now)
        db.add(row)
        for uid in ids:
            db.add(CampaignRecipient(campaign_id=CAMPAIGN, user_id=uid, state="pending"))
        _log(db, row, "initial")
        return row.status


def log_progress(engine, outcome):
    with Session(engine) as db:
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is not None and row.status == "running":
            # Outcome is deliberately not logged: callers may accidentally pass
            # an exception or provider response instead of a fixed state code.
            _log(db, row, "progress")


def tick(engine, settings, request, now=None):
    if os.getenv(ARM_ENV) != CAMPAIGN:
        return "disabled"
    if not callable(request):
        raise TypeError("An explicit reminder sender is required")
    now = time.time() if now is None else now
    with m.mutation(engine, settings) as db:
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is None or row.status != "running":
            return "inactive"
        errors = readiness(db, settings, now)
        if errors:
            row.status, row.blockers = "paused", errors
            _log(db, row, "paused")
            return "paused"
        # Recovery is strictly scoped: never touch the original announcement.
        billing_campaign.recover_uncertain(db, now, campaign_id=CAMPAIGN)
        row.heartbeat = now
        if now > row.deadline:
            row.status = "expired"
            _log(db, row, "expired")
            return "expired"
        if now < row.next_send or billing_campaign.high_priority(db, now):
            return "yielding"
        item = db.scalar(select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == CAMPAIGN,
            CampaignRecipient.state.in_(("pending", "retry")), CampaignRecipient.retry_at <= now)
            .order_by(CampaignRecipient.user_id).limit(1))
        if item is None:
            outstanding = db.scalar(select(CampaignRecipient.user_id).where(
                CampaignRecipient.campaign_id == CAMPAIGN,
                CampaignRecipient.state.in_(("pending", "retry", "sending"))).limit(1))
            if outstanding is None:
                row.status = "complete"
                _log(db, row, "complete")
            return "empty"
        if not eligible(db, item.user_id):
            item.state, item.error = "excluded", "recipient_no_longer_eligible"
            return "excluded"
        uid = item.user_id
        item.state, item.attempted_at = "sending", now
        row.next_send = now+2
        payload = {"chat_id": uid, "text": row.content["text"], "parse_mode": "HTML",
            "allow_paid_broadcast": False, "reply_markup": {"inline_keyboard": [[{
                "text": row.content["button"], "callback_data": row.content["callback_data"]}]]}}
    try:
        result = request(settings.bot_token, "sendMessage", payload, timeout=5)
    except Exception:
        result = {}
    if not isinstance(result, dict):
        result = {}
    with m.mutation(engine, settings) as db:
        item = db.get(CampaignRecipient, (CAMPAIGN, uid))
        if item.state != "sending":
            return "uncertain"
        billing_campaign.apply_send_result(item, result, now)
        if result.get("error_code") == 403:
            consent = db.get(MarketingConsent, uid)
            if consent:
                consent.blocked = True
        return item.state
