"""Explicit, durable bank-subscription rollout and one-time transition notice."""
import logging
import os
import time
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import billing, billing_campaign, manual_checkout, manual_payments as m, subscription_promotion
from .billing_models import BillingCampaign, CampaignRecipient, MarketingConsent, BillingOrder, Entitlement
from .models import User, StarsTestOrder, MonitorControl, SourceProbe, Delivery, DeliveryTiming

CAMPAIGN = "manual-card-launch-20261002-v1"
# A monitor/delivery transaction can commit just after the caller captured now,
# including while it waited for the billing lock. Do not treat that as stale.
READINESS_FUTURE_SKEW = 5
COPY = ("🚘 <b>Твоє наступне авто — у повідомленні AutoDeal</b>\n\n"
        "AutoDeal шукає відповідні авто за твоїми фільтрами, допомагає помічати вигідні пропозиції "
        "та надсилає сповіщення в Telegram.\n\n"
        "<b>Відкриваємо платну підписку: 250 гривень за 30 днів</b>.\n\n"
        "Твої фільтри збережені. Переглянь умови та оформи доступ кнопкою нижче. "
        "Автоматичних списань немає.")
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())


def inventory(db, now):
    counts = dict(active=0, expired=0, without_access=0, active_legacy=0, active_entitlements=0)
    for uid in db.scalars(select(User.id)):
        until = billing.expiry(db, uid)
        counts["active" if until > now else "expired" if until > 0 else "without_access"] += 1
    counts["active_legacy"] = db.scalar(select(func.count(func.distinct(StarsTestOrder.user_id))).where(
        StarsTestOrder.state.in_(("paid", "refund_sending", "refund_uncertain")), StarsTestOrder.paid_until > now)) or 0
    counts["active_entitlements"] = db.scalar(select(func.count()).select_from(Entitlement).where(Entitlement.expires_at > now)) or 0
    return counts


def eligible(db, uid, now=None):
    return subscription_promotion.eligible(db, uid, now)


def snapshot(db, now):
    row = db.get(BillingCampaign, CAMPAIGN)
    ctrl = billing.control(db)
    counts = dict(db.execute(select(CampaignRecipient.state, func.count()).where(
        CampaignRecipient.campaign_id == CAMPAIGN).group_by(CampaignRecipient.state)).all())
    checkout_error = None
    try:
        manual_checkout.require_sales(db)
    except m.ReviewError as exc:
        checkout_error = exc.code
    return {"checkout_error": checkout_error, "offer_valid": bool(ctrl and manual_checkout.offer_valid(ctrl.offer)),
            "public_enabled": m.public_creation_allowed(),
            "campaign": CAMPAIGN, "status": row.status if row else "not_prepared",
            "sales": bool(ctrl and ctrl.sales), "enforce": bool(ctrl and ctrl.enforce),
            "payment_method": (ctrl.offer or {}).get("method") if ctrl else None,
            "recipient_states": counts, "access": inventory(db, now),
            "started_at": row.not_before if row and row.status != "prepared" else None}


def launch_errors(db, settings, now):
    errors = []
    if not settings.manual_payment_review_enabled or not settings.manual_payment_notices_enabled or not m.public_creation_allowed():
        errors.append("manual_features_disabled")
    if not billing.verified_admin(settings):
        errors.append("owner_unverified")
    if not os.getenv("MANUAL_PAYMENT_BACKUP_REFERENCE", "").startswith("libfile_"):
        errors.append("verified_private_backup_missing")
    if os.getenv("MANUAL_PAYMENT_STORAGE_READY") != "true":
        errors.append("storage_continuity_not_verified")
    if not settings.live or not settings.monitor_enabled:
        errors.append("delivery_disabled")
    heartbeat = db.get(MonitorControl, "pilot")
    if not heartbeat or not -READINESS_FUTURE_SKEW <= now-heartbeat.heartbeat <= 180:
        errors.append("monitor_stale")
    # Use the authoritative existing probe ID, not a guessed deployment name.
    from . import telegram_setup
    hook = db.get(SourceProbe, telegram_setup.PROBE_ID)
    if not hook or hook.status != "configured":
        errors.append("webhook_unavailable")
    last = db.scalar(select(func.max(DeliveryTiming.accepted_at)).join(
        Delivery, Delivery.id == DeliveryTiming.delivery_id).where(Delivery.state == "sent"))
    if not last or not -READINESS_FUTURE_SKEW <= now-last <= 86400:
        errors.append("car_delivery_unverified")
    if settings.ria_recent_publications_enabled:
        probe = db.get(SourceProbe, "recent-publications-v1")
        success = (probe.result.get("last_success_at") or 0) if probe else 0
        if not -READINESS_FUTURE_SKEW <= now-success <= 7200:
            errors.append("discovery_stale")
    if db.scalar(select(BillingOrder.id).where(BillingOrder.state == "pending").limit(1)):
        errors.append("existing_invoice_pending")
    try:
        if manual_checkout.subscription_preview.receiving_profile() is None:
            errors.append("recipient_missing")
    except Exception:
        errors.append("recipient_invalid")
    return errors


