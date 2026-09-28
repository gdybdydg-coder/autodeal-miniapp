"""Owner-requested listing explanations using saved evidence only. No source calls."""
import re
import time
from urllib.parse import urlsplit

from sqlalchemy import select

from . import launch, quota_management as qm
from .models import Delivery, DeliveryTiming, Listing, MonitorJob, MonitorSeen, Search, SourceProbe
from .operational_stats import timestamp
from .valuation import number

OUTBOX = "listing-check-outbox-"
HELP = ("🔎 Перевірка оголошення\n\nНадішли /check ID або /check посилання AUTO.RIA.\n"
        "Наприклад: /check 40345395\n\nПеревірка читає лише збережену історію твоїх підписок. "
        "Запити AUTO.RIA не витрачаються; повторне надсилання авто не запускається.")
STAGES = {
    "inactive_subscription": "Запис належить зупиненому або попередньому запуску пошуку.",
    "checking": "Авто очікує перевірки або зараз оцінюється.",
    "listing_unavailable": "Під час перевірки AUTO.RIA повідомила, що оголошення недоступне.",
    "cancelled": "Обробку цього запису скасовано.",
    "matched": "За збереженою перевіркою авто підійшло до пошуку.",
    "source_exclusion": "У збережених даних немає підтвердження відповідності правилам джерела.",
    "invalid_price": "У збережених даних немає коректної додатної ціни.",
    "unresolved_filter": "Для цього пошуку немає збереженого зіставлення фільтрів.",
    "filter_mismatch": "Збережені характеристики не відповідають фільтрам цього пошуку.",
    "market_unconfirmed": "Придатну ринкову оцінку не підтверджено — підтвердженої вигоди немає.",
    "below_min_discount": "Вигода за збереженою оцінкою нижча від порога цього пошуку.",
    "checked_without_match": "Перевірка записана, але відповідність цьому пошуку не підтверджена.",
    "checked_without_evidence": "Недостатньо збережених даних для точного пояснення.",
}
REASONS = {
    "filter_brand": "Марка не відповідає фільтру.",
    "filter_model": "Модель не відповідає фільтру.",
    "filter_region": "Область не входить до вибраних у пошуку.",
    "filter_body": "Вказаний тип кузова не відповідає фільтру.",
    "filter_fuel": "Вказане паливо не відповідає фільтру.",
    "filter_gear": "Вказана коробка передач не відповідає фільтру.",
    "filter_price": "Ціна поза заданим бюджетом.",
    "filter_year": "Рік випуску поза заданим діапазоном.",
    "filter_mileage": "Пробіг поза заданим діапазоном.",
    "quota_exceeded": "Очікування через ліміт запитів або паузу AUTO.RIA.",
    "busy": "Очікування вільного слота обробки.",
    "search_limit": "Перевірку відкладено через обмеження одного циклу.",
    "connection_error": "Остання спроба мала помилку з’єднання.",
    "upstream_error": "Остання спроба мала помилку відповіді AUTO.RIA.",
    "listing_unavailable": "Остання спроба отримала відповідь про недоступність оголошення.",
    "reserved_for_new_publications": "Додаткова перевірка очікує бюджету після нових публікацій.",
    "invalid_response": "Остання відповідь AUTO.RIA не пройшла перевірку формату.",
    "unsupported_filter": "Не вдалося зіставити один із фільтрів із довідником AUTO.RIA.",
    "abroad": "Авто позначене як таке, що перебуває за кордоном.",
    "custom": "Авто позначене як таке, що потребує розмитнення.",
}
DELIVERY = {
    "sent": "✅ Telegram підтвердив прийняття повідомлення. Це не підтверджує push на телефон.",
    "pending": "⏳ Повідомлення очікує в черзі надсилання; перед відправкою діють повторні перевірки.",
    "sending": "📤 Відправлення розпочато, остаточного результату ще немає.",
    "uncertain": "❔ Результат відправлення невідомий. Автоматично не повторюємо, щоб уникнути дубля.",
    "failed": "❌ Telegram відхилив спробу надсилання.",
    "cancelled": "⏸ Надсилання скасовано. Точну причину потрібно звіряти з історією підписки.",
}


