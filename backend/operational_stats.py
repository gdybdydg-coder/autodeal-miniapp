"""Read-only aggregate observations. No provider calls, repairs or send claims."""
import math
import statistics
import time
from datetime import datetime, timezone

from sqlalchemy import exists, func, select

from . import active_window, poll_schedule
from .models import (Delivery, DeliveryTiming, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorSeen, MonitorWatch, Search, User)

WINDOW = 86400


def distribution(values):
    values.sort()
    return {"samples": len(values),
            "p50_seconds": round(statistics.median(values), 3) if values else None,
            "p95_seconds": round(values[math.ceil(len(values)*.95)-1], 3) if values else None}


def latency(db, uid=None, *, now=None):
    now = time.time() if now is None else now
    query = select(DeliveryTiming.discovered_at, DeliveryTiming.queued_at,
                   DeliveryTiming.accepted_at).join(Delivery, Delivery.id == DeliveryTiming.delivery_id).where(
        Delivery.state == "sent", DeliveryTiming.accepted_at >= now-WINDOW,
        DeliveryTiming.accepted_at <= now)
    if uid is not None:
        query = query.where(Delivery.user_id == uid)
    discovered, queued, accepted = [], [], 0
    # Only three numeric columns; no car payloads, identities or publication-date guesses.
    for found_at, queued_at, end in db.execute(query).yield_per(500):
        accepted += 1
        for start, values in ((found_at, discovered), (queued_at, queued)):
            if start is not None and math.isfinite(start) and math.isfinite(end) and 0 < start <= end:
                values.append(end-start)
    return {"window_seconds": WINDOW, "accepted_messages": accepted,
            "discovery_to_telegram": distribution(discovered),
            "queue_to_telegram": distribution(queued),
            "percentile_method": "median_and_nearest_rank_p95",
            "receipt_basis": "telegram_api_acceptance"}


def queues(db, uid=None, *, now=None):
    now = time.time() if now is None else now
    interest = select(MonitorSeen.search_id).join(Search, Search.id == MonitorSeen.search_id).join(
        User, User.id == Search.user_id).join(MonitorWatch, MonitorWatch.search_id == Search.id).where(
        MonitorSeen.source_id == MonitorJob.source_id, MonitorSeen.state == "pending",
        MonitorSeen.epoch == MonitorWatch.epoch, Search.enabled.is_(True), User.ready.is_(True))
    match = select(MonitorMatch.search_id).join(Search, Search.id == MonitorMatch.search_id).join(
        MonitorWatch, MonitorWatch.search_id == Search.id).where(
        Search.user_id == Delivery.user_id, Search.enabled.is_(True),
        MonitorMatch.listing_id == Delivery.listing_id, MonitorMatch.epoch == MonitorWatch.epoch,
        MonitorMatch.fingerprint == Search.fingerprint)
    if uid is not None:
        interest = interest.where(Search.user_id == uid)
        match = match.where(Search.user_id == uid)
    supplemental = func.coalesce(MonitorJob.result["discovery_kind"].as_string(), "") == active_window.KIND
    valuations = {"new_publications": 0, "supplemental": 0}
    for extra, count in db.execute(select(supplemental, func.count()).select_from(MonitorJob).where(
            MonitorJob.state == "pending", exists(interest)).group_by(supplemental)):
        valuations["supplemental" if extra else "new_publications"] = count
    waiting = select(Delivery.id).join(User, User.id == Delivery.user_id).where(
        Delivery.state == "pending", User.ready.is_(True), exists(match))
    sending = select(func.count()).select_from(Delivery).join(User, User.id == Delivery.user_id).where(
        Delivery.state == "sending", User.ready.is_(True), exists(match))
    pending = db.scalar(select(func.count()).select_from(waiting.subquery())) or 0
    oldest = db.scalar(select(func.min(DeliveryTiming.queued_at)).where(
        DeliveryTiming.delivery_id.in_(waiting), DeliveryTiming.queued_at > 0,
        DeliveryTiming.queued_at <= now))
    return {"valuation": {**valuations, "total": sum(valuations.values()), "unit": "unique_cars"},
            "delivery": {"pending": pending, "sending": db.scalar(sending) or 0,
                         "oldest_wait_seconds": round(now-oldest, 3) if oldest is not None else None,
                         "unit": "messages"}, "scope": "current_ready_subscriptions"}


