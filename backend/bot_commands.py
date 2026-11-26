"""User-requested onboarding replies. Never replay an uncertain Telegram send."""
import asyncio
import logging
import time
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import telegram_setup
from .models import BotReply, Search, SourceBudget, SourceProbe, TelegramTest, User
from .ria_budget import BudgetLimits, total_cap

COMMANDS = [
    {"command": "start", "description": "Почати та відкрити AUTODeal"},
    {"command": "help", "description": "Як налаштувати радари"},
    {"command": "stats", "description": "Статистика (адмін)"},
    {"command": "stop", "description": "Зупинити всі сповіщення"},
    {"command": "reminders", "description": "Налаштувати нагадування про підписку"},
]
WELCOME = (
    "🚘 Вітаємо в AUTODeal!\n\n"
    "Знаходимо вигідні авто на AUTO.RIA й повідомляємо про них у цьому чаті.\n\n"
    "1️⃣ Відкрийте «Мої радари». У кожного користувача є 3 місця.\n"
    "2️⃣ Оберіть марки й моделі, категорію ціни, області, роки та мінімальну вигоду.\n"
    "3️⃣ Збережіть радар і натисніть «Увімкнути» за наявності активної підписки.\n\n"
    "⚙️ Створювати й редагувати радари можна без активної підписки.\n"
    "🔔 Відстежуємо нові публікації після ввімкнення радара.\n"
    "💳 Без активної підписки надсилаємо нагадування про тариф раз на два дні о 09:00.\n"
    "/help — допомога\n/reminders — нагадування про підписку\n/stop — зупинити всі сповіщення"
)
HELP = (
    "🔎 Як користуватися AUTODeal\n\n"
    "Відкрийте «Мої радари» та створіть до 3 радарів. У кожного — власні марки, "
    "моделі, одна категорія ціни, області, роки й відсоток вигоди. "
    "Кількість місць не залежить від тарифу.\n\n"
    "🔔 Після ввімкнення стежимо за новими публікаціями. "
    "Пауза зберігає налаштування; видалення звільняє місце.\n"
    "📊 Поріг ціни: підтверджена нижня межа AUTO.RIA × 0,95 × "
    "(1 − вибраний відсоток / 100). Це ціновий орієнтир, не гарантований прибуток.\n"
    "✏️ Змінюйте умови кнопкою «Редагувати» у картці радара.\n\n"
    "💳 Моніторинг потребує активної підписки. Після її завершення радари "
    "стають на паузу. Після поновлення ввімкніть їх вручну. "
    "Створення та редагування доступні й без активної підписки.\n"
    "Нагадування про тариф надходять раз на два дні о 09:00. Команда /stop вимикає всі сповіщення.\n\n"
    "Якщо чат не підключений, надішліть /start. У налаштуваннях є кнопка «Надіслати тест».\n\n"
    "/reminders — керування нагадуваннями про підписку.\n/stop — зупинити всі сповіщення. Радари залишаться збереженими."
)
STOPPED = (
    "⏸ Усі сповіщення AUTODeal зупинено.\n\n"
    "Ваші радари й налаштування збережені. Щоб відновити моніторинг, надішліть /start "
    "і вручну ввімкніть потрібні радари за наявності активної підписки.\n"
    "🔕 Нагадування про підписку також вимкнено. Щоб знову отримувати їх, надішліть /reminders."
)


def connection_confirmed(db, uid):
    test = db.get(TelegramTest, uid)
    return bool((test and test.state == "sent") or db.scalar(select(BotReply.id).where(
        BotReply.user_id == uid, BotReply.command == "/start", BotReply.state == "sent").limit(1)))


STATS_UNAVAILABLE = "⚠️ Не вдалося отримати статистику. Спробуйте пізніше."


def stats_text(db, uid, admin_uid, *, settings=None):
    if not admin_uid:
        logging.getLogger(__name__).warning("AUTODeal admin bootstrap requested by Telegram user %s", uid)
        return f"🔐 Адмін ще не налаштований. Ваш Telegram ID: {uid}"
    if uid != admin_uid:
        return f"⛔ Ви не є адміністратором. Ваш Telegram ID: {uid}"
    try:
        return _stats_text(db, admin_uid, settings=settings)
    except (SQLAlchemyError, ValueError):
        logging.getLogger(__name__).error("Admin statistics unavailable")
        return STATS_UNAVAILABLE


