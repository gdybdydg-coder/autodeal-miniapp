"""Owner-confirmed package snapshots and private, durable quota warnings.

No provider balance is inferred from payment, no purchases, no accounting reset.
Existing SourceProbe rows hold bounded command state; no schema migration.
"""
import logging
import re
import secrets
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import SourceBudget, SourceProbe, User
from .ria_budget import BudgetLimits, TOTAL_OVERRIDE_ID, total_cap

COMMANDS = {"/quota", "/quota_set", "/quota_confirm", "/quota_cancel"}
OUTBOX = "quota-outbox-"
DRAFT = "quota-draft-"
ALERT_STATE = "quota-alert-state-v1"
TTL = 300
MAX_REMAINING = 100_000_000
log = logging.getLogger(__name__)


def grouped(value):
    return f"{value:,}".replace(",", " ")


def snapshot(db, now=None):
    from .ria_search import budget_state
    now = time.time() if now is None else now
    limits = BudgetLimits.env()
    row = db.get(SourceBudget, "auto_ria")
    if row is None:
        return None
    cap = total_cap(db, limits)
    override = db.get(SourceProbe, TOTAL_OVERRIDE_ID)
    effective = (override and override.status == "confirmed"
                 and override.result.get("base_total_cap") == limits.total)
    return {"remaining": max(0, cap-row.total), "cap": cap, "used": row.total,
            "used_day": sum(t > now-86400 for t in row.calls),
            "used_hour": sum(t > now-3600 for t in row.calls),
            "hourly": limits.hourly, "daily": limits.daily,
            "generation": override.result["generation"] if effective else f"env-{limits.total}",
            "gate": budget_state(row, now, limits, db=db)}


def gate_text(state):
    labels = {"available": "Запити доступні.", "total": "Внутрішній сукупний ліміт вичерпано.",
              "hourly": "Досягнуто годинного ліміту.", "daily": "Досягнуто добового ліміту.",
              "upstream": "Діє пауза після відповіді AUTO.RIA."}
    gate = state["gate"]
    text = labels.get(gate["reason"], "Стан квоти недоступний.")
    if gate.get("retry_after_seconds"):
        text += f" Повторна перевірка приблизно через {gate['retry_after_seconds']} с."
    return text


def status_text(db):
    state = snapshot(db)
    if state is None:
        return "Облік запитів ще недоступний."
    estimate = (f"≈{state['remaining']/state['used_day']:.1f} дн.".replace(".", ",")
                if state["used_day"] else "недостатньо даних")
    return ("📡 AUTODeal — бюджет запитів\n\n"
            f"Залишок за внутрішнім лімітом: {grouped(state['remaining'])}\n"
            f"За годину: {grouped(state['used_hour'])} / {grouped(state['hourly'])}\n"
            f"За 24 год: {grouped(state['used_day'])} / {grouped(state['daily'])}\n"
            f"За поточного темпу вистачить на {estimate}\n{gate_text(state)}\n\n"
            "Це локальний облік, а не онлайн-баланс AUTO.RIA.\n"
            "Після активації пакета перевір залишок у кабінеті та надішли:\n"
            "/quota_set КІЛЬКІСТЬ\n"
            "Наприклад: /quota_set 1000000 — лише якщо саме стільки залишилось.\n"
            "Бот попросить окреме підтвердження. Оплачений пакет у черзі ще не активний.")


def respond(db, uid, key, text, now, **extra):
    ident = OUTBOX + key
    if db.get(SourceProbe, ident) is None:
        db.add(SourceProbe(id=ident, status="pending", checked_at=now, requests=0,
                           result={"uid": uid, "text": text, "created_at": now, **extra}))


