import asyncio
import hmac
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import telegram_user
from .auto_ria import probe_once, probe_status
from .auto_ria import RiaError
from .ria_search import RiaSearch, initialize_budget, verify_search_once, quota_status, budget_usage
from .ria_budget import BudgetLimits, peer_scan_limit
from .ria_validation import validate_once, validate_run_id, validation_status
from .models import (Base, Delivery, EnabledRequest, Filters, Listing, MonitorControl,
                     MonitorSeen, MonitorWatch, Search, SearchRequest, TelegramTest, User)
from . import monitor, telegram_setup


@dataclass(frozen=True)
class Settings:
    database_url: str
    bot_token: str
    webhook_secret: str
    delivery_enabled: bool = False
    source_ready: bool = False
    origin: str = "https://gdybdydg-coder.github.io"
    auto_ria_api_key: str = field(default="", repr=False)
    ria_validation_run_id: str = ""
    monitor_enabled: bool = False
    configure_webhook: bool = False
    miniapp_release: str = ""

    @classmethod
    def env(cls):
        return cls(
            database_url=os.environ["DATABASE_URL"],
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            webhook_secret=os.environ["TELEGRAM_WEBHOOK_SECRET"],
            delivery_enabled=os.getenv("DELIVERY_ENABLED") == "true",
            source_ready=os.getenv("SOURCE_READY") == "true",
            auto_ria_api_key=os.getenv("AUTO_RIA_API_KEY", "").strip(),
            ria_validation_run_id=os.getenv("RIA_VALIDATION_RUN_ID", ""),
            monitor_enabled=os.getenv("MONITOR_ENABLED") == "true",
            configure_webhook=os.getenv("TELEGRAM_CONFIGURE_WEBHOOK") == "true",
            miniapp_release=os.getenv("MINIAPP_RELEASE", ""),
        )

    @property
    def live(self):
        return self.delivery_enabled and self.source_ready


