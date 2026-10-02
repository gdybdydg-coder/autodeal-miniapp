"""One-time Stars purchases. Public sales remain closed until the durable launch passes."""
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import telegram_setup
from .models import User, StarsTestOrder
from .billing_models import (BillingControl, Entitlement, BillingOrder, AccessEvent,
                             MarketingConsent, BillingNotice, BillingCampaign)

DAYS = 30
PREFIX = "ad-sub:"
CONTROL = "commercial-v1"
LOG = logging.getLogger(__name__)


def prepared():
    return os.getenv("SUBSCRIPTION_LAUNCH_PREPARED") == "true"


def verified_admin(settings):
    expected = os.getenv("SUBSCRIPTION_EXPECTED_ADMIN_ID", "")
    return (expected.isdecimal() and int(expected) > 0
            and settings.admin_telegram_id == int(expected))


def control(db, *, lock=False):
    if lock:
        # PostgreSQL row serialization; an actual UPDATE also serializes SQLite tests.
        db.execute(update(BillingControl).where(BillingControl.id == CONTROL)
                   .values(version=BillingControl.version + 1))
    if lock or "billing_control" not in db.info:
        db.info["billing_control"] = db.get(BillingControl, CONTROL, populate_existing=lock)
    return db.info["billing_control"]


def offer_errors(offer):
    errors = []
    if type(offer.get("stars")) is not int or not 1 <= offer["stars"] <= 100000:
        errors.append("commercial_stars_price_not_approved")
    if offer.get("days") != DAYS or offer.get("currency") != "XTR":
        errors.append("offer_not_approved")
    for key in ("price_approval", "terms", "terms_version", "refund_terms", "transition_review", "search_review", "payment_review"):
        if not isinstance(offer.get(key), str) or not offer[key].strip():
            errors.append(key + "_required")
    if sum(len(offer.get(k, "")) for k in ("terms", "refund_terms") if isinstance(offer.get(k), str)) > 2400:
        errors.append("terms_too_long")
    if offer.get("support_route") != "bot_paysupport":
        errors.append("support_route_not_approved")
    return errors


def expiry(db, uid):
    row = db.get(Entitlement, uid)
    result = row.expires_at if row else 0
    # Preserve the actual paid owner pilot's promised access, without recording a sale.
    legacy = db.scalar(select(func.max(StarsTestOrder.paid_until)).where(
        StarsTestOrder.user_id == uid, StarsTestOrder.state.in_(["paid", "refund_sending", "refund_uncertain"])))
    return max(result, legacy or 0)


def allowed(db, uid, now=None):
    now = time.time() if now is None else now
    row = control(db)
    return not row or not row.enforce or expiry(db, uid) > now


def public_status(db, uid, now=None):
    now = time.time() if now is None else now
    row = control(db)
    until = expiry(db, uid)
    offer = row.offer if row else {}
    manual = offer.get("method") == "bank_manual"
    if manual:
        from . import manual_checkout, manual_payments
        can_sell = (manual_checkout.offer_valid(offer) and manual_payments.public_creation_allowed()
                    and manual_checkout.subscription_preview.receiving_profile() is not None)
    else:
        can_sell = not offer_errors(offer)
    return {"sales_enabled": bool(row and row.sales and can_sell),
            "paid_access_required": bool(row and row.enforce), "access_available": allowed(db, uid, now),
            "expires_at": until or None, "days": DAYS,
            "payment_method": "bank_manual" if manual else "stars",
            "amount_uah": 250 if manual else None,
            "amount_stars": offer.get("stars") if row and row.sales else None,
            "purchase_url": "https://t.me/" + telegram_setup.BOT_USERNAME + "?start=subscribe"}


def message(uid, text, keyboard=None):
    result = {"method": "sendMessage", "chat_id": uid, "text": text, "parse_mode": "HTML"}
    if keyboard:
        result["reply_markup"] = {"inline_keyboard": keyboard}
    return result