def handle(db, settings, uid, command, text, update_id, command_at):
    """Caller authenticates the webhook and holds the existing user lock."""
    now = time.time()
    if not settings.ria_quota_management_enabled or not settings.admin_telegram_id or uid != settings.admin_telegram_id:
        return  # Do not reveal budget data or create privileged state for clients.
    if not -30 <= now-command_at <= TTL:
        respond(db, uid, str(update_id), "Команда застаріла. Надішли /quota ще раз.", now)
        return
    args = text.split()[1:]
    message = "Невірна команда. Інструкція: /quota"
    if command == "/quota" and not args:
        message = status_text(db)
    elif command == "/quota_set" and len(args) == 1:
        value = args[0]
        if not (re.fullmatch(r"[0-9]{1,9}", value) and int(value) <= MAX_REMAINING):
            message = "Вкажи цілий залишок від 0 до 100 000 000 без пробілів: /quota_set 1000000"
        else:
            # Same row lock as every paid request: the draft fixes its ceiling
            # now, so calls spent before confirmation cannot be credited twice.
            budget = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
            if budget is None:
                message = "Облік запитів ще недоступний."
            else:
                state = snapshot(db, now)
                nonce = secrets.token_hex(4)
                db.add(SourceProbe(id=DRAFT+nonce, status="pending", checked_at=now, requests=0,
                    result={"uid": uid, "remaining": int(value), "snapshot_total": budget.total,
                            "total_cap": budget.total+int(value), "base_total_cap": BudgetLimits.env().total,
                            "previous_generation": state["generation"]}))
                message = (f"Підтвердити залишок активного пакета: {grouped(int(value))} запитів?\n\n"
                    "Перевір цю цифру в кабінеті AUTO.RIA. Підтверджуй лише вже активний пакет. "
                    "Це заміна залишку, а не додавання пакета поверх наявного.\n"
                    "Витрати за час підтвердження будуть відняті. Лічильники, годинний і добовий ліміти збережуться.\n\n"
                    f"Підтвердити протягом 5 хвилин:\n/quota_confirm {nonce}\n"
                    f"Скасувати:\n/quota_cancel {nonce}")
    elif command in {"/quota_confirm", "/quota_cancel"} and len(args) == 1 and re.fullmatch(r"[0-9a-f]{8}", args[0]):
        # Serialize confirmations with accounting; a stale second draft cannot
        # replace a newer confirmed snapshot even if its nonce is different.
        budget = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
        draft = db.get(SourceProbe, DRAFT+args[0])
        if not draft or draft.result.get("uid") != uid or draft.status != "pending":
            message = "Це підтвердження вже використане, скасоване або не існує. Перевір /quota."
        elif command == "/quota_cancel":
            draft.status = "cancelled"
            message = "Зміну бюджету скасовано. Чинний залишок збережено."
        elif (not budget or now-draft.checked_at > TTL
              or draft.result["base_total_cap"] != BudgetLimits.env().total
              or draft.result["previous_generation"] != snapshot(db, now)["generation"]):
            draft.status = "expired"
            message = "Підтвердження застаріло або бюджет уже змінено. Перевір залишок і надішли /quota_set знову."
        else:
            data = {**draft.result, "generation": args[0], "confirmed_at": now}
            db.merge(SourceProbe(id=TOTAL_OVERRIDE_ID, status="confirmed", checked_at=now, requests=0, result=data))
            draft.status = "confirmed"
            db.flush()
            remaining = max(0, data["total_cap"]-budget.total)
            message = (f"✅ Внутрішній залишок поновлено: {grouped(remaining)} запитів.\n"
                       "Лічильники витрат не обнулено.\n" + gate_text(snapshot(db, now)) +
                       "\nМонітор підхопить доступний бюджет автоматично; зупинені підписки залишаться зупиненими.")
            log.info("AUTO.RIA package budget confirmed remaining=%s", remaining)
    respond(db, uid, str(update_id), message, now)


def warning_level(state):
    remaining, pace = state["remaining"], state["used_day"]
    if remaining == 0:
        return 3
    if remaining <= max(1000, pace):
        return 2
    if remaining <= max(10000, pace*3):
        return 1
    return 0


def public_status(engine, settings):
    """Operational state only: no chat identifiers, nonces or private replies."""
    with Session(engine) as db:
        menu = db.get(SourceProbe, "telegram-quota-owner-menu-v1")
        alert = db.get(SourceProbe, ALERT_STATE)
        override = db.get(SourceProbe, TOTAL_OVERRIDE_ID)
        confirmed = bool(override and override.status == "confirmed"
                         and override.result.get("base_total_cap") == BudgetLimits.env().total)
        return {"enabled": settings.ria_quota_management_enabled,
                "owner_configured": bool(settings.admin_telegram_id),
                "menu_configured": bool(menu and menu.status == "configured"
                    and menu.result.get("uid") == settings.admin_telegram_id),
                "alerts_checked_at": alert.checked_at if alert else None,
                "balance_source": "owner_confirmed_snapshot" if confirmed else "server_limit",
                "provider_balance_read_automatically": False}


