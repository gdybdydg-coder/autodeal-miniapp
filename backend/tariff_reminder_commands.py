"""Private Telegram controls for the daily, first-purchase reminder campaign.

Only explicit reminder actions change category preferences. Administrative
controls verify the protected owner configuration before opening a DB session.
The webhook returns a reply payload; this module never sends advertisements.
"""
from html import escape
import time

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, telegram_setup
from .manual_payments import ReviewError
from .subscription_promotion import EligibilityUnavailable

PREFIX = "ad-reminder:"
OFF_CONFIRMATION = "🔕 Нагадування про придбання тарифу вимкнено"
UNAVAILABLE = "Налаштування нагадувань тимчасово недоступні. Спробуй пізніше."


def _api():
    from . import tariff_reminders
    return tariff_reminders


def _number(value):
    return str(value) if type(value) is int and value >= 0 else "невідомо"


def _admin_card(uid, data):
    enabled = data.get("enabled") is True
    lines = ["🛠 <b>Нагадування про придбання тарифу</b>",
             "Стан: " + ("✅ Увімкнено" if enabled else "🔕 Вимкнено"),
             "Розклад: щодня о 09:00 · Europe/Kyiv",
             "Вікно надсилання: 09:00–09:30 за Києвом",
             "Наступний запуск: " + escape(str(data.get("next_run_kyiv") or "не запланований")),
             "Придатних отримувачів зараз: " + _number(data.get("eligible_recipients")),
             "Кількість перевіряється знову перед запуском і надсиланням."]
    last = data.get("last_result")
    if isinstance(last, dict) and type(last.get("selected")) is int:
        errors = last.get("errors")
        if type(errors) is not int:
            temporary, permanent = last.get("temporary_errors"), last.get("permanent_errors")
            errors = temporary + permanent if type(temporary) is int and type(permanent) is int else None
        lines.append("Останній запуск: обрано " + _number(last.get("selected"))
                     + ", надіслано " + _number(last.get("sent"))
                     + ", виключено " + _number(last.get("excluded"))
                     + ", помилки " + _number(errors)
                     + ", невизначено " + _number(last.get("uncertain")))
        lines.append("«Надіслано» означає підтвердження Telegram API, а не прочитання.")
    else:
        lines.append("Запусків цієї кампанії ще не було.")
    text = data.get("text")
    if isinstance(text, str):
        lines.append("<b>Текст нагадування</b>\n" + text)
    lines.append("Кнопки: 💳 Переглянути тариф · 🔕 Не нагадувати")
    toggle = {"text": "🔕 Вимкнути кампанію" if enabled else "✅ Увімкнути кампанію",
              "callback_data": PREFIX + ("admin:off" if enabled else "admin:on")}
    result = billing.message(uid, "\n\n".join(lines), [[toggle], [{
        "text": "🔄 Оновити стан", "callback_data": PREFIX + "admin:view"}]])
    result["protect_content"] = True
    return result


def _user_card(uid):
    return billing.message(uid,
        "🔔 <b>Нагадування про придбання тарифу</b>\n\n"
        "За твоїм дозволом — щодня о 09:00 за Києвом, доки ти ще не придбав тариф.\n"
        "Обери, чи хочеш отримувати ці нагадування. Налаштування пошуку й доступу збережуться.",
        [[{"text": "🔔 Дозволити щоденні нагадування", "callback_data": PREFIX + "on"}],
         [{"text": "🔕 Не нагадувати", "callback_data": PREFIX + "off"}]])


def handle(engine, settings, event, request=None, now=None):
    if not isinstance(event, dict):
        return None
    cb = event.get("callback_query")
    is_callback = cb is not None
    if is_callback and not isinstance(cb, dict):
        return None
    msg = cb.get("message", {}) if is_callback else event.get("message", {})
    if not isinstance(msg, dict):
        return None
    sender = cb.get("from", {}) if is_callback else msg.get("from", {})
    chat = msg.get("chat", {})
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    uid = sender.get("id")
    if (type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot")
            or chat.get("type") != "private" or type(chat.get("id")) is not int
            or chat.get("id") != uid):
        return None
    text = msg.get("text", "")
    action = cb.get("data", "") if is_callback else ""
    if not isinstance(text, str) or not isinstance(action, str):
        return None
    pieces = text.split()
    command, _, mention = (pieces[0] if pieces else "").partition("@")
    if mention and mention.lower() != telegram_setup.BOT_USERNAME.lower():
        return None
    actions = {PREFIX + part for part in ("on", "off", "admin:on", "admin:off", "admin:view")}
    if is_callback and action not in actions:
        return None
    if not is_callback and command not in ("/reminders", "/tariff_reminders"):
        return None
    now = time.time() if now is None else now
    update_id = event.get("update_id")
    if type(update_id) is not int or not 0 <= update_id < 2**63:
        return {"ok": True}
    if not is_callback and (type(msg.get("date")) is not int or not 0 <= now-msg["date"] <= 300):
        return {"ok": True}
    if is_callback and (not isinstance(cb.get("id"), str) or not cb["id"]):
        return {"ok": True}
    admin_action = action.startswith(PREFIX + "admin:") if is_callback else command == "/tariff_reminders"
    if admin_action and (not billing.verified_admin(settings) or uid != settings.admin_telegram_id):
        return {"ok": True}
    if is_callback:
        try:
            (request or telegram_setup.call)(settings.bot_token, "answerCallbackQuery",
                {"callback_query_id": cb["id"]}, timeout=3)
        except Exception:
            # A failed UI acknowledgment must not discard an explicit opt-out.
            pass
    try:
        api = _api()
        if admin_action:
            verb = action.removeprefix(PREFIX + "admin:") if is_callback else (
                pieces[1] if len(pieces) == 2 else "view")
            if verb in ("on", "off"):
                data = api.set_enabled(engine, settings, uid, verb == "on", now, update_id=update_id)
            else:
                with Session(engine) as db:
                    data = api.snapshot(db, settings, now)
            return _admin_card(uid, data)
        if not is_callback:
            return _user_card(uid)
        enabled = action == PREFIX + "on"
        enabled = api.set_preference(engine, settings, uid, enabled, update_id, now)
        return billing.message(uid, "🔔 Щоденні нагадування про придбання тарифу увімкнено."
            "\nЧас: 09:00 за Києвом. Вимкнути їх можна командою /reminders."
            if enabled else OFF_CONFIRMATION)
    except PermissionError:
        return {"ok": True}
    except (SQLAlchemyError, EligibilityUnavailable, ReviewError):
        return billing.message(uid, UNAVAILABLE)