def duration(value):
    if value is None:
        return "немає даних"
    number, unit = (value, "с") if value < 60 else (value/60, "хв") if value < 3600 else (value/3600, "год")
    return f"{number:.1f}".replace(".", ",") + " " + unit


def timestamp(value):
    if not value or not math.isfinite(value):
        return "ще немає"
    zone = poll_schedule.KYIV or timezone.utc
    return datetime.fromtimestamp(value, zone).strftime("%d.%m %H:%M:%S") + (
        " (Київ)" if poll_schedule.KYIV else " (UTC)")


def text(db, settings=None):
    from . import monitor
    now = time.time()
    members = monitor.active_members(db)
    groups = {member.feed_id for _, _, member in members}
    checked = list(db.scalars(select(MonitorFeed.checked_at).where(MonitorFeed.id.in_(groups),
        MonitorFeed.checked_at > 0, MonitorFeed.checked_at <= now)))
    control = db.get(MonitorControl, "pilot")
    healthy = bool(control and 0 <= now-control.heartbeat < monitor.LEASE+60)
    state = ("вимкнений" if settings is not None and not settings.monitor_enabled else
             "свіжий сигнал роботи" if healthy else "немає свіжого сигналу роботи")
    lines = ["🛠 Робота бота", f"Монітор: {state}", f"Груп пошуку: {len(groups)}",
             f"Останній успішний запит пошуку: {timestamp(max(checked, default=None))}",
             f"Груп без успішної перевірки: {len(groups)-len(checked)}"]
    if checked:
        lines.append(f"Найдавніша перевірка серед груп: {timestamp(min(checked))}")
    if settings is not None:
        interval = monitor.poll_interval(len(groups), provider_pricing_enabled=settings.ria_ai_price_enabled,
            active_window_enabled=settings.ria_active_window_enabled,
            schedule_enabled=settings.ria_poll_schedule_enabled, now=now)
        lines.append(f"Плановий інтервал нових публікацій: {interval} с")
        paused = active_window.night_paused(schedule_enabled=
            settings.ria_ai_price_enabled and settings.ria_poll_schedule_enabled, now=now)
        extra = ("вимкнено" if not settings.ria_active_window_enabled else
                 "нічна пауза до 08:00 (Київ)" if paused else "кожні 300 с")
        lines.append(f"Додаткова перевірка: {extra}")
    q, timing = queues(db, now=now), latency(db, now=now)
    v, d = q["valuation"], q["delivery"]
    lines.extend(["", "⏳ Черги зараз (чинні підписки)",
        f"Оцінювання: {v['total']} авто — нові {v['new_publications']}, додаткові {v['supplemental']}",
        f"Надсилання: {d['pending']} повідомлень чекають, {d['sending']} у процесі"])
    if d["pending"]:
        lines.append(f"Найдовше очікування надсилання: {duration(d['oldest_wait_seconds'])}")
    lines.extend(["", "⏱ Затримки за останні 24 год",
                  f"Прийнято Telegram: {timing['accepted_messages']} повідомлень"])
    for title, key in (("Від виявлення ботом до Telegram", "discovery_to_telegram"),
                       ("Від постановки в чергу до Telegram", "queue_to_telegram")):
        sample = timing[key]
        lines.append(f"{title}: немає коректних вимірів" if not sample["samples"] else
            f"{title}: типово {duration(sample['p50_seconds'])}; "
            f"95% — до {duration(sample['p95_seconds'])} (вимірів: {sample['samples']})")
    if timing["discovery_to_telegram"]["samples"] < 20:
        lines.append("Мало вимірів: оцінка затримки ще нестійка.")
    lines.append("Це час після виявлення, не від публікації; прийняття Telegram не підтверджує push на телефон.")
    return "\n".join(lines)
