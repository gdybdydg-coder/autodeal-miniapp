"""Explicit operator-gated webhook setup and user-requested delivery check."""
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import SourceProbe

BOT_USERNAME = "auto_deal_finder1_bot"
WEBHOOK_URL = "https://autodeal-api.onrender.com/telegram/webhook"
PROBE_ID = "telegram-webhook-v1"
APP_URL = "https://gdybdydg-coder.github.io/autodeal-miniapp/"


def call(token, method, payload):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            result = response.json()
            return result if isinstance(result, dict) else {"uncertain": True}
    except (httpx.HTTPError, ValueError):
        return {"uncertain": True}


def configure(engine, settings, request=call):
    if not settings.configure_webhook:
        return
    status = "unavailable"
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", settings.webhook_secret):
        status = "invalid_secret_format"
    else:
        me = request(settings.bot_token, "getMe", {})
        if me.get("ok") is True and (me.get("result") or {}).get("username") == BOT_USERNAME:
            info = request(settings.bot_token, "getWebhookInfo", {})
            if info.get("ok") is True and isinstance(info.get("result"), dict):
                current = info["result"].get("url")
                if current in ("", WEBHOOK_URL):
                    response = request(settings.bot_token, "setWebhook", {
                        "url": WEBHOOK_URL, "secret_token": settings.webhook_secret,
                        "allowed_updates": ["message"], "drop_pending_updates": False,
                        "max_connections": 5})
                    if response.get("ok") is True and response.get("result") is True:
                        status = "configured"
                elif current:
                    status = "existing_webhook_conflict"
        elif me.get("ok") is True:
            status = "wrong_bot"
    with Session(engine) as db:
        db.merge(SourceProbe(id=PROBE_ID, status=status, checked_at=time.time(), requests=0, result={}))
        db.commit()


def webhook_status(engine):
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        return {"status": row.status if row else "not_configured"}


def send_test(token, uid):
    return call(token, "sendMessage", {"chat_id": uid,
        "text": "✅ AUTODeal: тестове повідомлення доставлено.\n"
                "Повернись у «Мої пошуки» та ввімкни сповіщення для одного пошуку.\n"
                "Це перевірка зв’язку, не оголошення про авто.\n"
                "/stop — вимкнути всі сповіщення."})


def configure_menu(engine, settings, request=call):
    """Update only the bot's launch button after an operator publishes a release.

    No messages, webhook changes, source requests or delivery flag changes.
    """
    version = settings.miniapp_release
    if not version:
        return
    probe_id = "telegram-menu-" + version
    with Session(engine) as db:
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    status = "unavailable"
    target = APP_URL + "?v=" + version
    try:
        me = request(settings.bot_token, "getMe", {})
        if me.get("ok") is True and (me.get("result") or {}).get("username") == BOT_USERNAME:
            menu = request(settings.bot_token, "getChatMenuButton", {})
            old = menu.get("result") or {}
            if menu.get("ok") is True and old.get("type") in {"default", "commands", "web_app"}:
                old_url = (old.get("web_app") or {}).get("url", "")
                parsed = urlsplit(old_url)
                ours = (parsed.scheme == "https" and parsed.netloc == "gdybdydg-coder.github.io"
                        and parsed.path in {"/autodeal-miniapp/", "/autodeal-miniapp/index.html"})
                if old.get("type") == "web_app" and not ours:
                    status = "existing_menu_conflict"
                else:
                    result = request(settings.bot_token, "setChatMenuButton", {"menu_button": {
                        "type": "web_app", "text": "Відкрити AUTODeal", "web_app": {"url": target}}})
                    if result.get("ok") is True and result.get("result") is True:
                        check = request(settings.bot_token, "getChatMenuButton", {})
                        if check.get("ok") is True and ((check.get("result") or {}).get("web_app") or {}).get("url") == target:
                            status = "configured"
        elif me.get("ok") is True:
            status = "wrong_bot"
    except Exception:
        status = "unavailable"
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        row.status, row.checked_at = status, time.time()
        row.result = {"release": version}
        db.commit()


def menu_status(engine, version):
    with Session(engine) as db:
        row = db.get(SourceProbe, "telegram-menu-" + version) if version else None
        return {"status": row.status if row else "not_configured", "release": version or None}
