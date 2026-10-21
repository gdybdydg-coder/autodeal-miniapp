"""Private owner-only 1-Star payment pilot; never changes search/delivery state."""
import logging
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import StarsTestOrder, SourceProbe
from . import telegram_setup

OWNER = 777292211
COMMANDS = {"/paytest", "/paytest_confirm", "/payment", "/refundtest", "/paysupport"}
TERMS = (
    "🧪 Приватний тест AUTODeal\n\n"
    "Разова оплата: 1 ⭐, реальне списання. Тестовий доступ — 30 днів. "
    "Автоматичних списань немає. Пошук працює за твоїми збереженими фільтрами; "
    "оплата не вмикає зупинені пошуки й не гарантує всі оголошення або доставку за 30 секунд.\n\n"
    "Повернути тестову оплату: /refundtest. Підтримка оплати: /paysupport. "
    "Telegram не надає підтримку покупок у цьому боті.\n\n"
    "Якщо погоджуєшся з цими умовами та списанням 1 ⭐, надішли /paytest_confirm."
)


def configure(engine, settings, request=None):
    """Install menu only for owner's private chat. No global command changes."""
    if not settings.configure_webhook or telegram_setup.webhook_status(engine)["status"] != "configured":
        return
    request = request or telegram_setup.call
    from .bot_commands import COMMANDS as public_commands
    with Session(engine) as db:
        row = db.get(SourceProbe, "stars-owner-menu-v1")
        if row and row.status == "configured":
            return
        response = request(settings.bot_token, "setMyCommands", {
            "scope": {"type": "chat", "chat_id": OWNER},
            "commands": public_commands+[
                {"command": "paytest", "description": "Тест оплати 1 ⭐ (лише власник)"},
                {"command": "payment", "description": "Статус тестового доступу"},
                {"command": "refundtest", "description": "Повернути тестову оплату 1 ⭐"},
                {"command": "paysupport", "description": "Допомога з оплатою"}]})
        ok = response.get("ok") is True and response.get("result") is True
        db.merge(SourceProbe(id="stars-owner-menu-v1", status="configured" if ok else "unavailable",
                             checked_at=time.time(), requests=0, result={}))
        db.commit()
        logging.getLogger(__name__).info("Owner-only Stars test menu: %s", "configured" if ok else "unavailable")


def message(text):
    return {"method": "sendMessage", "chat_id": OWNER, "text": text}


def valid_payment(order, uid, data):
    return (order is not None and uid == order.user_id == OWNER
            and data.get("currency") == "XTR"
            and type(data.get("total_amount")) is int and data["total_amount"] == 1)