def date_text(value):
    return datetime.fromtimestamp(value, ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y о %H:%M")


def card(db, uid, now):
    state = public_status(db, uid, now)
    parts = ["🚘 <b>AutoDeal · Абонемент</b>"]
    if state["expires_at"]:
        parts.append(("✅ Доступ активний до " if state["expires_at"] > now else "⌛ Доступ завершився ")
                     + "<b>" + date_text(state["expires_at"]) + "</b> (Київ).")
    elif state["paid_access_required"]:
        parts.append("Для пошуку та сповіщень потрібен абонемент. Твої фільтри збережені.")
    else:
        parts.append("Зараз пошук доступний безкоштовно.")
    keyboard = []
    if state["sales_enabled"]:
        price = "250 грн" if state["payment_method"] == "bank_manual" else f"{state['amount_stars']} ⭐"
        parts.append(f"<b>{price} за 30 днів</b>\nРазова оплата. Автоматичних списань немає.")
        keyboard.append([{"text": "Оформити абонемент", "callback_data": PREFIX + "terms"}])
    else:
        parts.append("Продаж абонементів ще не відкрито.")
    parts.append("Статус: /subscription · Умови: /terms\nДопомога з оплатою: /paysupport\nВідмова від реклами: /marketing_off · Усі сповіщення: /stop")
    return message(uid, "\n\n".join(parts), keyboard)


def terms_card(db, uid):
    row = control(db)
    if not row or not row.sales or offer_errors(row.offer):
        return message(uid, "Продаж ще не відкрито. Умови покупки та повернення будуть доступні тут до оплати. /subscription")
    offer = row.offer
    token = hashlib.sha256(json.dumps(offer, sort_keys=True).encode()).hexdigest()[:20]
    text = (f"🚘 <b>AutoDeal · 30 днів</b>\n\n<b>{offer['stars']} ⭐</b> · разова оплата\n\n"
            + escape(offer["terms"]) + "\n\n" + escape(offer["refund_terms"])
            + "\n\n30 днів від підтвердження оплати. Продовження додає 30 днів до чинного строку. "
            "Автоматичних списань немає. Зупинені пошуки не вмикаються самі.\n\n"
            "Підтримка: /paysupport. Telegram не обслуговує покупки в цьому боті. "
            "Натискаючи кнопку, ти погоджуєшся з наведеними умовами.")
    return message(uid, text, [[{"text": f"Погоджуюсь · оплатити {offer['stars']} ⭐", "callback_data": PREFIX + "buy:" + token}]])


def matches_payment(row, uid, data):
    return (row is not None and row.user_id == uid and data.get("currency") == "XTR"
            and type(data.get("total_amount")) is int and data["total_amount"] == row.amount
            and data.get("invoice_payload") == row.id)


def apply_payment(engine, uid, data, now):
    charge = data.get("telegram_payment_charge_id")
    if not isinstance(charge, str) or not 1 <= len(charge) <= 256:
        return False
    with Session(engine) as db:
        control(db, lock=True)
        row = db.get(BillingOrder, data.get("invoice_payload", ""))
        if not matches_payment(row, uid, data) or row.state != "pending":
            return False
        if db.scalar(select(BillingOrder.id).where(BillingOrder.charge_id == charge)):
            return False
        until = max(now, expiry(db, uid)) + DAYS * 86400
        row.state, row.charge_id, row.expires_at = "paid", charge, until
        db.merge(Entitlement(user_id=uid, expires_at=until, updated_at=now))
        db.add(AccessEvent(id="payment:"+row.id, user_id=uid, actor=uid, kind="paid", at=now,
                           expires_at=until, reason="Telegram successful_payment verified"))
        try:
            db.commit()  # Charge, audit and entitlement are one transaction.
        except IntegrityError:
            db.rollback()
            return False
        return True


def consent(db, uid, permitted, update_id, now, source):
    row = db.get(MarketingConsent, uid, with_for_update=True)
    if row is None:
        row = MarketingConsent(user_id=uid, allowed=False, blocked=False, update_id=-1, at=now, source=source)
        db.add(row)
    if update_id > row.update_id:
        row.allowed, row.update_id, row.at, row.source = permitted, update_id, now, source


def admin_command(db, settings, uid, text, update_id, now):
    if not verified_admin(settings) or uid != settings.admin_telegram_id:
        return {"ok": True}
    from . import billing_campaign
    parts = text.split(maxsplit=4)
    action = parts[1] if len(parts) > 1 else "status"
    control(db, lock=True)
    if action == "cancel":
        row = db.get(BillingCampaign, billing_campaign.CAMPAIGN)
        if row:
            row.status = "cancelled"
        result = "Кампанію скасовано. Платежі та наданий доступ збережено."
    elif action == "pause":
        row = control(db)
        row.sales = row.enforce = False
        campaign = db.get(BillingCampaign, billing_campaign.CAMPAIGN)
        if campaign:
            campaign.status = "cancelled"
        result = "Нові продажі та платні обмеження вимкнено. Історія й строки доступу збережені."
    elif action == "grant" and len(parts) == 5:
        try:
            target = int(parts[2])
            until = datetime.fromisoformat(parts[3]).timestamp() if "+" in parts[3] or parts[3].endswith("Z") else 0
            if not db.get(User, target) or until <= now or len(parts[4]) > 500:
                raise ValueError()
        except (ValueError, OverflowError):
            return message(uid, "Формат: /billing_admin grant USER_ID ISO_DATE_WITH_ZONE причина")
        key = "admin-grant:"+str(update_id)
        if not db.get(AccessEvent, key):
            until = max(until, expiry(db, target))
            db.merge(Entitlement(user_id=target, expires_at=until, updated_at=now))
            db.add(AccessEvent(id=key, user_id=target, actor=uid, kind="gift", at=now, expires_at=until, reason=parts[4]))
        result = "Доступ збережено без скорочення чинного строку."
    elif action in ("refund", "refund_confirm") and len(parts) == 3:
        row = db.get(BillingOrder, parts[2])
        if not row or row.state != "paid" or not row.charge_id:
            return message(uid, "Підтвердженого платежу для повернення не знайдено. Невизначені повернення повторно не надсилаємо.")
        if action == "refund":
            return message(uid, f"Повернення {row.amount} ⭐ за замовлення <code>{escape(row.id)}</code>. "
                           "Перевір узгоджені умови. Для підтвердження: <code>/billing_admin refund_confirm " + escape(row.id) + "</code>")
        row.state = "refund_sending"
        order_id, payer, charge = row.id, row.user_id, row.charge_id
        db.commit()
        response = telegram_setup.call(settings.bot_token, "refundStarPayment",
            {"user_id": payer, "telegram_payment_charge_id": charge}, timeout=5)
        control(db, lock=True)
        row = db.get(BillingOrder, order_id, populate_existing=True)
        if row.state != "refunded":
            row.state = "refunded" if response.get("ok") is True and response.get("result") is True else "refund_uncertain"
        result = "Повернення: " + row.state + ". Наданий доступ збережено для окремої перевірки; інші оплати й подарунки не анульовано."
    elif action == "payments":
        rows = db.scalars(select(BillingOrder).order_by(BillingOrder.created_at.desc()).limit(20)).all()
        result = "Останні комерційні платежі:\n" + "\n".join(
            f"{r.id} · user {r.user_id} · {r.amount} XTR · {r.state}" for r in rows)
    elif action == "access":
        rows = db.scalars(select(Entitlement).order_by(Entitlement.updated_at.desc()).limit(30)).all()
        result = "Доступ:\n" + "\n".join(f"user {r.user_id} · {date_text(r.expires_at)}" for r in rows)
    elif action == "reply" and len(parts) >= 4:
        ticket = db.get(BillingNotice, "support:"+parts[2])
        if not ticket or ticket.kind != "support":
            return message(uid, "Звернення не знайдено.")
        key = "support-reply:"+str(update_id)
        if not db.get(BillingNotice, key):
            reply_text = text.split(maxsplit=3)[3][:3000]
            db.add(BillingNotice(id=key, kind="support_reply", user_id=ticket.user_id,
                                 text="Відповідь підтримки AutoDeal:\n"+reply_text))
        result = "Відповідь поставлено в чергу."
    else:
        result = json.dumps(billing_campaign.snapshot(db, settings, now), ensure_ascii=False, indent=2)
        result += "\n/billing_admin payments · access · cancel · pause\nДля відповіді: /billing_admin reply TICKET текст"
    db.commit()
    return message(uid, "<pre>" + escape(result[:3700]) + "</pre>")


def handle(engine, settings, event, request=None, now=None):
    if not prepared():
        with Session(engine) as db:
            if not control(db):
                return None
    now = time.time() if now is None else now
    checkout = event.get("pre_checkout_query")
    if isinstance(checkout, dict) and str(checkout.get("invoice_payload", "")).startswith(PREFIX):
        uid = (checkout.get("from") or {}).get("id")
        with Session(engine) as db:
            cfg = control(db)
            order = db.get(BillingOrder, checkout.get("invoice_payload", ""))
            ok = (type(uid) is int and not (checkout.get("from") or {}).get("is_bot") and cfg and cfg.sales and not offer_errors(cfg.offer)
                  and matches_payment(order, uid, checkout) and order.state == "pending"
                  and order.amount == cfg.offer["stars"] and order.terms_version == cfg.offer["terms_version"]
                  and 0 <= now-order.created_at <= 900)
        return {"method": "answerPreCheckoutQuery", "pre_checkout_query_id": checkout.get("id"),
                "ok": bool(ok), **({} if ok else {"error_message": "Оплата недоступна. Відкрий /subscription для актуальних умов."})}
    cb = event.get("callback_query")
    msg = cb.get("message", {}) if isinstance(cb, dict) else event.get("message", {})
    sender = cb.get("from", {}) if isinstance(cb, dict) else msg.get("from", {}) if isinstance(msg, dict) else {}
    if not isinstance(msg, dict) or not isinstance(sender, dict) or not isinstance(msg.get("chat"), dict):
        return None
    uid, chat = sender.get("id"), msg["chat"]
    if type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot") or chat.get("type") != "private" or chat.get("id") != uid:
        return None
    data = msg.get("successful_payment")
    if isinstance(data, dict) and str(data.get("invoice_payload", "")).startswith(PREFIX):
        if apply_payment(engine, uid, data, now):
            with Session(engine) as db:
                return card(db, uid, now)
        return {"ok": True}
    refund = msg.get("refunded_payment")
    if isinstance(refund, dict) and str(refund.get("invoice_payload", "")).startswith(PREFIX):
        with Session(engine) as db:
            control(db, lock=True)
            row = db.get(BillingOrder, refund.get("invoice_payload", ""))
            if matches_payment(row, uid, refund) and row.charge_id == refund.get("telegram_payment_charge_id"):
                row.state = "refunded"
                # Do not silently revoke paid/gift time; admin reviews overlapping grants.
                key = "refund-review:" + row.id
                if not db.get(BillingNotice, key):
                    db.add(BillingNotice(id=key, kind="admin", user_id=settings.admin_telegram_id,
                                         text="Повернення підтверджене. Перевірити доступ для замовлення " + row.id))
                db.commit()
        return {"ok": True}
    text = msg.get("text", "")
    if not isinstance(text, str):
        return None
    token = text.split()[0] if text.split() else ""
    command, _, mention = token.partition("@")
    if mention and mention.lower() != telegram_setup.BOT_USERNAME.lower():
        return None
    action = cb.get("data", "") if isinstance(cb, dict) else ""
    if cb and (not isinstance(action, str) or not action.startswith(PREFIX)):
        return None
    commands = {"/subscription", "/terms", "/paysupport", "/support", "/marketing", "/marketing_off", "/billing_admin"}
    deep = command == "/start" and text.split()[1:] == ["subscribe"]
    if not cb and command not in commands and not deep and command != "/stop":
        return None
    update_id = event.get("update_id")
    stamp = msg.get("date")
    if type(update_id) is not int or not 0 <= update_id < 2**63:
        return {"ok": True}
    # Payment events above remain valid when delayed. Commands may not replay old actions.
    if not cb and command != "/stop" and (type(stamp) is not int or not 0 <= now-stamp <= 300):
        return {"ok": True}
    if cb:
        if not isinstance(cb.get("id"), str):
            return {"ok": True}
        req = request or telegram_setup.call
        req(settings.bot_token, "answerCallbackQuery", {"callback_query_id": cb["id"]}, timeout=3)
    with Session(engine) as db:
        if command == "/billing_admin":
            return admin_command(db, settings, uid, text, update_id, now)
        if command in ("/stop", "/marketing_off") or action == PREFIX+"off":
            control(db, lock=True)
            consent(db, uid, False, update_id, now, "explicit_opt_out")
            db.commit()
            return None if command == "/stop" else message(uid, "Рекламні повідомлення вимкнено. Налаштування пошуку збережені.")
        if action == PREFIX+"on":
            control(db, lock=True)
            consent(db, uid, True, update_id, now, "explicit_marketing_button_v1")
            db.commit()
            return message(uid, "Згоду на новини й пропозиції AutoDeal збережено. Відмовитися: /marketing_off")
        if command == "/marketing":
            return message(uid, "Хочеш отримувати новини та платні пропозиції AutoDeal? Це необов’язково. Відмова: /marketing_off", [[
                {"text": "Дозволити рекламні повідомлення", "callback_data": PREFIX+"on"}]])
        if command in ("/paysupport", "/support"):
            body = text.partition(" ")[2].strip()
            if not body:
                return message(uid, "Напиши /paysupport і коротко опиши питання в тому самому повідомленні. Звернення отримає підтримка AutoDeal. Не надсилай паролі чи дані картки. Telegram не обслуговує покупки в цьому боті.")
            if not verified_admin(settings):
                return message(uid, "Підтримка тимчасово недоступна. Спробуй пізніше; повідомлення не передано.")
            control(db, lock=True)
            key = "support:"+str(update_id)
            if not db.get(BillingNotice, key):
                db.add(BillingNotice(id=key, kind="support", user_id=uid,
                                     text=f"Платіжне звернення {update_id}:\n"+body[:3000]))
            db.commit()
            return message(uid, "Звернення збережено для підтримки AutoDeal. Відповідь надійде в цей чат.")
        if command == "/terms" or action == PREFIX+"terms":
            return terms_card(db, uid)
        if action.startswith(PREFIX+"buy:"):
            cfg = control(db, lock=True)
            if not cfg or not cfg.sales or offer_errors(cfg.offer):
                return card(db, uid, now)
            offer = cfg.offer
            expected = hashlib.sha256(json.dumps(offer, sort_keys=True).encode()).hexdigest()[:20]
            if action != PREFIX+"buy:"+expected:
                return terms_card(db, uid)
            if db.scalar(select(BillingOrder.id).where(BillingOrder.update_id == update_id)):
                return {"ok": True}
            pending = db.scalar(select(BillingOrder.id).where(BillingOrder.user_id == uid,
                BillingOrder.state == "pending", BillingOrder.created_at > now-900))
            if pending:
                return message(uid, "У тебе вже є відкритий рахунок. Скористайся ним або зачекай 15 хвилин.")
            order_id = PREFIX+secrets.token_hex(16)
            db.add(BillingOrder(id=order_id, user_id=uid, update_id=update_id, amount=offer["stars"],
                                terms_version=offer["terms_version"], created_at=now))
            db.commit()
            return {"method": "sendInvoice", "chat_id": uid, "title": "AutoDeal · 30 днів",
                    "description": "Разова оплата 30 днів пошуку та сповіщень. Без автоматичних списань. Підтримка: /paysupport.",
                    "payload": order_id, "currency": "XTR", "provider_token": "",
                    "prices": [{"label": "Абонемент на 30 днів", "amount": offer["stars"]}], "start_parameter": "subscribe"}
        return card(db, uid, now)
