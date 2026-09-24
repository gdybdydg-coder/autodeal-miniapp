"""User-requested onboarding replies. Never replay an uncertain Telegram send."""
import asyncio
import logging
import time
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import telegram_setup
from .models import BotReply, Search, SourceBudget, SourceProbe, TelegramTest, User
from .ria_budget import BudgetLimits

COMMANDS = [
    {"command": "start", "description": "Почати та відкрити AUTODeal"},
    {"command": "help", "description": "Як налаштувати пошук"},
    {"command": "stats", "description": "Статистика (адмін)"},
    {"command": "stop", "description": "Зупинити всі сповіщення"},
]
WELCOME = (
    "🚘 Вітаємо в AUTODeal!\n\n"
    "Знаходимо нові оголошення AUTO.RIA за твоїми побажаннями та надсилаємо їх у цей чат.\n\n"
    "1️⃣ Натисни «Налаштувати пошук».\n"
    "2️⃣ Обери авто, бюджет та одну або кілька областей.\n"
    "3️⃣ Збережи підписку й натисни «Увімкнути сповіщення».\n\n"
    "📊 У картці показуємо орієнтовну ринкову ціну, коли оцінка доступна.\n"
    "🔔 Відстежуємо нові публікації після ввімкнення пошуку.\n\n"
    "/help — допомога\n/stop — зупинити всі сповіщення"
)
HELP = (
    "🔎 Як користуватися AUTODeal\n\n"
    "Відкрий застосунок, обери фільтри та створи підписку. "
    "У картці збереженої підписки натисни «Увімкнути сповіщення». "
    "Можна вибрати кілька областей в одному пошуку.\n\n"
    "🔔 Після ввімкнення стежимо за новими публікаціями. "
    "Статус перевірок видно в картці підписки та в налаштуваннях.\n"
    "📊 Ринкова ціна приблизна; якщо оцінки немає, це буде зазначено в картці.\n"
    "✏️ Зміна фільтрів ставить пошук на паузу — ввімкни його після перевірки змін.\n\n"
    "Якщо зв’язок із чатом не підтверджений, надішли /start. "
    "У налаштуваннях також є кнопка «Надіслати тест».\n\n"
    "/stop — зупинити всі сповіщення. Збережені фільтри залишаться."
)
STOPPED = (
    "⏸ Усі сповіщення AUTODeal зупинено.\n\n"
    "Твої фільтри збережені. Щоб відновити пошук, надішли /start "
    "і ввімкни потрібні підписки в застосунку."
)


def connection_confirmed(db, uid):
    test = db.get(TelegramTest, uid)
    return bool((test and test.state == "sent") or db.scalar(select(BotReply.id).where(
        BotReply.user_id == uid, BotReply.command == "/start", BotReply.state == "sent").limit(1)))


def stats_text(db, uid, admin_uid):
    if not admin_uid:
        logging.getLogger(__name__).warning("AUTODeal admin bootstrap requested by Telegram user %s", uid)
        return f"🔐 Адмін ще не налаштований. Ваш Telegram ID: {uid}"
    if uid != admin_uid:
        return "⛔ Команда доступна лише адміністратору."
    total = db.scalar(select(func.count()).select_from(User)) or 0
    active = db.scalar(select(func.count()).select_from(User).where(User.ready.is_(True))) or 0
    searches = db.scalar(select(func.count()).select_from(Search).where(Search.enabled.is_(True))) or 0
    owners = db.scalar(select(func.count(func.distinct(Search.user_id))).where(Search.enabled.is_(True))) or 0
    budget = db.get(SourceBudget, "auto_ria")
    if budget:
        limits = BudgetLimits.env()
        cutoff = time.time() - 86400
        used_day = sum(stamp > cutoff for stamp in budget.calls)
        remaining = max(0, limits.total - budget.total)
        grouped = lambda value: f"{value:,}".replace(",", " ")
        estimate = (f"≈{remaining / used_day:.1f}".replace(".", ",") + " дн."
                    if used_day else "поки немає даних")
        quota = ("\n\n📡 Квота AUTO.RIA (локальний облік):\n"
                 f"За 24 год: {grouped(used_day)} запитів\n"
                 f"Залишок за нашим лімітом: {grouped(remaining)}\n"
                 f"За поточного темпу: {estimate}\n"
                 "Пакет провайдера та дату поновлення перевіряй окремо.")
    else:
        quota = "\n\n📡 Облік квоти AUTO.RIA ще недоступний."
    return (
        "📊 AUTODeal — статистика\n\n"
        f"👥 Усього користувачів: {total}\n"
        f"🟢 Підключені до бота: {active}\n"
        f"🔎 Активних пошуків: {searches}\n"
        f"🚘 Користувачів з активним пошуком: {owners}"
        + quota
    )


def payload(command, uid, release, text=None):
    label = "Налаштувати пошук" if command == "/start" else "Відкрити AUTODeal"
    return {"chat_id": uid, "text": text or {"/start": WELCOME, "/help": HELP, "/stop": STOPPED}[command],
            "reply_markup": {"inline_keyboard": [[{"text": label, "web_app": {
                "url": telegram_setup.APP_URL + ("?" + urlencode({"v": release}) if release else "")}}]]}}


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
                result = request(settings.bot_token, "sendMessage", payload(
                    reply.command, reply.user_id, settings.miniapp_release,
                    stats_text(db, reply.user_id, settings.admin_telegram_id) if reply.command == "/stats" else None))
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
    probe_id = "telegram-commands-v1"
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        if row and row.status == "configured":
            return
        result = request(settings.bot_token, "setMyCommands", {"commands": COMMANDS})
        ok = result.get("ok") is True and result.get("result") is True
        db.merge(SourceProbe(id=probe_id, status="configured" if ok else "unavailable",
                            checked_at=time.time(), requests=0, result={}))
        db.commit()


async def run(engine, settings, stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(deliver_one, engine, settings)
        except Exception:
            logging.getLogger(__name__).error("Bot command reply processing failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
