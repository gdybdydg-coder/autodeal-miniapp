"""Owner-only durable Telegram trial. Collection and production billing stay OFF."""
import copy
import math
import re
import secrets
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import SourceProbe, SubscriptionPreview
from . import telegram_setup

OWNER = 777292211
AMOUNT_UAH, DAYS = 250, 30
PAYMENTS_ENABLED = False
MAX_ORDERS = 20
COMMANDS = {"/subtest", "/subscription_test", "/subtest_admin"}
PREFIX = "subtest:"


def initial():
    return {"orders": [], "expires_at": 0, "last_update": -1}


def view(engine):
    with Session(engine) as db:
        row = db.get(SubscriptionPreview, OWNER)
        return copy.deepcopy(row.state) if row else initial()


def apply(engine, action, order_id, update_id, now):
    """One namespace row: PG row lock + CAS also protects SQLite test races."""
    for _ in range(8):
        with Session(engine) as db:
            row = db.scalar(select(SubscriptionPreview).where(
                SubscriptionPreview.user_id == OWNER).with_for_update())
            if row is None:
                db.add(SubscriptionPreview(user_id=OWNER, version=0, updated_at=now, state=initial()))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                continue
            state = copy.deepcopy(row.state)
            if update_id <= state["last_update"]:
                return state, "Тестовий стан уже збережено."
            if now < row.updated_at:
                return state, "Час сервера змінився. Тестову дію не виконано."
            orders = state["orders"]
            note = ""
            if action == "create":
                if orders and orders[-1]["status"] in ("awaiting", "review"):
                    note = "Відкрита тестова заявка вже є."
                elif len(orders) >= MAX_ORDERS:
                    note = "Ліміт 20 тестових заявок досягнуто. Історію не скидаємо."
                else:
                    orders.append({"id": secrets.token_hex(16), "amount": AMOUNT_UAH, "days": DAYS,
                                   "status": "awaiting", "created_at": now})
                    note = "Тестову заявку створено. Гроші не переказуй."
            else:
                order = next((o for o in orders if o["id"] == order_id), None)
                if order is None:
                    note = "Тестову заявку не знайдено."
                elif action == "receipt":
                    if order["status"] == "awaiting":
                        order.update(status="review", receipt_ref="SYNTH-"+order["id"], receipt_at=now)
                        note = "Демонстраційну квитанцію позначено для перевірки."
                    else:
                        note = "Квитанцію вже оброблено."
                elif action == "approve":
                    if order["status"] == "review":
                        # Explicit synthetic approval, not bank reconciliation/payment.
                        state["expires_at"] = max(now, state["expires_at"])+order["days"]*86400
                        order.update(status="approved", approved_at=now, expires_at=state["expires_at"],
                                     payment_ref="SYNTH-"+order["id"])
                        note = "Підтверджено лише тестовий абонемент. Реальної оплати немає."
                    elif order["status"] == "approved":
                        note = "Цю заявку вже підтверджено. Строк вдруге не додається."
                    else:
                        note = "Спочатку натисни «Тестова квитанція»."
                elif action == "reject":
                    if order["status"] in ("awaiting", "review"):
                        order.update(status="rejected", rejected_at=now)
                        note = "Тестову заявку відхилено."
                    else:
                        note = "Закриту заявку не змінено."
            state["last_update"] = update_id
            expected = row.version
            saved = db.execute(update(SubscriptionPreview).where(
                SubscriptionPreview.user_id == OWNER, SubscriptionPreview.version == expected).values(
                version=expected+1, updated_at=now, state=state))
            if saved.rowcount == 1:
                db.commit()
                return state, note
            db.rollback()
    raise RuntimeError("Owner subscription preview busy")


