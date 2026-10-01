"""Durable launch/check and consent-only broadcast on the existing web service.

The DB is the clock/state authority. A five-second polling coroutine is only the
executor; restarts do not recreate or re-arm the campaign. Uncertain sends are
never automatically replayed. All network requests explicitly disallow paid fanout.
"""
import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import billing, telegram_setup
from .models import BotReply, Delivery, DeliveryTiming, MonitorControl, SourceProbe, User
from .billing_models import (BillingControl, BillingCampaign, CampaignRecipient,
                             MarketingConsent, BillingNotice, BillingOrder, Entitlement)

CAMPAIGN = "autodeal-launch-20261002-0900-kyiv"
WHEN = datetime(2026, 10, 2, 9, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
VERSION = "20261002-v1"
LEASE = 60
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
COPY_A = ("🚘 <b>Твоє наступне авто — у повідомленні AutoDeal</b>\n\n"
          "AutoDeal шукає відповідні авто за твоїми фільтрами, допомагає помічати вигідні пропозиції "
          "та надсилає сповіщення в Telegram.\n\n"
          "Відкриваємо платну підписку: <b>{price} за 30 днів</b>.\n"
          "Твої фільтри збережені. Переглянь умови та оформи доступ кнопкою нижче. "
          "Автоматичних списань немає.\n\nВідмова від рекламних повідомлень: /marketing_off")
COPY_B = ("🔎 <b>Шукай авто зі своїми умовами — отримуй сповіщення в Telegram</b>\n\n"
          "Обери марку, бюджет та області. AutoDeal відстежуватиме відповідні оголошення "
          "й допомагатиме знаходити вигідні варіанти.\n\n"
          "Запускаємо платний доступ: <b>{price} за 30 днів</b>. "
          "Збережені фільтри залишаються. Умови й оформлення — за кнопкою нижче. "
          "Без автоматичних списань.\n\nВідмова від рекламних повідомлень: /marketing_off")


def content(offer):
    price = f"{offer['stars']} ⭐" if type(offer.get("stars")) is int else "250 грн"
    return {"version": VERSION, "text": COPY_A.format(price=price), "media": None,
            "price_verified": not billing.offer_errors(offer),
            "button": "Оформити підписку", "callback_data": billing.PREFIX+"terms"}


def count_audience(db, now):
    total = db.scalar(select(func.count()).select_from(User)) or 0
    eligible = [uid for uid in db.scalars(select(User.id).join(MarketingConsent, MarketingConsent.user_id == User.id)
                 .where(User.ready.is_(True), MarketingConsent.allowed.is_(True), MarketingConsent.blocked.is_(False)))
                if billing.expiry(db, uid) <= now]
    return total, eligible


def snapshot(db, settings, now):
    row = db.get(BillingCampaign, CAMPAIGN)
    ctrl = billing.control(db)
    total, audience = count_audience(db, now)
    counts = dict(db.execute(select(CampaignRecipient.state, func.count()).where(
        CampaignRecipient.campaign_id == CAMPAIGN).group_by(CampaignRecipient.state)).all())
    preview = db.get(BillingNotice, "launch-preview:"+VERSION)
    return {"campaign_id": CAMPAIGN, "status": row.status if row else "not_installed",
            "not_before": "2026-10-02T09:00:00+03:00", "timezone": "Europe/Kyiv",
            "deadline": "2026-10-02T09:15:00+03:00", "heartbeat": row.heartbeat if row else 0,
            "blockers": row.blockers if row else [], "admin_matches_protected_config": billing.verified_admin(settings),
            "sales": bool(ctrl and ctrl.sales), "enforce": bool(ctrl and ctrl.enforce),
            "existing_users": total, "eligible_now": len(audience), "excluded_now": total-len(audience),
            "audience_snapshot": row.audience if row else {}, "recipient_states": counts, "preview": preview.state if preview else "not_queued",
            "preview_message_id": preview.message_id if preview else None,
            "commercial_orders": db.scalar(select(func.count()).select_from(BillingOrder)) or 0,
            "commercial_entitlements": db.scalar(select(func.count()).select_from(Entitlement)) or 0}


def initialize(engine, settings, now=None):
    if not billing.prepared():
        return
    now = time.time() if now is None else now
    # Offer can be supplied only through protected configuration after commercial review.
    try:
        offer = json.loads(os.getenv("SUBSCRIPTION_APPROVED_OFFER_JSON", "{}"))
        if not isinstance(offer, dict) or len(json.dumps(offer)) > 6000:
            offer = {}
    except (ValueError, TypeError):
        offer = {}
    with Session(engine) as db:
        if not billing.control(db):
            db.add(BillingControl(id=billing.CONTROL, sales=False, enforce=False, offer=offer))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
        ctrl = billing.control(db, lock=True)
        if not db.get(BillingCampaign, CAMPAIGN):
            errors = billing.offer_errors(ctrl.offer)
            if not billing.verified_admin(settings):
                errors.append("admin_configuration_mismatch")
            row = BillingCampaign(id=CAMPAIGN, not_before=WHEN, deadline=WHEN+900, timezone="Europe/Kyiv",
                content=content(ctrl.offer), audience={"rule": "explicit_marketing_consent_v1", "exclude": ["stopped", "blocked", "active_access"]},
                status="expired" if now >= WHEN else "scheduled_blocked" if errors else "scheduled",
                blockers=errors, heartbeat=now)
            db.add(row)
        # The recipient is checked against the actual protected runtime Settings before enqueue AND send.
        key = "launch-preview:"+VERSION
        if billing.verified_admin(settings) and not db.get(BillingNotice, key):
            draft = ("🛠 <b>Попередній перегляд для власника</b>\n"
                     "Продаж і масова розсилка вимкнені. 250 грн — погоджений орієнтир; ціна Stars ще не погоджена. "
                     "Цей текст не готовий до відправки клієнтам.\n\n" + content(ctrl.offer)["text"])
            db.add(BillingNotice(id=key, kind="preview", user_id=settings.admin_telegram_id, text=draft))
        if now >= WHEN:
            queue_report(db, settings, now)
        db.commit()
        LOG.info("Billing preparation %s", json.dumps(snapshot(db, settings, now), sort_keys=True))


def readiness(db, settings, now):
    ctrl = billing.control(db)
    errors = billing.offer_errors(ctrl.offer if ctrl else {})
    if not billing.verified_admin(settings):
        errors.append("admin_configuration_mismatch")
    if not settings.live or not settings.monitor_enabled:
        errors.append("monitor_or_delivery_disabled")
    monitor = db.get(MonitorControl, "pilot")
    if not monitor or not 0 <= now-monitor.heartbeat <= 180:
        errors.append("monitor_heartbeat_stale")
    hook = db.get(SourceProbe, telegram_setup.PROBE_ID)
    if not hook or hook.status != "configured":
        errors.append("webhook_unavailable")
    last_delivery = db.scalar(select(func.max(DeliveryTiming.accepted_at)).join(Delivery, Delivery.id == DeliveryTiming.delivery_id).where(Delivery.state == "sent"))
    if not last_delivery or not 0 <= now-last_delivery <= 86400:
        errors.append("car_delivery_not_verified")
    if settings.ria_recent_publications_enabled:
        recent = db.get(SourceProbe, "recent-publications-v1")
        last = (recent.result.get("last_success_at") or 0) if recent else 0
        if not 0 <= now-last <= 7200:
            errors.append("discovery_heartbeat_stale")
    preview = db.get(BillingNotice, "launch-preview:"+VERSION)
    if not preview or preview.state != "sent":
        errors.append("admin_preview_not_confirmed")
    campaign = db.get(BillingCampaign, CAMPAIGN)
    if (not campaign or not campaign.content.get("price_verified")
            or campaign.content != content(ctrl.offer if ctrl else {})):
        errors.append("campaign_price_not_approved")
    return errors


def queue_report(db, settings, now):
    key = "launch-report:"+VERSION
    if not db.get(BillingNotice, key) and billing.verified_admin(settings):
        report = snapshot(db, settings, now)
        db.add(BillingNotice(id=key, kind="admin", user_id=settings.admin_telegram_id,
                            text="AutoDeal · звіт запуску\n"+json.dumps(report, ensure_ascii=False, indent=2)
                                 +"\nSent означає прийняття Telegram API, не прочитання."))


def high_priority(db, now):
    return bool(db.scalar(select(Delivery.id).where(Delivery.state.in_(["pending", "sending"])).limit(1))
                or db.scalar(select(BotReply.id).where(BotReply.state.in_(["pending", "sending"])).limit(1))
                or db.scalar(select(BillingOrder.id).where(BillingOrder.state == "pending", BillingOrder.created_at > now-15).limit(1)))


def recover_uncertain(db, now):
    db.execute(update(CampaignRecipient).where(CampaignRecipient.state == "sending", CampaignRecipient.attempted_at < now-LEASE)
               .values(state="uncertain", error="restart_after_send_claim"))
    db.execute(update(BillingNotice).where(BillingNotice.state == "sending", BillingNotice.attempted_at < now-LEASE)
               .values(state="uncertain"))


def tick(engine, settings, request=None, now=None):
    if not billing.prepared():
        return "disabled"
    now = time.time() if now is None else now
    request = request or telegram_setup.call
    lease = secrets.token_hex(16)
    with Session(engine) as db:
        recover_uncertain(db, now)
        claimed = db.execute(update(BillingCampaign).where(BillingCampaign.id == CAMPAIGN,
            BillingCampaign.lease_until <= now).values(lease_until=now+LEASE, lease_token=lease, heartbeat=now))
        db.commit()
        if claimed.rowcount != 1:
            return "busy"
    try:
        with Session(engine) as db:
            row = db.get(BillingCampaign, CAMPAIGN)
            if row.status in ("scheduled", "scheduled_blocked") and now >= row.not_before:
                errors = readiness(db, settings, now)
                if now > row.deadline:
                    errors.append("launch_window_missed")
                if errors:
                    row.status, row.blockers = ("expired" if now > row.deadline else "blocked"), errors
                    queue_report(db, settings, now)
                    db.commit()
                    LOG.info("Billing launch blocked %s", json.dumps(snapshot(db, settings, now), sort_keys=True))
                else:
                    # Read-only Telegram checks: never make a real test charge.
                    db.commit()
                    me = request(settings.bot_token, "getMe", {}, timeout=5)
                    hook = request(settings.bot_token, "getWebhookInfo", {}, timeout=5)
                    api_ok = (me.get("ok") is True and (me.get("result") or {}).get("username") == telegram_setup.BOT_USERNAME
                              and hook.get("ok") is True and (hook.get("result") or {}).get("url") == telegram_setup.WEBHOOK_URL
                              and not (hook.get("result") or {}).get("last_error_date"))
                    ctrl = billing.control(db, lock=True)
                    db.refresh(row)
                    errors = readiness(db, settings, now)
                    if row.status not in ("scheduled", "scheduled_blocked") or row.lease_token != lease:
                        return "cancelled"
                    if not api_ok or errors:
                        row.status, row.blockers = "blocked", errors + ([] if api_ok else ["telegram_api_unavailable"])
                        queue_report(db, settings, now)
                    else:
                        ctrl.sales = ctrl.enforce = True
                        row.status, row.blockers = "running", []
                        total, ids = count_audience(db, now)
                        row.audience = {**row.audience, "selected_at": now, "total_at_launch": total,
                                        "selected_at_launch": len(ids), "excluded_at_launch": total-len(ids)}
                        for uid in ids:
                            db.add(CampaignRecipient(campaign_id=CAMPAIGN, user_id=uid, state="pending"))
                    db.commit()
            db.refresh(row)
            if row.status != "running":
                return row.status
            errors = readiness(db, settings, now)
            if errors or now > row.deadline:
                ctrl = billing.control(db, lock=True)
                if errors:
                    ctrl.sales = ctrl.enforce = False
                row.status, row.blockers = "blocked" if errors else "expired", errors or ["broadcast_window_missed"]
                queue_report(db, settings, now)
                db.commit()
                return row.status
            if high_priority(db, now) or now < row.next_send:
                return "yielding_to_service"
            recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == CAMPAIGN,
                CampaignRecipient.state.in_(["pending", "retry"]), CampaignRecipient.retry_at <= now)
                .order_by(CampaignRecipient.user_id).limit(1))
            if not recipient:
                outstanding = db.scalar(select(CampaignRecipient.user_id).where(CampaignRecipient.campaign_id == CAMPAIGN,
                    CampaignRecipient.state.in_(["pending", "retry", "sending"])).limit(1))
                if outstanding is None:
                    row.status = "complete"
                    queue_report(db, settings, now)
                    db.commit()
                return row.status
            user = db.get(User, recipient.user_id)
            consent = db.get(MarketingConsent, recipient.user_id)
            if (not user or not user.ready or not consent or not consent.allowed or consent.blocked
                    or billing.expiry(db, recipient.user_id) > now):
                recipient.state, recipient.error = "excluded", "recipient_no_longer_eligible"
                db.commit()
                return "excluded"
            recipient.state, recipient.attempted_at = "sending", now
            uid = recipient.user_id
            payload = {"chat_id": uid, "text": row.content["text"], "parse_mode": "HTML", "allow_paid_broadcast": False,
                       "reply_markup": {"inline_keyboard": [[{"text": row.content["button"], "callback_data": row.content["callback_data"]}]]}}
            row.next_send = now+5
            db.commit()  # Durable send claim before the network, never retry an unknown result.
        result = request(settings.bot_token, "sendMessage", payload, timeout=5)
        with Session(engine) as db:
            recipient = db.get(CampaignRecipient, (CAMPAIGN, uid))
            apply_send_result(recipient, result, now)
            if result.get("error_code") == 403:
                consent = db.get(MarketingConsent, uid)
                if consent:
                    consent.blocked = True
            db.commit()
            return recipient.state
    finally:
        with Session(engine) as db:
            db.execute(update(BillingCampaign).where(BillingCampaign.id == CAMPAIGN,
                BillingCampaign.lease_token == lease).values(lease_until=0, lease_token=""))
            db.commit()