def _stats_text(db, admin_uid, *, settings=None):
    from . import operational_stats
    from . import purchase_stats
    purchases = purchase_stats.counts(db, settings, admin_uid=admin_uid)
    clients = select(User.id).where(User.id.not_in(
        purchase_stats.excluded_user_ids(settings, admin_uid=admin_uid)))
    active = db.scalar(select(func.count()).select_from(User).where(
        User.ready.is_(True), User.id.in_(clients))) or 0
    from . import radar_monitor
    if radar_monitor.enabled(db):
        from .radar_models import Radar
        eligible = (Radar.status == "active", Radar.active_until > time.time(),
                    Radar.user_id.in_(clients), radar_monitor.subscription_clause(Radar.user_id))
        searches = db.scalar(select(func.count()).select_from(Radar).where(*eligible)) or 0
        owners = db.scalar(select(func.count(func.distinct(Radar.user_id))).where(*eligible)) or 0
        search_label, owner_label = "Активних радарів", "Користувачів з активним радаром"
    else:
        searches = db.scalar(select(func.count()).select_from(Search).where(
            Search.enabled.is_(True), Search.user_id.in_(clients))) or 0
        owners = db.scalar(select(func.count(func.distinct(Search.user_id))).where(
            Search.enabled.is_(True), Search.user_id.in_(clients))) or 0
        search_label, owner_label = "Активних пошуків", "Користувачів з активним пошуком"
    budget = db.get(SourceBudget, "auto_ria")
    if budget:
        limits = BudgetLimits.env()
        cutoff = time.time() - 86400
        used_day = sum(stamp > cutoff for stamp in budget.calls)
        remaining = max(0, total_cap(db, limits) - budget.total)
        grouped = lambda value: f"{value:,}".replace(",", " ")
        estimate = (f"≈{remaining / used_day:.1f}".replace(".", ",") + " дн."
                    if used_day else "поки немає даних")
        quota = ("\n\n📡 Квота AUTO.RIA (локальний облік):\n"
                 f"За 24 год: {grouped(used_day)} запитів\n"
                 f"Залишок за нашим лімітом: {grouped(remaining)}\n"
                 f"За поточного темпу: {estimate}\n"
                 "Це внутрішній ліміт бота, а не баланс пакета AUTO.RIA.\n"
                 "Фактичний залишок пакета — у кабінеті AUTO.RIA.")
        if remaining == 0:
            quota += "\n⏸ Внутрішній ліміт вичерпано; у пакеті AUTO.RIA ще можуть бути запити."
    else:
        quota = "\n\n📡 Облік квоти AUTO.RIA ще недоступний."
    return (
        "📊 AUTODeal — статистика\n\n"
        f"👥 Усього клієнтів: {purchases['total']}\n"
        f"🟢 Підключені до бота: {active}\n"
        f"🔎 {search_label}: {searches}\n"
        f"🚘 {owner_label}: {owners}"
        + "\n\n" + purchase_stats.text(purchases)
        + "\n\n" + operational_stats.text(db, settings) + quota
    )


def payload(command, uid, release, text=None):
    label = "Мої радари" if command == "/start" else "Відкрити AUTODeal"
    return {"chat_id": uid, "text": text or {"/start": WELCOME, "/help": HELP, "/stop": STOPPED}[command],
            "reply_markup": {"inline_keyboard": [[{"text": label, "web_app": {
                "url": telegram_setup.APP_URL + ("?" + urlencode({"v": release}) if release else "")}}]]}}


def checkout_welcome(db, settings, body):
    """Offer checkout only in the welcome explicitly requested by this user."""
    if not settings.manual_payment_review_enabled:
        return body
    from . import billing
    state = billing.public_status(db, body["chat_id"], settings=settings)
    if state["payment_method"] != "bank_manual" or (state["expires_at"] or 0) > time.time():
        return body
    current = state.get("request")
    if current and current["state"] in ("created", "review", "clarification"):
        body["text"] += "\n\nЗаявку на оплату вже відкрито. Перегляньте її стан. Якщо вже сплатили — повторно не сплачуйте."
        button = {"text": "Статус оплати", "url": state["status_url"]}
    elif state["sales_enabled"]:
        # The direct purchase link accepts the exact disclosed current offer.
        # No campaign or marketing-consent record is created by this welcome.
        body["text"] += "\n\n💳 Підписка — 250 грн / 30 днів.\n\n" + state["terms"]
        button = {"text": "Придбати підписку · 250 грн", "url": state["purchase_url"]}
    else:
        return body
    body["reply_markup"]["inline_keyboard"].append([button])
    return body