def initialize(engine, settings, now=None):
    stage = os.getenv("MANUAL_PAYMENT_LAUNCH_STAGE", "")
    if stage not in ("prepare", "live"):
        return
    if not settings.manual_payment_review_enabled or not billing.verified_admin(settings):
        LOG.info("Manual launch blocked: owner or review configuration unavailable")
        return
    now = time.time() if now is None else now
    with m.mutation(engine, settings) as db:
        ctrl = billing.control(db)
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is None:
            if stage != "prepare" or ctrl.sales or ctrl.enforce:
                LOG.info("Manual launch blocked: prepare before launch")
                return
            ctrl.offer = dict(manual_checkout.OFFER)
            row = BillingCampaign(id=CAMPAIGN, not_before=now, deadline=now+86400,
                timezone="Europe/Kyiv", content={"text": COPY, "version": manual_checkout.TERMS_VERSION},
                audience={"rule": "owner_authorized_service_transition", "exclude": ["stopped", "blocked", "explicit_opt_out"],
                          "before_access": inventory(db, now)}, status="prepared", blockers=[], heartbeat=now)
            db.add(row)
        elif stage == "live" and row.status == "prepared":
            errors = launch_errors(db, settings, now)
            if errors:
                row.blockers = errors
                LOG.info("Manual launch blocked %s", json.dumps(errors))
                return
            ctrl.offer = dict(manual_checkout.OFFER)
            ctrl.sales = ctrl.enforce = True
            row.status, row.blockers, row.not_before, row.deadline = "running", [], now, now+86400
            ids = [uid for uid in db.scalars(select(User.id)) if eligible(db, uid, now)]
            row.audience = {**row.audience, "selected_at": now, "selected": len(ids),
                            "before_access": inventory(db, now)}
            for uid in ids:
                db.add(CampaignRecipient(campaign_id=CAMPAIGN, user_id=uid, state="pending"))
        elif (stage == "live" and row.status == "paused"
              and os.getenv("MANUAL_PAYMENT_RESUME_PENDING") == CAMPAIGN
              and not row.audience.get("pending_resume_at")):
            # Explicit, one-use recovery of an entirely unattempted queue only.
            # Never recreate recipients, reset send states, or extend the deadline.
            errors = launch_errors(db, settings, now)
            try:
                manual_checkout.require_sales(db)
            except m.ReviewError as exc:
                errors.append(exc.code)
            if not ctrl.enforce:
                errors.append("access_enforcement_paused")
            if now > row.deadline:
                errors.append("broadcast_window_missed")
            if db.scalar(select(CampaignRecipient.user_id).where(
                    CampaignRecipient.campaign_id == CAMPAIGN,
                    (CampaignRecipient.state != "pending") | (CampaignRecipient.attempted_at != 0)).limit(1)):
                errors.append("queue_already_attempted")
            if errors:
                row.blockers = errors
                LOG.info("Manual resume blocked %s", json.dumps(errors))
            else:
                row.status, row.blockers = "running", []
                row.audience = {**row.audience, "pending_resume_at": now}
        # Restarts never create a second launch or repeat a consumed recovery.
    with Session(engine) as db:
        LOG.info("Manual launch %s", json.dumps(snapshot(db, now), sort_keys=True))


def log_progress(engine, outcome):
    """Aggregate-only diagnostics: no recipient identities or message bodies."""
    with Session(engine) as db:
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is None or row.status != "running":
            return
        counts = dict(db.execute(select(CampaignRecipient.state, func.count()).where(
            CampaignRecipient.campaign_id == CAMPAIGN).group_by(CampaignRecipient.state)).all())
        from .models import BotReply
        priority = {}
        for label, model in (("car_deliveries", Delivery), ("bot_replies", BotReply)):
            priority[label] = db.scalar(select(func.count()).select_from(model).where(
                model.state.in_(("pending", "sending")))) or 0
        LOG.info("Manual launch progress %s", json.dumps({"status": row.status,
            "last_tick": outcome, "recipient_states": counts, "priority_work": priority}, sort_keys=True))


def tick(engine, settings, request, now=None):
    now = time.time() if now is None else now
    with m.mutation(engine, settings) as db:
        row = db.get(BillingCampaign, CAMPAIGN)
        if row is None or row.status != "running":
            return "inactive"
        ctrl = billing.control(db)
        try:
            manual_checkout.require_sales(db)
            checkout_ready = True
        except m.ReviewError as exc:
            checkout_ready = False
            row.blockers = [exc.code]
        if not ctrl.enforce or not checkout_ready:
            row.status = "paused"
            if not ctrl.enforce:
                row.blockers = ["access_enforcement_paused"]
            LOG.info("Manual launch paused %s", json.dumps(row.blockers))
            return "paused"
        billing_campaign.recover_uncertain(db, now)
        if now > row.deadline:
            row.status = "expired"
            return "expired"
        if now < row.next_send or billing_campaign.high_priority(db, now):
            return "yielding"
        item = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == CAMPAIGN,
            CampaignRecipient.state.in_(("pending", "retry")), CampaignRecipient.retry_at <= now)
            .order_by(CampaignRecipient.user_id).limit(1))
        if item is None:
            outstanding = db.scalar(select(CampaignRecipient.user_id).where(CampaignRecipient.campaign_id == CAMPAIGN,
                CampaignRecipient.state.in_(("pending", "retry", "sending"))).limit(1))
            if outstanding is None:
                row.status = "complete"
                LOG.info("Manual launch %s", json.dumps(snapshot(db, now), sort_keys=True))
            return "empty"
        if not eligible(db, item.user_id, now):
            item.state, item.error = "excluded", "recipient_opted_out_or_stopped"
            return "excluded"
        uid = item.user_id
        prior_state, prior_attempted_at = item.state, item.attempted_at
        item.state, item.attempted_at = "sending", now
        row.next_send = now+2
        payload = {"chat_id": uid, "text": COPY, "parse_mode": "HTML", "allow_paid_broadcast": False,
                   "reply_markup": {"inline_keyboard": [[{"text": "Оформити підписку", "callback_data": manual_checkout.PREFIX+"terms"}]]}}
    return subscription_promotion.dispatch_claim(engine, settings, CAMPAIGN, uid, request, payload, now,
        prior_state=prior_state, prior_attempted_at=prior_attempted_at)