def create_app(settings: Settings, engine=None):
    BudgetLimits.env()  # Validate before serving requests or running startup probes.
    peer_scan_limit()
    validate_run_id(settings.ria_validation_run_id)
    if settings.miniapp_release and not re.fullmatch(r"[a-z0-9-]{1,40}", settings.miniapp_release):
        raise ValueError("Invalid Mini App release")
    if not settings.bot_token or len(settings.webhook_secret) < 32:
        raise ValueError("Configure server-only Telegram secrets")
    engine = engine or create_engine(settings.database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def lifespan(app):
        # Initial schema only. Use versioned migrations before altering deployed tables.
        Base.metadata.create_all(engine)
        initialize_budget(engine)
        monitor.initialize(engine)
        await asyncio.to_thread(probe_once, engine, settings.auto_ria_api_key)
        await asyncio.to_thread(verify_search_once, engine, settings.auto_ria_api_key)
        await asyncio.to_thread(validate_once, engine, settings.auto_ria_api_key, settings.ria_validation_run_id)
        await asyncio.to_thread(telegram_setup.configure, engine, settings)
        await asyncio.to_thread(telegram_setup.configure_menu, engine, settings)
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(engine, settings, stop)) if settings.monitor_enabled else None
        try:
            yield
        finally:
            stop.set()
            if task:
                await task

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[settings.origin],
                       allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-Telegram-Init-Data"])

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not reflect request bodies / credentials in error payloads.
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    def session():
        with Session(engine) as db:
            yield db

    def identity(x_telegram_init_data: str = Header(default="")):
        try:
            return telegram_user(x_telegram_init_data, settings.bot_token)
        except (ValueError, KeyError, TypeError, OverflowError):
            raise HTTPException(401, "Reopen Mini App in Telegram") from None

    def user_row(db, uid):
        # Serialize per-user mutations, including /stop, with a row lock in PostgreSQL.
        user = db.scalar(select(User).where(User.id == uid).with_for_update())
        if user is None:
            try:
                with db.begin_nested():
                    user = User(id=uid, ready=False, last_update=-1, last_command_at=0)
                    db.add(user)
                    db.flush()
            except IntegrityError:
                user = db.scalar(select(User).where(User.id == uid).with_for_update())
                if user is None:
                    raise
        return user

    def check_enable(user, db, search_id=None):
        if not settings.live:
            raise HTTPException(503, "Delivery/source not connected")
        if not user.ready:
            raise HTTPException(409, "Send /start to the bot first")
        if settings.monitor_enabled:
            if not settings.auto_ria_api_key or telegram_setup.webhook_status(engine)["status"] != "configured":
                raise HTTPException(503, "Delivery/source not connected")
            # Serialize the global pilot slot across different Telegram users.
            control = db.scalar(select(MonitorControl).where(MonitorControl.id == "pilot").with_for_update())
            if not control or time.time() - control.heartbeat >= monitor.LEASE + 60:
                raise HTTPException(503, "Monitor is offline")
            test = db.get(TelegramTest, user.id)
            if not test or test.state != "sent":
                raise HTTPException(409, "Send a test notification first")
            other = db.scalar(select(Search.id).where(Search.enabled.is_(True), Search.id != (search_id or -1)).limit(1))
            if other:
                raise HTTPException(409, "Pilot allows one active search")

    def latest(db):
        return db.scalar(select(func.max(Listing.id))) or 0

    def view(search, db):
        watch = db.get(MonitorWatch, search.id)
        status = watch.status if watch else "off"
        if watch and status == "watching":
            states = set(db.scalars(select(MonitorSeen.state).where(MonitorSeen.search_id == search.id).distinct()))
            if "expired" in states:
                status = "coverage_limited"
            elif "pending" in states:
                status = "checking"
        return {"id": search.id, "name": search.name, "filters": search.filters,
                "enabled": search.enabled, "delivery_available": settings.live,
                "monitor_status": status,
                "last_checked_at": watch.checked_at if watch else None}

    @app.get("/health")
    def health():
        # Read-only readiness probe. Never expose a connection URL or DB exception.
        try:
            with engine.connect() as connection:
                connection.execute(select(1)).scalar_one()
        except SQLAlchemyError:
            return JSONResponse({"ok": False, "database": "unavailable",
                                 "delivery_available": False}, status_code=503)
        return {"ok": True, "database": "connected",
                "database_type": engine.dialect.name,
                "delivery_available": settings.live}

    @app.get("/api/subscriptions")
    def subscriptions(uid=Depends(identity), db=Depends(session)):
        return [view(row, db) for row in db.scalars(select(Search).where(Search.user_id == uid))]

    @app.get("/api/source-status")
    def source_status():
        # Cached public diagnostic only; refreshing NEVER spends API requests.
        return {**probe_status(engine, bool(settings.auto_ria_api_key)), "quota": quota_status(engine),
                "budget": budget_usage(engine),
                "valuation_check": validation_status(engine, settings.ria_validation_run_id),
                "monitor": monitor.runtime_status(engine, settings.monitor_enabled),
                "telegram": telegram_setup.webhook_status(engine),
                "miniapp_menu": telegram_setup.menu_status(engine, settings.miniapp_release)}

    @app.get("/api/notifications/status")
    def notification_status(uid=Depends(identity), db=Depends(session)):
        user, test = db.get(User, uid), db.get(TelegramTest, uid)
        runtime = monitor.runtime_status(engine, settings.monitor_enabled)
        connected = telegram_setup.webhook_status(engine)["status"] == "configured"
        return {**runtime, "available": settings.live and runtime["running"] and connected,
                "telegram_ready": bool(user and user.ready),
                "test_sent": bool(test and test.state == "sent"),
                "bot_url": "https://t.me/" + telegram_setup.BOT_USERNAME + "?start=notifications"}

    @app.post("/api/notifications/test")
    def notification_test(uid=Depends(identity), db=Depends(session)):
        user = user_row(db, uid)
        if not settings.monitor_enabled or telegram_setup.webhook_status(engine)["status"] != "configured":
            raise HTTPException(503, "Telegram is not connected")
        if not user.ready:
            raise HTTPException(409, "Send /start to the bot first")
        row = db.get(TelegramTest, uid)
        if row and time.time() - row.attempted_at < 600:
            raise HTTPException(429, "Wait ten minutes before another test")
        if row is None:
            row = TelegramTest(user_id=uid)
            db.add(row)
        row.attempted_at, row.state = time.time(), "sending"
        db.commit()  # Persist before network I/O; a timeout must not duplicate a test.
        user = user_row(db, uid)
        if not user.ready:
            row.state = "cancelled"
        else:
            result = telegram_setup.send_test(settings.bot_token, uid)
            row.state = "sent" if result.get("ok") is True and type((result.get("result") or {}).get("message_id")) is int else "uncertain"
        db.commit()
        return {"state": row.state}

    @app.post("/api/cars/search")
    def search_cars(payload: Filters, uid=Depends(identity)):
        try:
            return RiaSearch(engine, settings.auto_ria_api_key).search(payload)
        except RiaError as exc:
            code = str(exc)
            status = 422 if code == "unsupported_filter" else 429 if code in {"quota_exceeded", "busy", "search_limit"} else 503
            return JSONResponse({"detail": code, "quota": quota_status(engine)}, status_code=status)
        except Exception:
            return JSONResponse({"detail": "source_unavailable"}, status_code=503)

    @app.post("/api/subscriptions")
    def save(payload: SearchRequest, uid=Depends(identity), db=Depends(session)):
        user = user_row(db, uid)
        if not payload.name.strip():
            raise HTTPException(422, "Name is required")
        fingerprint = payload.filters.fingerprint()
        row = db.scalar(select(Search).where(Search.user_id == uid, Search.fingerprint == fingerprint))
        if payload.enabled:
            check_enable(user, db, row.id if row else None)
        was_enabled = bool(row and row.enabled)
        if row is None:
            count = db.scalar(select(func.count()).select_from(Search).where(Search.user_id == uid))
            if count >= 20:
                raise HTTPException(409, "Maximum 20 subscriptions")
            row = Search(user_id=uid, fingerprint=fingerprint, after_listing=latest(db))
            db.add(row)
        elif payload.enabled and not row.enabled:
            row.after_listing = latest(db)
        row.name = payload.name.strip()
        row.filters = payload.filters.canonical()
        row.enabled = payload.enabled
        db.flush()
        if row.enabled != was_enabled:
            monitor.reset_watch(db, row.id, row.enabled)
        db.commit()
        return view(row, db)

    @app.patch("/api/subscriptions/{search_id}")
    def enable(search_id: int, payload: EnabledRequest, uid=Depends(identity), db=Depends(session)):
        user = user_row(db, uid)
        row = db.scalar(select(Search).where(Search.id == search_id, Search.user_id == uid))
        if row is None:
            raise HTTPException(404, "Subscription not found")
        if payload.enabled:
            check_enable(user, db, row.id)
            if not row.enabled:
                row.after_listing = latest(db)
        if row.enabled != payload.enabled:
            monitor.reset_watch(db, row.id, payload.enabled)
        row.enabled = payload.enabled
        db.commit()
        return view(row, db)

    @app.delete("/api/subscriptions/{search_id}", status_code=204)
    def delete(search_id: int, uid=Depends(identity), db=Depends(session)):
        user_row(db, uid)
        row = db.scalar(select(Search).where(Search.id == search_id, Search.user_id == uid))
        if row is None:
            raise HTTPException(404, "Subscription not found")
        monitor.reset_watch(db, row.id, False)
        db.delete(row)
        db.commit()

    @app.post("/telegram/webhook")
    async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
        if not hmac.compare_digest(x_telegram_bot_api_secret_token, settings.webhook_secret):
            raise HTTPException(403, "Invalid webhook")
        # Streaming body cap also covers requests without Content-Length.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Update too large")
        import json
        try:
            event = json.loads(raw)
            update_id = event["update_id"]
            message = event.get("message", {})
            chat = message.get("chat", {})
            sender = message.get("from", {})
            uid = sender.get("id")
            text = message.get("text", "")
            command_at = message.get("date")
            if type(update_id) is not int or not isinstance(text, str):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(422, "Invalid update") from None
        if chat.get("type") != "private" or chat.get("id") != uid or type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot"):
            return {"ok": True}
        command = text.split()[0].split("@")[0] if text.split() else ""
        if command not in ("/start", "/stop"):
            return {"ok": True}
        if type(command_at) is not int or command_at <= 0:
            raise HTTPException(422, "Invalid message date")
        with Session(engine) as db:
            user = user_row(db, uid)
            if (command_at, update_id) <= (user.last_command_at, user.last_update):
                return {"ok": True}
            user.last_update = update_id
            user.last_command_at = command_at
            user.ready = command == "/start"
            if command == "/stop":
                for sid in db.scalars(select(Search.id).where(Search.user_id == uid)):
                    monitor.reset_watch(db, sid, False)
                db.execute(update(Search).where(Search.user_id == uid).values(enabled=False))
                db.execute(update(Delivery).where(Delivery.user_id == uid, Delivery.state == "pending").values(state="cancelled"))
            db.commit()
        # /start records consent only; the explicit Mini App test confirms delivery.
        return {"ok": True}

    return app


def factory():
    return create_app(Settings.env())