def handle(engine, settings, event, request=None, now=None):
    """Only called after webhook secret authentication; output is Telegram webhook reply."""
    now = time.time() if now is None else now
    request = request or (lambda token, method, payload:
                         telegram_setup.call(token, method, payload, timeout=5))
    checkout = event.get("pre_checkout_query")
    if checkout is not None:
        if not isinstance(checkout, dict):
            return {"ok": True}
        uid = (checkout.get("from") or {}).get("id")
        with Session(engine) as db:
            row = db.get(StarsTestOrder, checkout.get("invoice_payload", ""))
            ok = (type(uid) is int and valid_payment(row, uid, checkout)
                  and row.state == "pending" and 0 <= now-row.created_at <= 900)
        result = {"method": "answerPreCheckoutQuery", "pre_checkout_query_id": checkout.get("id"), "ok": bool(ok)}
        if not ok:
            result["error_message"] = "Цей рахунок недоступний або прострочений."
        return result
    msg = event.get("message") or {}
    sender, chat = msg.get("from") or {}, msg.get("chat") or {}
    uid = sender.get("id")
    if (type(uid) is not int or uid != OWNER or chat.get("type") != "private"
            or chat.get("id") != OWNER or sender.get("is_bot")):
        return None
    payment = msg.get("successful_payment")
    refunded = msg.get("refunded_payment")
    if payment is not None or refunded is not None:
        data = payment if payment is not None else refunded
        if not isinstance(data, dict):
            return {"ok": True}
        charge = data.get("telegram_payment_charge_id")
        if not isinstance(charge, str) or not charge or len(charge) > 256:
            return {"ok": True}
        with Session(engine) as db:
            row = db.scalar(select(StarsTestOrder).where(
                StarsTestOrder.id == data.get("invoice_payload", "")).with_for_update())
            if not valid_payment(row, uid, data):
                return {"ok": True}
            if refunded is not None:
                if row.charge_id == charge and row.state in ("paid", "refund_sending", "refund_uncertain"):
                    row.state = "refunded"
                    db.commit()
                    return message("✅ Тестову оплату повернуто. Пошуки та /stop не змінено.")
                return {"ok": True}
            # Late successful payments must still be recorded after invoice expiry.
            if row.state != "pending":
                return {"ok": True}
            row.state, row.charge_id, row.paid_until = "paid", charge, now+30*86400
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return {"ok": True}
            until = datetime.fromtimestamp(row.paid_until, ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
        result = message(f"✅ Оплату 1 ⭐ підтверджено. Тестовий доступ активний до {until}.\n"
                         "Користуйся AUTODeal зі збереженими фільтрами. Зупинені пошуки вмикай самостійно.\n"
                         "Статус: /payment · Повернення: /refundtest")
        result["reply_markup"] = {"inline_keyboard": [[{"text": "Відкрити AUTODeal", "web_app": {"url": telegram_setup.APP_URL}}]]}
        return result
    text = msg.get("text", "")
    if not isinstance(text, str):
        return None
    token = text.split()[0] if text.split() else ""
    command, _, mention = token.partition("@")
    if command not in COMMANDS or (mention and mention.lower() != telegram_setup.BOT_USERNAME.lower()):
        return None
    if command == "/paytest":
        return message(TERMS)
    if command == "/paysupport":
        return message("🧪 Підтримка приватної тестової оплати: /payment показує статус; /refundtest повертає 1 ⭐. "
                       "Якщо статус невизначений, повідом про це в нашому чаті розробки. "
                       "Telegram не обслуговує покупки в цьому боті.")
    with Session(engine) as db:
        if command == "/payment":
            row = db.scalar(select(StarsTestOrder).where(StarsTestOrder.user_id == OWNER,
                StarsTestOrder.state.in_(["paid", "refunded", "refund_sending", "refund_uncertain"]))
                .order_by(StarsTestOrder.created_at.desc()).limit(1))
            if not row:
                return message("Оплати ще немає. Почати тест: /paytest")
            if row.state == "paid":
                until = datetime.fromtimestamp(row.paid_until, ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
                return message(f"{'✅ Активний' if row.paid_until > now else '⌛ Завершений'} тестовий доступ до {until}. Оплачено 1 ⭐.")
            return message({"refunded": "✅ Оплату повернуто.", "refund_sending": "Повернення обробляється.",
                            "refund_uncertain": "Результат повернення невизначений. Автоматично не повторюємо; потрібна перевірка."}[row.state])
        if command == "/refundtest":
            row = db.scalar(select(StarsTestOrder).where(StarsTestOrder.user_id == OWNER,
                StarsTestOrder.state == "paid").order_by(StarsTestOrder.created_at.desc()).with_for_update().limit(1))
            if not row:
                return message("Немає підтвердженої оплати для повернення. Статус: /payment")
            row.state = "refund_sending"
            order_id, charge = row.id, row.charge_id
            db.commit()  # Claim before network: never replay an uncertain refund.
        else:
            update_id, date = event.get("update_id"), msg.get("date")
            if type(update_id) is not int or type(date) is not int or not 0 <= now-date <= 300:
                return {"ok": True}
            if db.scalar(select(StarsTestOrder).where(StarsTestOrder.command_update == update_id)):
                return {"ok": True}
            if db.scalar(select(StarsTestOrder).where(StarsTestOrder.state == "paid", StarsTestOrder.paid_until > now)):
                return message("Тестовий доступ уже оплачений. Статус: /payment")
            count = len(db.scalars(select(StarsTestOrder.id)).all())
            if count >= 20:
                return message("Ліміт приватного тесту досягнуто. Звернися в чат розробки.")
            payload = "autodeal-owner:"+secrets.token_hex(16)
            db.add(StarsTestOrder(id=payload, user_id=OWNER, command_update=update_id,
                                 created_at=now, state="pending"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return {"ok": True}
            return {"method": "sendInvoice", "chat_id": OWNER, "title": "AUTODeal — приватний тест",
                    "description": "Тестовий доступ на 30 днів. Разова реальна оплата 1 ⭐; повернення /refundtest.",
                    "payload": payload, "currency": "XTR", "provider_token": "",
                    "prices": [{"label": "Тестовий доступ", "amount": 1}], "start_parameter": "owner-stars-test"}
    try:
        response = request(settings.bot_token, "refundStarPayment", {"user_id": OWNER, "telegram_payment_charge_id": charge})
    except Exception:
        response = {}
    state = "refunded" if response.get("ok") is True and response.get("result") is True else "refund_uncertain"
    with Session(engine) as db:
        row = db.scalar(select(StarsTestOrder).where(StarsTestOrder.id == order_id).with_for_update())
        if row.state != "refunded":
            row.state = state
        db.commit()
    return message("✅ Повернення 1 ⭐ підтверджено." if state == "refunded" else
                   "Результат повернення невизначений. Повторне списання не запускаємо. Статус: /payment")