def parse_id(value):
    if not isinstance(value, str) or len(value) > 1024:
        return None
    value = value.strip()
    if re.fullmatch(r"[1-9][0-9]{0,11}", value):
        return value
    if value.startswith(("auto.ria.com/", "www.auto.ria.com/")):
        value = "https://"+value
    if any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in {"https", "http"} or url.hostname not in {"auto.ria.com", "www.auto.ria.com"}
                or url.username or url.password or url.port not in {None, 80, 443}):
            return None
    except ValueError:
        return None
    found = re.fullmatch(r"/(?:uk/|ru/)?auto_[^/]+_([1-9][0-9]{0,11})\.html", url.path)
    return found.group(1) if found else None


def explain(db, uid, source_id):
    trace = launch.listing_trace(db, uid, source_id)
    lines = [f"🔎 AUTODeal — оголошення {source_id}",
             "Історія твоїх підписок; дані могли змінитися після перевірки.", ""]
    if trace["state"] == "not_observed":
        lines.extend(["У твоїх підписках немає збереженого запису про виявлення цього авто.",
            "Це не доводить, що оголошення не існує або не підходить. Без запису точну причину пропуску встановити не можна."])
    else:
        job = db.get(MonitorJob, source_id)
        evaluated_at = job.result.get("evaluated_at") if job and isinstance(job.result, dict) else None
        if number(evaluated_at, positive=True):
            lines.extend([f"Збережена оцінка/перевірка: {timestamp(evaluated_at)}", ""])
        ids = [entry["search_id"] for entry in trace["subscriptions"][:8]]
        searches = {row.id: row for row in db.scalars(select(Search).where(Search.id.in_(ids), Search.user_id == uid))}
        seen = {row.search_id: row for row in db.scalars(select(MonitorSeen).where(
            MonitorSeen.search_id.in_(ids), MonitorSeen.source_id == source_id))}
        for entry in trace["subscriptions"][:8]:
            sid = entry["search_id"]
            search = searches.get(sid)
            name = " ".join(search.name.split())[:60] if search else f"#{sid}"
            lines.append(f"🔍 {name} (#{sid})")
            if sid in seen:
                lines.append(f"Виявлено: {timestamp(seen[sid].first_seen)}")
            lines.append(REASONS.get(entry["reason"]) if entry["state"] == "source_exclusion" and entry["reason"] in REASONS
                         else STAGES.get(entry["state"], STAGES["checked_without_evidence"]))
            if entry["state"] == "below_min_discount" and search:
                threshold = search.filters.get("minDiscount", 15)
                if type(threshold) in (int, float):
                    lines.append(f"Поріг пошуку: {threshold:g}%.")
            if entry["state"] == "checking" and entry["reason"] in REASONS:
                lines.append(REASONS[entry["reason"]])
            if entry["state"] == "filter_mismatch" and entry["reason"] in REASONS:
                lines.append(REASONS[entry["reason"]])
            lines.append("")
        if len(trace["subscriptions"]) > 8:
            lines.append(f"Ще пошуків із записами: {len(trace['subscriptions'])-8}.")
    if trace["state"] == "observed" or trace.get("delivery_state"):
        lines.append(DELIVERY.get(trace.get("delivery_state"), "Запису про надсилання у твій чат немає."))
    if trace.get("telegram_accepted"):
        accepted = db.scalar(select(DeliveryTiming.accepted_at).join(Delivery, Delivery.id == DeliveryTiming.delivery_id)
            .join(Listing, Listing.id == Delivery.listing_id).where(Delivery.user_id == uid,
                Listing.source == "auto_ria", Listing.source_id == source_id, Delivery.state == "sent"))
        if accepted:
            lines.append(f"Прийнято Telegram: {timestamp(accepted)}")
    lines.extend(["", "Перевірка без запитів AUTO.RIA та повторного надсилання авто."])
    return "\n".join(lines)


def handle(db, settings, uid, text, update_id, command_at):
    if not settings.admin_telegram_id or uid != settings.admin_telegram_id:
        return
    ident = OUTBOX+str(update_id)
    if db.get(SourceProbe, ident) is not None:
        return
    now = time.time()
    if not -30 <= now-command_at <= qm.TTL:
        message = "Команда застаріла. Надішли /check ще раз із ID або посиланням."
    else:
        parts = text.split(maxsplit=1)
        source_id = parse_id(parts[1]) if len(parts) == 2 else None
        message = explain(db, uid, source_id) if source_id else HELP
    db.add(SourceProbe(id=ident, status="pending", checked_at=now, requests=0,
                       result={"uid": uid, "text": message, "created_at": now}))


def deliver_one(engine, settings, request=None):
    if not settings.admin_telegram_id:
        return "disabled"
    return qm.deliver_reply(engine, settings, request, prefix=OUTBOX)