def deliver_one(engine, settings, request=None):
    request = request or telegram_setup.call
    with Session(engine) as db:
        reply = db.scalar(select(BotReply).where(BotReply.state == "pending")
                          .order_by(BotReply.id).with_for_update(skip_locked=True).limit(1))
        if reply is None:
            return "idle"
        user = db.get(User, reply.user_id, with_for_update=True)
        if (not user or time.time() - reply.command_at > 300
                or (reply.command == "/start" and not user.ready)
                or (reply.command == "/stop" and user.ready)):
            reply.state = "cancelled"
            db.commit()
            return "cancelled"
        reply.state, reply.attempted_at = "sending", time.time()
        reply_id = reply.id
        db.commit()  # Claim before network I/O; no retry after a crash/timeout.
        reply = db.get(BotReply, reply_id)
        user = db.get(User, reply.user_id, with_for_update=True)
        if ((reply.command == "/start" and not user.ready)
                or (reply.command == "/stop" and user.ready)):
            reply.state = "cancelled"
        else:
            try:
                body = payload(
                    reply.command, reply.user_id, settings.miniapp_release,
                    stats_text(db, reply.user_id, settings.admin_telegram_id, settings=settings)
                    if reply.command == "/stats" else None)
                if reply.command == "/start":
                    body = checkout_welcome(db, settings, body)
                result = request(settings.bot_token, "sendMessage", body)
                message_id = (result.get("result") or {}).get("message_id")
                if result.get("ok") is True and type(message_id) is int and message_id > 0:
                    reply.state, reply.message_id = "sent", message_id
                else:
                    reply.state = "failed" if result.get("ok") is False else "uncertain"
            except Exception:
                reply.state = "uncertain"
        db.commit()
        return reply.state


def configure(engine, settings, request=None):
    if not settings.configure_webhook or telegram_setup.webhook_status(engine)["status"] != "configured":
        return
    request = request or telegram_setup.call
    bank_menu = settings.manual_payment_review_enabled
    probe_id = "telegram-commands-bank-v1" if bank_menu else "telegram-commands-v1"
    commands = COMMANDS + ([
        {"command": "subscription", "description": "Абонемент і статус оплати"},
        {"command": "terms", "description": "Умови абонемента"},
        {"command": "paysupport", "description": "Допомога з оплатою"},
    ] if bank_menu else [])
    with Session(engine) as db:
        from .radar_monitor import enabled as radars_enabled
        if radars_enabled(db):
            probe_id += "-radars-v1"
        probe_id += "-reminders-v2"
        row = db.get(SourceProbe, probe_id)
        if row and row.status == "configured":
            return
        result = request(settings.bot_token, "setMyCommands", {"commands": commands})
        ok = result.get("ok") is True and result.get("result") is True
        db.merge(SourceProbe(id=probe_id, status="configured" if ok else "unavailable",
                            checked_at=time.time(), requests=0, result={}))
        db.commit()


async def run(engine, settings, stop):
    from . import quota_management, listing_check
    next_warning = 0
    while not stop.is_set():
        try:
            await asyncio.to_thread(deliver_one, engine, settings)
        except Exception:
            logging.getLogger(__name__).error("Bot command reply processing failed")
        try:
            await asyncio.to_thread(listing_check.deliver_one, engine, settings)
        except Exception:
            logging.getLogger(__name__).error("Listing check reply processing failed")
        if settings.ria_quota_management_enabled:
            try:
                if time.monotonic() >= next_warning:
                    await asyncio.to_thread(quota_management.check_warning, engine, settings)
                    next_warning = time.monotonic()+60
                await asyncio.to_thread(quota_management.deliver_one, engine, settings)
            except Exception:
                logging.getLogger(__name__).error("Quota owner reply processing failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
