"""Owner-issued access, separate from payments and money statistics.

Preview is read-only. Confirmation shares the existing billing lock and writes
one entitlement plus an immutable gift AccessEvent atomically. Signed preview
tokens are also durable retry identities; no extra payment or preview table is
required and granting access never starts a user's paused radars.
"""
import base64
import hashlib
import hmac
import json
import math
import re
import time
import unicodedata
import uuid

from fastapi import Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, radar_monitor
from .billing_models import AccessEvent, Entitlement
from .models import User

DAY, TOKEN_TTL, PREFIX = 86400, 300, "admin-gift:"
DOMAIN = b"autodeal/subscription-grant/v1\0"


class GrantError(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def owner(settings, actor):
    if not billing.verified_admin(settings):
        raise GrantError(503, "admin_configuration_unavailable", "Налаштування адміністратора тимчасово недоступні.")
    if type(actor) is not int or actor != settings.admin_telegram_id:
        raise GrantError(403, "admin_only", "Надавати підписки може лише адміністратор.")


def target_id(value):
    if type(value) is not int or not 0 < value < 2**52:
        raise GrantError(422, "invalid_telegram_id", "Вкажіть коректний числовий Telegram ID користувача.")
    return value


def duration(value):
    if type(value) is not int or not 1 <= value <= 3650:
        raise GrantError(422, "invalid_days", "Кількість днів має бути цілим числом від 1 до 3650.")
    return value


def reason_text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 500 or not value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise GrantError(422, "invalid_reason", "Вкажіть причину надання підписки: від 1 до 500 символів.")
    return value.strip()


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise GrantError(422, "invalid_time", "Не вдалося перевірити час операції. Спробуйте ще раз.")
    return float(value)


def signature(settings, value):
    secret = getattr(settings, "webhook_secret", "")
    if not isinstance(secret, str) or len(secret) < 32:
        raise GrantError(503, "admin_configuration_unavailable", "Налаштування адміністратора тимчасово недоступні.")
    return hmac.new(secret.encode(), DOMAIN + value.encode("ascii"), hashlib.sha256).hexdigest()


def encode(settings, payload):
    value = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")).encode()).decode().rstrip("=")
    return value + "." + signature(settings, value)


def decode(settings, actor, token):
    invalid = GrantError(422, "invalid_confirmation", "Підтвердження недійсне. Перевірте дані ще раз.")
    if (not isinstance(token, str) or len(token) > 4096
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-f0-9]{64}", token)):
        raise invalid
    value, received = token.split(".")
    if not hmac.compare_digest(received, signature(settings, value)):
        raise invalid
    try:
        payload = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if (not isinstance(payload, dict) or set(payload) != {"version", "action_id", "actor", "telegram_id",
                "days", "reason", "before_expiry", "issued_at", "valid_until"}
                or payload["version"] != 1 or type(payload["actor"]) is not int or payload["actor"] != actor
                or not isinstance(payload["action_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", payload["action_id"])):
            raise invalid
        target_id(payload["telegram_id"])
        duration(payload["days"])
        if reason_text(payload["reason"]) != payload["reason"]:
            raise invalid
        before = payload["before_expiry"]
        if type(before) not in (int, float) or not math.isfinite(before) or before < 0:
            raise invalid
        timestamp(payload["issued_at"])
        timestamp(payload["valid_until"])
        if payload["valid_until"] != payload["issued_at"] + TOKEN_TTL:
            raise invalid
    except (ValueError, TypeError, KeyError, OverflowError, GrantError):
        raise invalid from None
    return payload


def known_user(db, uid, *, lock=False):
    query = select(User).where(User.id == uid)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise GrantError(404, "user_not_found", "Користувача не знайдено. Спочатку він має відкрити бота й надіслати /start.")
    return row


def preview(engine, settings, actor, telegram_id, days, reason, now=None):
    owner(settings, actor)
    target_id(telegram_id)
    duration(days)
    reason = reason_text(reason)
    now = timestamp(time.time() if now is None else now)
    with Session(engine) as db:
        user = known_user(db, telegram_id)
        before = billing.expiry(db, telegram_id)
        payload = {"version": 1, "action_id": uuid.uuid4().hex, "actor": actor,
            "telegram_id": telegram_id, "days": days, "reason": reason,
            "before_expiry": before, "issued_at": now, "valid_until": now + TOKEN_TTL}
        return {"confirmation_token": encode(settings, payload), "action_id": payload["action_id"],
            "telegram_id": telegram_id, "days": days, "reason": reason,
            "current_expires_at": before, "proposed_expires_at": max(now, before) + days * DAY,
            "valid_until": payload["valid_until"], "user_ready": user.ready,
            "radar_resume_required": True}


def result(event, payload, replayed):
    return {"action_id": payload["action_id"], "telegram_id": event.user_id,
            "current_expires_at": payload["before_expiry"], "expires_at": event.expires_at,
            "replayed": replayed, "radar_resume_required": True}


def confirm(engine, settings, actor, confirmation_token, now=None):
    owner(settings, actor)
    payload = decode(settings, actor, confirmation_token)
    clock = time.time if now is None else lambda: now
    timestamp(clock())
    event_id = PREFIX + payload["action_id"]
    with Session(engine) as db, db.begin():
        # Same global lock order as a paid confirmation: billing, then user.
        # The UPDATE in control(lock=True) also serializes SQLite fixtures.
        if billing.control(db, lock=True) is None:
            raise GrantError(503, "access_control_unavailable", "Облік підписок тимчасово недоступний. Спробуйте пізніше.")
        known_user(db, payload["telegram_id"], lock=True)
        previous = db.get(AccessEvent, event_id)
        if previous is not None:
            expected = max(previous.at, payload["before_expiry"]) + payload["days"] * DAY
            if (previous.kind != "gift" or previous.actor != actor or previous.user_id != payload["telegram_id"]
                    or previous.reason != payload["reason"] or previous.expires_at != expected
                    or not payload["issued_at"] <= previous.at < payload["valid_until"]):
                raise GrantError(409, "action_conflict", "Це підтвердження вже використано для іншої операції.")
            # A committed operation remains safely recoverable after token TTL.
            # Never recalculate its expiry or create another gift on retry.
            return result(previous, payload, True)
        checked = timestamp(clock())
        if not payload["issued_at"] <= checked < payload["valid_until"]:
            raise GrantError(409, "confirmation_expired", "Час підтвердження минув. Перевірте дані ще раз.")
        before = billing.expiry(db, payload["telegram_id"])
        if before != payload["before_expiry"]:
            raise GrantError(409, "access_changed", "Строк підписки вже змінився. Оновіть дані та перевірте новий строк.")
        # Persist an elapsed Radar ceiling before renewing access. A late gift
        # must not revive an active row which the next worker has not paused yet.
        radar_monitor.sync_expired(db, uid=payload["telegram_id"], now=checked)
        until = max(checked, before) + payload["days"] * DAY
        db.merge(Entitlement(user_id=payload["telegram_id"], expires_at=until, updated_at=checked))
        db.flush()
        # Only a still-active uninterrupted ceiling can extend; paused remains
        # paused and ready/configuration/slot limits are never changed here.
        radar_monitor.sync_expired(db, uid=payload["telegram_id"], now=checked)
        event = AccessEvent(id=event_id, user_id=payload["telegram_id"], actor=actor,
                           kind="gift", at=checked, expires_at=until, reason=payload["reason"])
        db.add(event)
        db.flush()
        return result(event, payload, False)


def history(engine, settings, actor, telegram_id=None, limit=20):
    owner(settings, actor)
    if telegram_id is not None:
        target_id(telegram_id)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise GrantError(422, "invalid_limit", "Можна переглянути від 1 до 50 останніх записів.")
    with Session(engine) as db:
        query = select(AccessEvent).where(AccessEvent.kind == "gift")
        if telegram_id is not None:
            query = query.where(AccessEvent.user_id == telegram_id)
        rows = db.scalars(query.order_by(AccessEvent.at.desc(), AccessEvent.id.desc()).limit(limit))
        return {"items": [{"action_id": row.id.removeprefix(PREFIX), "telegram_id": row.user_id,
            "actor": row.actor, "at": row.at, "expires_at": row.expires_at, "reason": row.reason}
            for row in rows], "limit": limit}


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_id: StrictInt = Field(gt=0, lt=2**52)
    days: StrictInt = Field(ge=1, le=3650)
    reason: str = Field(min_length=1, max_length=500)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(min_length=1, max_length=4096)


def install(app, engine, settings, identity):
    @app.exception_handler(GrantError)
    async def grant_error(request, exc):
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SQLAlchemyError:
            raise GrantError(503, "grant_storage_unavailable",
                "Не вдалося перевірити результат. Повторіть те саме підтвердження; нову операцію не створюйте.") from None

    def admin(uid=Depends(identity)):
        owner(settings, uid)
        return uid

    @app.post("/api/admin/subscription-grants/preview")
    def preview_grant(body: PreviewBody, actor=Depends(admin)):
        return call(preview, engine, settings, actor, body.telegram_id, body.days, body.reason)

    @app.post("/api/admin/subscription-grants/confirm")
    def confirm_grant(body: ConfirmBody, actor=Depends(admin)):
        return call(confirm, engine, settings, actor, body.confirmation_token)

    @app.get("/api/admin/subscription-grants")
    def grant_history(telegram_id: int | None = Query(default=None, gt=0, lt=2**52),
                      limit: int = Query(default=20, ge=1, le=50), actor=Depends(admin)):
        return call(history, engine, settings, actor, telegram_id, limit)