def apply_send_result(row, result, now):
    mid = (result.get("result") or {}).get("message_id") if isinstance(result.get("result"), dict) else None
    if result.get("ok") is True and type(mid) is int:
        row.state, row.message_id = "sent", mid
    elif result.get("error_code") == 429:
        delay = (result.get("parameters") or {}).get("retry_after")
        if type(delay) is int and 1 <= delay <= 86400:
            row.state, row.retry_at = "retry", now+delay+1
        else:
            row.state = "uncertain"
    elif result.get("error_code") in (400, 401, 403, 404):
        row.state = "failed"
    else:
        row.state = "uncertain"
    if hasattr(row, "error"):
        row.error = str(result.get("error_code") or "")[:60]


def deliver_notice(engine, settings, request=None, now=None):
    now = time.time() if now is None else now
    if not billing.verified_admin(settings):
        return "admin_mismatch"
    request = request or telegram_setup.call
    with Session(engine) as db:
        recover_uncertain(db, now)
        row = db.scalar(select(BillingNotice).where(BillingNotice.state.in_(["pending", "retry"]),
                         BillingNotice.retry_at <= now).order_by(BillingNotice.id).with_for_update(skip_locked=True).limit(1))
        if not row:
            db.commit()
            return "empty"
        if row.kind == "preview" and high_priority(db, now):
            db.commit()
            return "yielding_to_service"
        key = row.id
        changed = db.execute(update(BillingNotice).where(BillingNotice.id == key,
            BillingNotice.state.in_(["pending", "retry"])).values(state="sending", attempted_at=now))
        if changed.rowcount != 1:
            db.rollback()
            return "busy"
        uid = row.user_id if row.kind == "support_reply" else settings.admin_telegram_id
        payload = {"chat_id": uid, "text": row.text, "allow_paid_broadcast": False}
        if row.kind == "preview":
            payload.update(parse_mode="HTML", protect_content=True, reply_markup={"inline_keyboard": [[
                {"text": "Оформити підписку · перегляд", "callback_data": billing.PREFIX+"terms"}]]})
        db.commit()
    result = request(settings.bot_token, "sendMessage", payload, timeout=5)
    with Session(engine) as db:
        row = db.get(BillingNotice, key)
        apply_send_result(row, result, now)
        db.commit()
        if row.kind == "preview":
            LOG.info("Billing preview %s", json.dumps(snapshot(db, settings, now), sort_keys=True))
        return row.state


async def run(engine, settings, stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick, engine, settings)
            await asyncio.to_thread(deliver_notice, engine, settings)
        except Exception:
            # No token, payload, user details or raw provider error in logs.
            LOG.error("Billing executor check failed; persistent state retained")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