def check_warning(engine, settings):
    if not (settings.ria_quota_management_enabled and settings.live and settings.admin_telegram_id):
        return
    now = time.time()
    with Session(engine) as db:
        # Lock order matches owner commands and never holds the budget over HTTP.
        owner = db.get(User, settings.admin_telegram_id, with_for_update=True)
        if not owner or not owner.ready:
            return
        budget = db.scalar(select(SourceBudget).where(SourceBudget.id == "auto_ria").with_for_update())
        if budget is None:
            return
        previous = db.get(SourceProbe, ALERT_STATE)
        if previous and now-previous.checked_at < 60:
            return
        state = snapshot(db, now)
        level = warning_level(state)
        old = previous.result if previous and previous.result.get("generation") == state["generation"] else {}
        highest = old.get("level", 0)
        if level > highest:
            title = {1: "⚠️ Запас запитів зменшується", 2: "⚠️ Малий залишок запитів", 3: "⏸ Внутрішній бюджет вичерпано"}[level]
            text = (f"{title}\n\nЗалишок за локальним обліком: {grouped(state['remaining'])}\n"
                    f"Витрачено за 24 год: {grouped(state['used_day'])}\n"
                    "Перевір активний пакет у кабінеті AUTO.RIA. Оплата нового пакета ще не означає його активацію.\n"
                    "Після активації вкажи фактичний залишок через /quota_set.\n"
                    "Деталі: /quota. Автоматичних покупок немає.")
            respond(db, settings.admin_telegram_id, f"warning-{state['generation']}-{level}", text, now,
                    warning_level=level, generation=state["generation"])
        db.merge(SourceProbe(id=ALERT_STATE, status="checked", checked_at=now, requests=0,
                            result={"generation": state["generation"], "level": max(level, highest)}))
        db.commit()


def deliver_one(engine, settings, request=None):
    if not settings.ria_quota_management_enabled or not settings.admin_telegram_id:
        return "disabled"
    from .telegram_setup import call
    request = request or call
    now = time.time()
    with Session(engine) as db:
        row = db.scalar(select(SourceProbe).where(SourceProbe.id.startswith(OUTBOX),
            SourceProbe.status == "pending", SourceProbe.checked_at <= now)
            .order_by(SourceProbe.checked_at, SourceProbe.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return "idle"
        data, ident = dict(row.result), row.id
        if data.get("uid") != settings.admin_telegram_id or now-data.get("created_at", 0) > TTL:
            row.status = "cancelled"
            db.commit()
            return "cancelled"
        changed = db.execute(update(SourceProbe).where(SourceProbe.id == ident, SourceProbe.status == "pending")
            .values(status="sending", requests=SourceProbe.requests+1, checked_at=now))
        db.commit()  # Never replay a request whose Telegram acceptance is unknown.
        if changed.rowcount != 1:
            return "busy"
        row = db.get(SourceProbe, ident)
        owner = db.get(User, settings.admin_telegram_id, with_for_update=True)
        if not owner:
            row.status = "cancelled"
        elif data.get("warning_level") and (not owner.ready or
                (state := snapshot(db, now)) is None or state["generation"] != data["generation"]
                or warning_level(state) < data["warning_level"]):
            row.status = "cancelled"
        else:
            try:
                result = request(settings.bot_token, "sendMessage", {"chat_id": settings.admin_telegram_id, "text": data["text"]})
                message_id = (result.get("result") or {}).get("message_id")
                if result.get("ok") is True and type(message_id) is int and message_id > 0:
                    row.status = "sent"
                elif result.get("ok") is False and result.get("error_code") == 429 and row.requests < 3:
                    delay = (result.get("parameters") or {}).get("retry_after", 5)
                    row.status, row.checked_at = "pending", now+min(300, max(1, delay if type(delay) is int else 5))
                else:
                    row.status = "failed" if result.get("ok") is False else "uncertain"
            except Exception:
                row.status = "uncertain"
        db.commit()
        return row.status


def configure(engine, settings, request=None):
    """Expose quota commands only in the configured owner's private chat menu."""
    if not (settings.ria_quota_management_enabled and settings.admin_telegram_id):
        return
    from . import bot_commands, telegram_setup
    if telegram_setup.webhook_status(engine)["status"] != "configured":
        return
    request = request or telegram_setup.call
    ident = "telegram-quota-owner-menu-v1"
    with Session(engine) as db:
        row = db.get(SourceProbe, ident)
        if row and row.status == "configured" and row.result.get("uid") == settings.admin_telegram_id:
            return
        commands = [*bot_commands.COMMANDS,
                    {"command": "quota", "description": "Залишок та ліміти запитів"},
                    {"command": "quota_set", "description": "Вказати залишок активного пакета"}]
        try:
            result = request(settings.bot_token, "setMyCommands", {"commands": commands,
                             "scope": {"type": "chat", "chat_id": settings.admin_telegram_id}})
            ok = result.get("ok") is True and result.get("result") is True
        except Exception:
            ok = False
        db.merge(SourceProbe(id=ident, status="configured" if ok else "unavailable", checked_at=time.time(),
                            requests=0, result={"uid": settings.admin_telegram_id}))
        db.commit()
