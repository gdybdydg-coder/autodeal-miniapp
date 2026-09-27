"""Read-only operational checks and durable, private owner incident notifications.

Runs independently of discovery and car dispatch. No paid probes, repairs, replay,
quota changes or schema migration. Cannot report a complete process/DB/Telegram
outage while that dependency is unavailable.
"""
import asyncio
import copy
import logging
import time

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from . import active_window, monitor, quota_management as qm
from .models import (Delivery, DeliveryTiming, MonitorControl, MonitorFeed, MonitorJob,
                     MonitorMatch, MonitorSeen, MonitorWatch, Search, SourceProbe, User)

STATE = "owner-health-state-v1"
OUTBOX = "owner-health-outbox-"
INTERVAL = 60
SETTLE = 120
STARTUP_GRACE = 180
QUEUE_AGE = 600
LABELS = {
    "source_pause": "Запити AUTO.RIA призупинені",
    "monitor": "Монітор пошуку не оновлює стан",
    "discovery": "Перевірка нових публікацій відстає",
    "valuation_queue": "Нові авто надто довго чекають перевірки",
    "delivery_queue": "Черга повідомлень надто довго чекає",
    "telegram": "Повторюються помилки надсилання Telegram",
}
RECOVERY = {
    "source_pause": "Запити AUTO.RIA знову доступні за локальною перевіркою.",
    "monitor": "Монітор пошуку знову оновлює стан.",
    "discovery": "Перевірка нових публікацій більше не перевищує поріг затримки.",
    "valuation_queue": "У черзі нових авто більше немає очікування понад 10 хвилин.",
    "delivery_queue": "У черзі повідомлень більше немає очікування понад 10 хвилин.",
    "telegram": "Є нові підтвердження прийняття повідомлень Telegram; частота помилок знизилася.",
}
log = logging.getLogger(__name__)


def observations(db, settings, now):
    """None means no evidence to assert failure OR recovery for that component."""
    signals = {key: (None, "") for key in LABELS}
    members = monitor.active_members(db)
    if not members:
        return signals, False
    groups = {member.feed_id for _, _, member in members}
    budget = qm.snapshot(db, now)
    gate = budget["gate"]["reason"] if budget else "unknown"
    # Cumulative exhaustion already has its own durable quota warning.
    signals["source_pause"] = (gate in {"hourly", "daily", "upstream"} or
        (gate == "total" and not settings.ria_quota_management_enabled),
        qm.gate_text(budget) if budget else "Облік запитів недоступний.") if budget else (None, "")
    if gate == "total" and settings.ria_quota_management_enabled:
        signals["source_pause"] = (None, "")
    control = db.get(MonitorControl, "pilot")
    monitor_bad = not settings.monitor_enabled or not control or now-control.heartbeat > 180
    signals["monitor"] = (monitor_bad, "Монітор вимкнений у налаштуваннях сервера." if not settings.monitor_enabled else
        "Активні підписки є, але свіжого сигналу роботи монітора немає понад 3 хвилини.")
    if gate == "available" and not monitor_bad:
        interval = monitor.poll_interval(len(groups), provider_pricing_enabled=settings.ria_ai_price_enabled,
            active_window_enabled=settings.ria_active_window_enabled,
            schedule_enabled=settings.ria_poll_schedule_enabled, now=now)
        threshold = max(600, 4*interval)
        feeds = {row.id: row for row in db.scalars(select(MonitorFeed).where(MonitorFeed.id.in_(groups)))}
        starts = {group: min(m.started_at for _, _, m in members if m.feed_id == group) for group in groups}
        late = sum(now-max(feeds[g].cursor, starts[g]) > threshold if g in feeds
                   else now-starts[g] > threshold for g in groups)
        signals["discovery"] = (bool(late), f"Груп із затримкою понад {round(threshold/60)} хв: {late} із {len(groups)}.")
        interest = exists(select(MonitorSeen.search_id).join(Search, Search.id == MonitorSeen.search_id)
            .join(User, User.id == Search.user_id).join(MonitorWatch, MonitorWatch.search_id == Search.id)
            .where(MonitorSeen.source_id == MonitorJob.source_id, MonitorSeen.state == "pending",
                   MonitorSeen.epoch == MonitorWatch.epoch, Search.enabled.is_(True), User.ready.is_(True)))
        # Supplemental backlog deliberately yields to new publications. It must
        # not be misreported as a stalled queue of new arrivals.
        pending = db.scalar(select(func.count()).select_from(MonitorJob).where(
            MonitorJob.state == "pending", MonitorJob.first_seen < now-QUEUE_AGE,
            func.coalesce(MonitorJob.result["discovery_kind"].as_string(), "") != active_window.KIND, interest))
        signals["valuation_queue"] = (pending > 0, f"Нових авто з очікуванням понад 10 хв: {pending}.")
    current_match = exists(select(MonitorMatch.search_id).join(Search, Search.id == MonitorMatch.search_id)
        .join(MonitorWatch, MonitorWatch.search_id == Search.id)
        .where(Search.user_id == Delivery.user_id, Search.enabled.is_(True),
               MonitorMatch.listing_id == Delivery.listing_id, MonitorMatch.epoch == MonitorWatch.epoch,
               MonitorMatch.fingerprint == Search.fingerprint))
    delayed = db.scalar(select(func.count()).select_from(Delivery).join(DeliveryTiming,
        DeliveryTiming.delivery_id == Delivery.id).join(User, User.id == Delivery.user_id)
        .where(Delivery.state == "pending", User.ready.is_(True),
               DeliveryTiming.queued_at < now-QUEUE_AGE, current_match))
    signals["delivery_queue"] = (delayed > 0, f"Повідомлень з очікуванням понад 10 хв: {delayed}.")
    outcomes = list(db.execute(select(Delivery.state, Delivery.user_id).join(DeliveryTiming,
        DeliveryTiming.delivery_id == Delivery.id).join(User, User.id == Delivery.user_id)
        .where(DeliveryTiming.send_started_at >= now-600, User.ready.is_(True),
               Delivery.state.in_(["sent", "failed", "uncertain"]))))
    bad = [uid for state, uid in outcomes if state != "sent"]
    good = sum(state == "sent" for state, _ in outcomes)
    if len(bad) >= 3 and len(set(bad)) >= 2 and len(bad) >= good:
        signals["telegram"] = (True, f"Невдалих або непідтверджених надсилань за 10 хв: {len(bad)}; прийнятих: {good}.")
    elif good >= 3:
        signals["telegram"] = (False, "")
    return signals, True


