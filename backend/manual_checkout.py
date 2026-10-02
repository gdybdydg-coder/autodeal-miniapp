"""Bank checkout selected explicitly by the owner; no automatic payment verification."""
from html import escape
import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, manual_payments as m, manual_receipts, subscription_preview, telegram_setup
from .manual_payment_models import PaymentRequest
from .models import User

PREFIX = "ad-bank:"
TERMS_VERSION = "20261002-card-v1"
TERMS = (
    "250 грн / 30 днів пошуку відповідних оголошень за збереженими фільтрами та сповіщень. "
    "Оцінка ринкової ціни орієнтовна; повне охоплення всіх оголошень і купівля авто не гарантуються. "
    "Оплата — переказ за реквізитами. Власник особисто перевіряє фактичне зарахування "
    "та підтверджує доступ. Натискання «Я оплатив» і квитанція не відкривають доступ автоматично. "
    "30 днів від підтвердження; при продовженні невикористані дні зберігаються. "
    "Автоматичних списань немає. Зупинений пошук сам не вмикається. "
    "Питання щодо зарахування, спірних платежів і повернення коштів: /paysupport та опис питання. "
    "Рішення щодо повернення приймає власник; відхилення заявки не означає повернення."
)
OFFER = {"method": "bank_manual", "currency": "UAH", "amount_minor": 25000,
         "days": 30, "terms_version": TERMS_VERSION, "terms": TERMS}
LABELS = {"created": "Очікує твого переказу", "review": "Очікує перевірки власником",
          "clarification": "Потрібне уточнення", "approved": "Підтверджено", "rejected": "Відхилено"}


def offer_valid(offer):
    return isinstance(offer, dict) and all(offer.get(k) == v for k, v in OFFER.items())


def require_sales(db):
    ctrl = billing.control(db)
    if not m.public_creation_allowed() or not ctrl or not ctrl.sales or not offer_valid(ctrl.offer):
        raise m.ReviewError("manual_sales_closed", 409)
    if subscription_preview.receiving_profile() is None:
        raise m.ReviewError("recipient_configuration_unavailable", 503)


def latest(db, uid):
    return db.scalar(select(PaymentRequest).where(PaymentRequest.user_id == uid)
                     .order_by(PaymentRequest.created_at.desc(), PaymentRequest.id.desc()).limit(1))


def status(engine, settings, uid):
    m.enabled(settings)
    with Session(engine) as db:
        ctrl = billing.control(db)
        until = billing.expiry(db, uid)
        request = latest(db, uid)
        try:
            require_sales(db)
            sales = True
        except m.ReviewError:
            sales = False
        return {"sales_enabled": sales, "paid_access_required": bool(ctrl and ctrl.enforce),
                "expires_at": until, "request": m.public(request) if request else None,
                "terms_version": TERMS_VERSION, "terms": TERMS, "amount_minor": 25000, "days": 30}


def create(engine, settings, uid, now, version, *, name="Клієнт", username=None):
    m.enabled(settings)
    # Register an authenticated requester without enabling searches or notifications.
    with m.mutation(engine, settings) as db:
        require_sales(db)
        if version != TERMS_VERSION:
            raise m.ReviewError("terms_changed", 409)
        if db.get(User, uid) is None:
            db.add(User(id=uid, ready=False))
    return m.create_request(engine, settings, uid, now, name=name, username=username, accepted_terms=version)


def requisites(engine, settings, uid, code):
    m.enabled(settings)
    with Session(engine) as db:
        row = m.request_row(db, code, uid)
        if row.state not in ("created", "clarification", "review"):
            raise m.ReviewError("request_already_reported", 409)
        require_sales(db)
        return {"request": m.public(row), "recipient": subscription_preview.receiving_profile()}


def response(uid, sections, buttons):
    payload = billing.message(uid, "\n\n".join(sections), buttons)
    payload["protect_content"] = True
    return payload


def overview(engine, settings, uid, now):
    data = status(engine, settings, uid)
    until = data["expires_at"]
    lines = ["🚘 <b>AutoDeal</b>"]
    if until > now:
        lines.append("✅ Доступ до <b>"+billing.date_text(until)+"</b> (Київ).")
    elif data["paid_access_required"]:
        lines.append("🔒 <b>Доступ завершено</b>" if until else "🔒 <b>Підключи доступ</b>")
        lines.append("Щоб отримувати авто за своїми фільтрами, підключи підписку.")
    else:
        lines.append("Пошук зараз безкоштовний.")
    lines.append("💳 <b>250 грн / 30 днів</b>")
    buttons = []
    row = data["request"]
    if row and row["state"] not in ("approved", "rejected"):
        if row.get("awaiting_receipt"):
            lines.append(manual_receipts.PROMPT)
            buttons.append([{"text": "📸 Надіслати скриншот", "callback_data": PREFIX+"receipt:"+row["code"]}])
        elif row["state"] == "review":
            lines.append("🕓 Оплата на перевірці. Повторно не сплачуй.")
            buttons.append([{"text": "🏦 Реквізити", "callback_data": PREFIX+"details:"+row["code"]}])
            buttons.append([{"text": "🔄 Перевірити оплату", "callback_data": PREFIX+"status:"+row["code"]}])
        elif data["sales_enabled"]:
            buttons.append([{"text": "💳 Перейти до оплати", "callback_data": PREFIX+"details:"+row["code"]}])
        else:
            buttons.append([{"text": "Статус оплати", "callback_data": PREFIX+"status:"+row["code"]}])
    elif data["sales_enabled"]:
        lines.append("Доступ після перевірки оплати. Без автосписань.\nНатискаючи кнопку, ти приймаєш умови підписки.")
        buttons.append([{"text": "💳 Продовжити за 250 грн" if until > now or until > 0 else "💳 Підключити за 250 грн",
                         "callback_data": PREFIX+"accept:"+TERMS_VERSION}])
    if not data["sales_enabled"]:
        lines.append("Нові оплати тимчасово недоступні.")
    buttons.append([{"text": "Умови", "callback_data": PREFIX+"terms"}])
    return response(uid, lines, buttons)


