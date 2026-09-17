"""Explicit operator-gated webhook setup and user-requested delivery check."""
import logging
import re
import time

import httpx
from sqlalchemy.orm import Session

from .models import SourceProbe

BOT_USERNAME = "auto_deal_finder1_bot"
WEBHOOK_URL = "https://autodeal-api.onrender.com/telegram/webhook"
PROBE_ID = "telegram-webhook-v1"


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