def event_id(kind, episode, recovered=False):
    return OUTBOX+f"{kind}-{episode}-{'recovery' if recovered else 'alert'}"


def enqueue(db, settings, kind, item, detail, now, recovered=False):
    ident = event_id(kind, item["episode"], recovered)
    text = ("✅ AUTODeal — стан відновився\n\n"+RECOVERY[kind] if recovered else
        "⚠️ AUTODeal — потрібна увага\n\n"+LABELS[kind]+".\n"+detail+
        "\nПроблема підтверджена кількома перевірками. Підписки та захист від повторів збережено.")
    if kind == "source_pause" and not recovered:
        text += "\nПоточний бюджет: /quota."
    db.add(SourceProbe(id=ident, status="pending", checked_at=now, requests=0,
        result={"uid": settings.admin_telegram_id, "text": text, "created_at": now,
                "kind": kind, "episode": item["episode"], "recovered": recovered}))


def check(engine, settings):
    if not (settings.owner_alerts_enabled and settings.live and settings.admin_telegram_id):
        return
    now = time.time()
    with Session(engine) as db:
        # Same owner lock as /stop and quota operations; no network inside check.
        owner = db.get(User, settings.admin_telegram_id, with_for_update=True)
        if not owner:
            return
        row = db.get(SourceProbe, STATE)
        if row and now-row.checked_at < INTERVAL:
            return
        old = copy.deepcopy(row.result) if row else {}
        signals, active = observations(db, settings, now) if owner.ready else ({}, False)
        gap = not row or now-row.checked_at > INTERVAL*3
        items = old.get("items", {})
        for kind in LABELS:
            item = items.setdefault(kind, {"active": False, "episode": 0})
            bad, detail = signals.get(kind, (None, ""))
            if not active:
                # Stopping subscriptions is not a recovery. Preserve episode
                # numbers to avoid reusing any old send claim after reactivation.
                item.update(active=False, since=None, target=None)
                continue
            if bad is None:
                item.update(since=None, target=None)
                continue
            if gap or item.get("target") is not bad:
                item.update(target=bad, since=now)
            if bad == item["active"] or now-item["since"] < SETTLE:
                continue
            item["active"] = bad
            if bad:
                item["episode"] += 1
                enqueue(db, settings, kind, item, detail, now)
            else:
                alert = db.get(SourceProbe, event_id(kind, item["episode"]))
                # No unprompted 'recovery' if the warning was never accepted.
                if alert and alert.status == "sent":
                    enqueue(db, settings, kind, item, "", now, recovered=True)
            log.info("Owner health incident kind=%s active=%s", kind, bad)
        db.merge(SourceProbe(id=STATE, status="checked", checked_at=now, requests=0,
            result={"items": items, "monitoring": active and owner.ready}))
        db.commit()


def valid_event(db, settings, data, now):
    state = db.get(SourceProbe, STATE)
    kind = data.get("kind")
    item = (state.result.get("items", {}).get(kind) or {}) if state else {}
    recovered = data.get("recovered") is True
    if (kind not in LABELS or not item or item.get("episode") != data.get("episode")
            or item.get("active") is recovered):
        return False
    signals, active = observations(db, settings, now)
    return active and signals[kind][0] is (not recovered)


def deliver_one(engine, settings, request=None):
    if not (settings.owner_alerts_enabled and settings.live and settings.admin_telegram_id):
        return "disabled"
    return qm.deliver_reply(engine, settings, request, prefix=OUTBOX,
                            validate=valid_event, require_ready=True)


def public_status(engine, settings):
    with Session(engine) as db:
        row = db.get(SourceProbe, STATE)
        return {"enabled": settings.owner_alerts_enabled, "owner_configured": bool(settings.admin_telegram_id),
                "checked_at": row.checked_at if row else None,
                "monitoring": bool(row and row.result.get("monitoring")),
                "active_incidents": [key for key, item in row.result.get("items", {}).items()
                    if key in LABELS and item.get("active")] if row else [],
                "external_uptime_monitor": False}


async def run(engine, settings, stop):
    next_check = time.monotonic()+STARTUP_GRACE
    while not stop.is_set():
        try:
            if time.monotonic() >= next_check:
                await asyncio.to_thread(check, engine, settings)
                next_check = time.monotonic()+INTERVAL
            await asyncio.to_thread(deliver_one, engine, settings)
        except Exception:
            log.error("Owner health check unavailable")  # no secrets/raw exceptions
            next_check = time.monotonic()+INTERVAL
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