def terms(engine, settings, uid, full=False):
    data = status(engine, settings, uid)
    lines = ["🚘 <b>Підписка AutoDeal</b>", "💳 <b>250 грн / 30 днів</b>"]
    if full:
        lines = ["📄 <b>Повні умови</b>", escape(TERMS)]
    else:
        lines.append("Авто за твоїми фільтрами та сповіщення в Telegram.")
        lines.append("Доступ на 30 днів після перевірки оплати власником.\nПри продовженні залишок днів зберігається. Без автосписань.")
        lines.append("Оплата й повернення: /paysupport")
    buttons = []
    if data["sales_enabled"]:
        lines.append("Натискаючи кнопку, ти приймаєш умови підписки.")
        buttons.append([{"text": "💳 Перейти до оплати · 250 грн", "callback_data": PREFIX+"accept:"+TERMS_VERSION}])
    else:
        lines.append("Нові оплати тимчасово недоступні.")
    if not full:
        buttons.append([{"text": "Повні умови", "callback_data": PREFIX+"full_terms"}])
    buttons.append([{"text": "← Назад", "callback_data": PREFIX+"view"}])
    return response(uid, lines, buttons)


def request_card(engine, settings, uid, code):
    row = m.get_status(engine, settings, uid, code)
    lines = ["🚘 <b>AutoDeal · Оплата</b>"]
    buttons = []
    if row["state"] == "approved":
        lines.append("✅ Доступ до <b>"+billing.date_text(row["expires_at"])+"</b> (Київ).")
    elif row.get("awaiting_receipt"):
        lines.append(manual_receipts.PROMPT)
    elif row["state"] == "review":
        lines.append("🕓 <b>Очікує перевірки власником</b>")
        lines.append("Після підтвердження відкриємо доступ на 30 днів. Повторно не сплачуй.")
        buttons.append([{"text": "🏦 Реквізити", "callback_data": PREFIX+"details:"+code}])
    elif row["state"] == "rejected":
        lines.append("Оплату не підтверджено. Допомога: /paysupport")
    else:
        lines.append("<b>"+LABELS[row["state"]]+"</b>")
        buttons.append([{"text": "💳 Перейти до оплати", "callback_data": PREFIX+"details:"+code}])
        buttons.append([{"text": "✅ Я оплатив", "callback_data": PREFIX+"paid:"+code}])
    if row["owner_note"]:
        lines.append(escape(row["owner_note"]))
    if row["state"] in ("created", "review", "clarification"):
        buttons.append([{"text": "📸 Надіслати скриншот", "callback_data": PREFIX+"receipt:"+code}])
        buttons.append([{"text": "🔄 Перевірити оплату", "callback_data": PREFIX+"status:"+code}])
    buttons.append([{"text": "← Назад", "callback_data": PREFIX+"view"}])
    return response(uid, lines, buttons)


def receipt_help(engine, settings, uid, code, now=None):
    return manual_receipts.await_receipt(engine, settings, uid, code, now)


def details(engine, settings, uid, code, more=False):
    row = m.get_status(engine, settings, uid, code)
    if row["state"] not in ("created", "clarification", "review"):
        return request_card(engine, settings, uid, code)
    data = requisites(engine, settings, uid, code)
    return render_requisites(uid, data["recipient"], code, row["state"], more,
                             awaiting_receipt=row.get("awaiting_receipt", False))


