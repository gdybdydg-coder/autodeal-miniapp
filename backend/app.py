import asyncio
import hmac
import os
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
from .ria_search import RiaSearch, initialize_budget, verify_search_once
from .models import Base, Delivery, EnabledRequest, Filters, Listing, Search, SearchRequest, User


@dataclass(frozen=True)
class Settings:
    database_url: str
    bot_token: str
    webhook_secret: str
    delivery_enabled: bool = False
    source_ready: bool = False
    origin: str = "https://gdybdydg-coder.github.io"
    auto_ria_api_key: str = field(default="", repr=False)

    @classmethod
    def env(cls):
        return cls(
            database_url=os.environ["DATABASE_URL"],
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            webhook_secret=os.environ["TELEGRAM_WEBHOOK_SECRET"],
            delivery_enabled=os.getenv("DELIVERY_ENABLED") == "true",
            source_ready=os.getenv("SOURCE_READY") == "true",
            auto_ria_api_key=os.getenv("AUTO_RIA_API_KEY", "").strip(),
        )

    @property
    def live(self):
        return self.delivery_enabled and self.source_ready


def create_app(settings: Settings, engine=None):
    if not settings.bot_token or len(settings.webhook_secret) < 32:
        raise ValueError("Configure server-only Telegram secrets")
    engine = engine or create_engine(settings.database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def lifespan(app):
        # Initial schema only. Use versioned migrations before altering deployed tables.
        Base.metadata.create_all(engine)
        initialize_budget(engine)
        await asyncio.to_thread(probe_once, engine, settings.auto_ria_api_key)
        await asyncio.to_thread(verify_search_once, engine, settings.auto_ria_api_key)
        yield

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

    def check_enable(user):
        if not settings.live:
            raise HTTPException(503, "Delivery/source not connected")
        if not user.ready:
            raise HTTPException(409, "Send /start to the bot first")

    def latest(db):
        return db.scalar(select(func.max(Listing.id))) or 0

    def view(search):
        return {"id": search.id, "name": search.name, "filters": search.filters,
                "enabled": search.enabled, "delivery_available": settings.live}

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
        return [view(row) for row in db.scalars(select(Search).where(Search.user_id == uid))]

    @app.get("/api/source-status")
    def source_status():
        # Cached public diagnostic only; refreshing NEVER spends API requests.
        return probe_status(engine, bool(settings.auto_ria_api_key))

    @app.post("/api/cars/search")
    def search_cars(payload: Filters, uid=Depends(identity)):
        try:
            return RiaSearch(engine, settings.auto_ria_api_key).search(payload)
        except RiaError as exc:
            code = str(exc)
            status = 422 if code == "unsupported_filter" else 429 if code in {"quota_exceeded", "busy", "search_limit"} else 503
            return JSONResponse({"detail": code}, status_code=status)
        except Exception:
            return JSONResponse({"detail": "source_unavailable"}, status_code=503)

    @app.post("/api/subscriptions")
    def save(payload: SearchRequest, uid=Depends(identity), db=Depends(session)):
        user = user_row(db, uid)
        if not payload.name.strip():
            raise HTTPException(422, "Name is required")
        if payload.enabled:
            check_enable(user)
        fingerprint = payload.filters.fingerprint()
        row = db.scalar(select(Search).where(Search.user_id == uid, Search.fingerprint == fingerprint))
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
        db.commit()
        return view(row)

    @app.patch("/api/subscriptions/{search_id}")
    def enable(search_id: int, payload: EnabledRequest, uid=Depends(identity), db=Depends(session)):
        user = user_row(db, uid)
        row = db.scalar(select(Search).where(Search.id == search_id, Search.user_id == uid))
        if row is None:
            raise HTTPException(404, "Subscription not found")
        if payload.enabled:
            check_enable(user)
            if not row.enabled:
                row.after_listing = latest(db)
        row.enabled = payload.enabled
        db.commit()
        return view(row)

    @app.delete("/api/subscriptions/{search_id}", status_code=204)
    def delete(search_id: int, uid=Depends(identity), db=Depends(session)):
        user_row(db, uid)
        row = db.scalar(select(Search).where(Search.id == search_id, Search.user_id == uid))
        if row is None:
            raise HTTPException(404, "Subscription not found")
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
                db.execute(update(Search).where(Search.user_id == uid).values(enabled=False))
                db.execute(update(Delivery).where(Delivery.user_id == uid, Delivery.state == "pending").values(state="cancelled"))
            db.commit()
        # No outbound messages or webhook registration as an API startup side effect.
        return {"ok": True}

    return app


def factory():
    return create_app(Settings.env())