def render(state, now, note="", *, admin=False, owner_controls=False):
    """Compact customer-style preview; identifiers/actions have an owner view."""
    order = state["orders"][-1] if state["orders"] else None
    amount, days = (order["amount"], order["days"]) if order else (AMOUNT_UAH, DAYS)
    sections = ["🛠 <b>AUTODeal · Кабінет власника</b>" if admin else
                "🚘 <b>AUTODeal</b>\nВигідні авто у твоєму Telegram",
                f"💳 <b>{escape(str(amount))} грн</b>  ·  📅 <b>{escape(str(days))} днів</b>",
                "🧪 <b>Тест без оплати</b>\nБот поки безкоштовний. Не переказуй кошти."]
    keyboard = []
    active = state["expires_at"] > now
    if state["expires_at"]:
        until = datetime.fromtimestamp(state["expires_at"], ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y · %H:%M")
        sections.append(("💎 <b>Абонемент активний</b>" if active else "⌛ <b>Абонемент завершений</b>")
                        +f"\n📅 До <b>{until}</b>")
    if order:
        labels = {"awaiting": "📝 <b>Заявку створено</b>", "review": "⏳ <b>Заявка на перевірці</b>",
                  "approved": "✅ <b>Тест підтверджено</b>", "rejected": "❌ <b>Заявку відхилено</b>"}
        if not admin and active and order["status"] == "rejected":
            sections.append("❌ <b>Продовження відхилено</b>\nЧинний абонемент залишається активним.")
        else:
            sections.append(labels[order["status"]])
        if admin:
            sections.append(f"🧾 <b>ID заявки</b>\n<code>AD-{escape(str(order['id']).upper())}</code>")
        if order["status"] == "awaiting":
            if not admin:
                sections.append("Додай тестову квитанцію кнопкою нижче.")
                keyboard.append([{"text": "📎 Тестова квитанція", "callback_data": PREFIX+"receipt:"+order["id"]}])
        elif order["status"] == "review" and admin:
            keyboard.append([{"text": "✅ Підтвердити 30 днів", "callback_data": PREFIX+"approve:"+order["id"]}])
        elif order["status"] == "review":
            sections.append("Тестову квитанцію отримано. Очікуємо підтвердження.")
            keyboard.append([{"text": "🔄 Оновити статус", "callback_data": PREFIX+"view"}])
        if admin and order["status"] in ("awaiting", "review"):
            keyboard.append([{"text": "❌ Відхилити заявку", "callback_data": PREFIX+"reject:"+order["id"]}])
    if not order or order["status"] in ("approved", "rejected"):
        keyboard.append([{"text": "🔄 Тест продовження" if active else "📝 Створити тестову заявку",
                          "callback_data": PREFIX+"create"}])
    repeated = {"Тестову заявку створено. Гроші не переказуй.",
                "Демонстраційну квитанцію позначено для перевірки.",
                "Підтверджено лише тестовий абонемент. Реальної оплати немає.",
                "Тестову заявку відхилено."}
    if note and note not in repeated:
        sections.append("ℹ️ "+escape(note))
    if admin:
        keyboard.append([{"text": "👤 Перегляд клієнта", "callback_data": PREFIX+"view"}])
    elif owner_controls:
        keyboard.append([{"text": "🛠 Деталі власника", "callback_data": PREFIX+"admin"}])
    return {"method": "sendMessage", "chat_id": OWNER, "text": "\n\n".join(sections), "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard}, "protect_content": True}


def handle(engine, settings, event, request=None, now=None):
    """Invoked only after Telegram webhook secret validation. No real funds."""
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now) or now <= 0:
        return None
    callback = event.get("callback_query")
    if callback is not None:
        if not isinstance(callback, dict) or not isinstance(callback.get("data"), str):
            return None
        data = callback["data"]
        if not data.startswith(PREFIX):
            return None
        sender, msg = callback.get("from"), callback.get("message")
    else:
        msg = event.get("message")
        sender = msg.get("from") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or not isinstance(sender, dict):
        return None
    chat = msg.get("chat")
    if (not isinstance(chat, dict) or type(sender.get("id")) is not int or sender["id"] != OWNER
            or sender.get("is_bot") or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] != OWNER):
        return None
    if callback is None:
        text = msg.get("text", "")
        if not isinstance(text, str):
            return None
        token = text.split()[0] if text.split() else ""
        command, _, mention = token.partition("@")
        if command not in COMMANDS or (mention and mention.lower() != telegram_setup.BOT_USERNAME.lower()):
            return None
        return render(view(engine), now, admin=command == "/subtest_admin", owner_controls=True)
    update_id = event.get("update_id")
    callback_id = callback.get("id")
    match = re.fullmatch(r"subtest:(view|admin|create|(?:receipt|approve|reject):[a-f0-9]{32})", data)
    if (match is None or type(update_id) is not int or not 0 <= update_id < 2**63
            or not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 256):
        return {"ok": True}
    action, _, order_id = data[len(PREFIX):].partition(":")
    if action in ("view", "admin"):
        state, note = view(engine), ""
    else:
        state, note = apply(engine, action, order_id, update_id, now)
    # Acknowledge only this owner's authenticated button; no messages are pushed.
    request = request or telegram_setup.call
    try:
        request(settings.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id}, timeout=5)
    except Exception:
        pass  # Committed synthetic state remains retry-safe; never log credentials.
    return render(state, now, note, admin=action in ("admin", "approve", "reject"), owner_controls=True)


def configure(engine, settings, request=None):
    """Only owner chat menu; preserve public, Stars and configured quota commands."""
    if not settings.configure_webhook or telegram_setup.webhook_status(engine)["status"] != "configured":
        return
    from .bot_commands import COMMANDS as public_commands
    request = request or telegram_setup.call
    quota_commands = []
    if settings.ria_quota_management_enabled and settings.admin_telegram_id == OWNER:
        quota_commands = [
            {"command": "check", "description": "Чому авто не надійшло: ID або посилання"},
            {"command": "quota", "description": "Залишок та ліміти запитів"},
            {"command": "quota_set", "description": "Вказати залишок активного пакета"},
        ]
    with Session(engine) as db:
        row = db.get(SourceProbe, "subscription-owner-menu-v2")
        if row and row.status == "configured":
            return
        response = request(settings.bot_token, "setMyCommands", {
            "scope": {"type": "chat", "chat_id": OWNER},
            "commands": public_commands+quota_commands+[
                {"command": "subtest", "description": "Приватний тест абонемента без оплати"},
                {"command": "subtest_admin", "description": "Деталі та підтвердження тестових заявок"},
                {"command": "paytest", "description": "Тест оплати 1 ⭐ (лише власник)"},
                {"command": "payment", "description": "Статус тестового доступу Stars"},
                {"command": "refundtest", "description": "Повернути тестову оплату Stars"},
                {"command": "paysupport", "description": "Підтримка тестової оплати"},
            ]})
        ok = response.get("ok") is True and response.get("result") is True
        db.merge(SourceProbe(id="subscription-owner-menu-v2", status="configured" if ok else "unavailable",
                             checked_at=time.time(), requests=0, result={}))
        db.commit()