def render_requisites(uid, profile, code=None, state="preview", more=False, *, awaiting_receipt=False):
    """Same bank-card template; owner delivery check needs no payment ledger row."""
    iban, card = profile["iban"], profile.get("card_number")
    grouped = " ".join(iban[i:i+4] for i in range(0, len(iban), 4))
    lines = ["🚘 <b>AutoDeal · Оплата доступу</b>",
             "💎 Тариф: 30 днів\n💳 <b>До сплати: 250 грн</b>",
             "👤 Отримувач: "+escape(profile["recipient_name"]),
             "<b>Рахунок · IBAN</b>\n<code>"+grouped+"</code>"]
    buttons = [[{"text": "Скопіювати IBAN", "copy_text": {"text": iban}}]]
    if card:
        lines.append("💳 <b>Номер картки</b>\n<code>"+" ".join(card[i:i+4] for i in range(0,16,4))+"</code>")
        buttons.append([{"text": "Скопіювати картку", "copy_text": {"text": card}}])
    if code is None:
        lines.append("Перевірка для власника. Переказ робити не потрібно.")
        buttons.append([{"text": "Відкрити підписку", "callback_data": PREFIX+"view"}])
        return response(uid, lines, buttons)
    if awaiting_receipt:
        lines.append("Переказ уже зроблено — повторно не сплачуй.")
        lines.append(manual_receipts.PROMPT)
        buttons.append([{"text": "📸 Надіслати скриншот", "callback_data": PREFIX+"receipt:"+code}])
    elif state == "review":
        lines.append("🕓 Ти вже повідомив про оплату.\nЯкщо переказ зроблено — повторно не сплачуй. Доступ відкриє власник після перевірки.")
        buttons.append([{"text": "🔄 Перевірити оплату", "callback_data": PREFIX+"status:"+code}])
    else:
        lines.append("Після переказу натисни «✅ Я оплатив».\nВласник перевірить оплату й відкриє доступ на 30 днів.")
        buttons.append([{"text": "✅ Я оплатив", "callback_data": PREFIX+"paid:"+code}])
    buttons.append([{"text": "← Назад", "callback_data": PREFIX+"view"}])
    return response(uid, lines, buttons)


def handle(engine, settings, event, request=None, now=None):
    if not settings.manual_payment_review_enabled:
        return None
    cb = event.get("callback_query")
    if cb is not None and not isinstance(cb, dict):
        return None
    msg = cb.get("message", {}) if cb else event.get("message", {})
    if not isinstance(msg, dict):
        return None
    sender = cb.get("from", {}) if cb else msg.get("from", {})
    chat = msg.get("chat", {})
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    uid = sender.get("id")
    if (type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot")
            or chat.get("type") != "private" or chat.get("id") != uid):
        return None
    action = cb.get("data", "") if cb else ""
    text = msg.get("text", "")
    if not isinstance(action, str) or not isinstance(text, str):
        return None
    pieces = text.split()
    command, _, mention = (pieces[0] if pieces else "").partition("@")
    if mention and mention.lower() != telegram_setup.BOT_USERNAME.lower():
        return None
    deep = command == "/start" and pieces[1:] == ["subscribe"]
    old_terms = action == billing.PREFIX+"terms"
    if cb and not action.startswith(PREFIX) and not old_terms:
        return None
    if not cb and command not in ("/subscription", "/terms") and not deep:
        return None
    now = time.time() if now is None else now
    if type(event.get("update_id")) is not int or not 0 <= event["update_id"] < 2**63:
        return {"ok": True}
    if not cb and (type(msg.get("date")) is not int or not 0 <= now-msg["date"] <= 300):
        return {"ok": True}
    try:
        m.enabled(settings)
        with Session(engine) as db:
            ctrl = billing.control(db)
            if not ctrl or ctrl.offer.get("method") != "bank_manual":
                return None
        if cb:
            if not isinstance(cb.get("id"), str):
                return {"ok": True}
            (request or telegram_setup.call)(settings.bot_token, "answerCallbackQuery",
                {"callback_query_id": cb["id"]}, timeout=3)
        part = action.removeprefix(PREFIX)
        verb, _, value = part.partition(":")
        if command == "/terms" or verb == "terms" or old_terms:
            return terms(engine, settings, uid)
        if verb == "full_terms":
            return terms(engine, settings, uid, full=True)
        if verb == "receipt":
            return receipt_help(engine, settings, uid, value, now)
        if verb == "bank":
            # Compatibility for already-delivered buttons; the extra form is gone.
            return details(engine, settings, uid, value)
        if verb == "accept":
            row = create(engine, settings, uid, now, value,
                name=str(sender.get("first_name") or "Клієнт")[:100], username=sender.get("username"))
            return details(engine, settings, uid, row["code"])
        if verb == "details":
            return details(engine, settings, uid, value)
        if verb == "paid":
            return receipt_help(engine, settings, uid, value, now)
        if verb == "status":
            return request_card(engine, settings, uid, value)
        return overview(engine, settings, uid, now)
    except m.ReviewError as exc:
        logging.getLogger(__name__).warning("Manual checkout unavailable reason=%s", exc.code)
        messages = {"manual_sales_closed": "Нові оплати зараз закриті. Якщо вже сплатив, відкрий статус заявки або /paysupport.",
                    "recipient_configuration_unavailable": "Реквізити тимчасово недоступні. Не переказуй кошти; звернись через /paysupport.",
                    "invalid_text": "Не вдалося відкрити оплату. Спробуй ще раз через /subscription.",
                    "terms_changed": "Умови змінилися. Відкрий /terms і переглянь їх ще раз."}
        return billing.message(uid, messages.get(exc.code, "Заявку не знайдено або дія недоступна. /subscription · /paysupport"))
    except SQLAlchemyError:
        return billing.message(uid, "Не вдалося перевірити збереження. Не сплачуй повторно. Онови /subscription або напиши /paysupport.")
