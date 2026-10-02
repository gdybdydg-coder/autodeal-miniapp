"""Authenticated review API and owner command; public bank sales remain blocked."""
from html import escape
import time
from urllib.parse import parse_qsl
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.exc import SQLAlchemyError

from . import billing, manual_payments as m, telegram_setup


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paid(Body):
    reported_amount_minor: StrictInt | None = None
    transfer_note: str | None = Field(default=None, max_length=500)


class Action(Body):
    code: str = Field(max_length=20)
    state: str
    revision: StrictInt
    note: str = Field(min_length=1, max_length=500)


class Preview(Body):
    code: str = Field(max_length=20)
    revision: StrictInt
    account: str = Field(min_length=3, max_length=120)
    operation: str = Field(min_length=3, max_length=120)
    actual_amount_minor: StrictInt
    bank_verified: StrictBool


class Confirm(Body):
    confirmation: str = Field(min_length=1, max_length=100)


class Retry(Body):
    notice_id: str = Field(min_length=32, max_length=32)


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except m.ReviewError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except SQLAlchemyError:
        # Never echo DB statements/parameters, customer evidence or account data.
        raise HTTPException(503, "payment_storage_unavailable_do_not_pay_again") from None


def install(app, engine, settings, identity):
    router = APIRouter(prefix="/api/manual-payments")

    def available(uid=Depends(identity)):
        call(m.enabled, settings)
        return uid

    def admin(uid=Depends(available)):
        call(m.owner, settings, uid)
        return uid

    @router.post("", status_code=201)
    def create(body: Body, uid=Depends(available), x_telegram_init_data: str = Header(default="")):
        if not m.public_creation_allowed():
            raise HTTPException(409, "public_bank_sales_blocked_by_platform_policy")
        # identity already verified HMAC, age, duplicate fields and sender type.
        signed_user = json.loads(dict(parse_qsl(x_telegram_init_data))["user"])
        name = str(signed_user.get("first_name") or "Клієнт")[:100]
        username = signed_user.get("username")
        return call(m.create_request, engine, settings, uid, time.time(), name=name, username=username)

    @router.get("/admin")
    def queue(state: str = "review", search: str = "", page: int = 1, size: int = 20, uid=Depends(admin)):
        return call(m.queue, engine, settings, uid, state=state, search=search, page=page, size=size)

    @router.get("/admin/{code}")
    def card(code: str, uid=Depends(admin)):
        return call(m.card, engine, settings, uid, code)

    @router.get("/admin/{code}/notices")
    def notices(code: str, uid=Depends(admin)):
        return call(m.notices, engine, settings, uid, code)

    @router.post("/admin/retry-notice")
    def retry(body: Retry, uid=Depends(admin)):
        call(m.retry_notice, engine, settings, uid, body.notice_id)
        return {"queued": True}

    @router.post("/admin/state")
    def change(body: Action, uid=Depends(admin)):
        return call(m.change_state, engine, settings, uid, body.code, body.state,
                    body.note, body.revision, time.time())

    @router.post("/admin/preview")
    def preview(body: Preview, uid=Depends(admin)):
        data = body.model_dump()
        code, revision = data.pop("code"), data.pop("revision")
        return call(m.preview, engine, settings, uid, code, revision, time.time(), **data)

    @router.post("/admin/confirm")
    def confirm(body: Confirm, uid=Depends(admin)):
        return call(m.confirm, engine, settings, uid, body.confirmation, time.time())

    @router.get("/{code}")
    def status(code: str, uid=Depends(available)):
        return call(m.get_status, engine, settings, uid, code)

    @router.post("/{code}/paid")
    def paid(code: str, body: Paid, uid=Depends(available)):
        return call(m.report_paid, engine, settings, uid, code, time.time(), **body.model_dump())

    app.include_router(router)


def handle(engine, settings, event):
    if not settings.manual_payment_review_enabled:
        return None
    message = event.get("message") or {}
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    content = message.get("text") or message.get("caption") or ""
    if not isinstance(content, str):
        return None
    parts = content.split()
    command, _, mention = (parts[0] if parts else "").partition("@")
    receipt_view = command == "/start" and len(parts) == 2 and parts[1].startswith("paymentreceipt_")
    if command not in ("/payments", "/payment_receipt") and not receipt_view:
        return None
    if mention and mention.lower() != telegram_setup.BOT_USERNAME.lower():
        return {"ok": True}
    uid = sender.get("id")
    if (type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot")
            or chat.get("type") != "private" or chat.get("id") != uid):
        return {"ok": True}
    try:
        if receipt_view:
            m.owner(settings, uid)
            c = m.card(engine, settings, uid, parts[1].removeprefix("paymentreceipt_"))
            if not c["receipt_file_id"]:
                raise m.ReviewError("receipt_missing", 404)
            key = "photo" if c["receipt_kind"] == "photo" else "document"
            return {"method": "sendPhoto" if key == "photo" else "sendDocument", "chat_id": uid,
                    key: c["receipt_file_id"], "protect_content": True,
                    "caption": "Квитанція до " + c["code"] + ". Не підтверджує фактичне зарахування."}
        if command == "/payments":
            m.owner(settings, uid)
            state = parts[1] if len(parts) > 1 else "review"
            page = int(parts[2]) if len(parts) > 2 else 1
            search = " ".join(parts[3:])
            q = m.queue(engine, settings, uid, state=state, page=page, size=10, search=search)
            lines = [f"📋 <b>Заявки · очікують {q['waiting']}</b>",
                     f"Сторінка {q['page']}/{q['pages']} · у вибірці {q['total']}"]
            lines.extend(f"{r['code']} · {escape(r['name'])} · {r['state']}" for r in q["items"])
            lines.extend(["", "Усі: /payments all 1", "Статуси: review, clarification, approved, rejected, all",
                          "Пошук: /payments all 1 номер_заявки", "Наступна сторінка: /payments " + state + " " + str(page+1)])
            return billing.message(uid, "\n".join(lines), [[{"text": "📋 Відкрити чергу та картки",
                "web_app": {"url": settings.origin + "/autodeal-miniapp/payment-review.html"}}]])
        m.enabled(settings)
        if len(parts) != 2:
            raise m.ReviewError("receipt_command_requires_request_code", 422)
        photo = message.get("photo") or []
        document = message.get("document") or {}
        if photo and isinstance(photo, list) and isinstance(photo[-1], dict):
            file = photo[-1]
            receipt_kind = "photo"
        elif document.get("mime_type") in ("application/pdf", "image/png", "image/jpeg"):
            file = document
            receipt_kind = "document"
        else:
            raise m.ReviewError("receipt_image_or_pdf_required", 422)
        if type(file.get("file_size")) is not int or not 0 < file["file_size"] <= 5*1024*1024:
            raise m.ReviewError("receipt_size_invalid", 422)
        file_id = m.text(file.get("file_id"), 512)
        result = m.report_paid(engine, settings, uid, parts[1], time.time(), receipt_file_id=file_id, receipt_kind=receipt_kind)
        if result["state"] not in m.OPEN:
            return billing.message(uid, "Заявку вже завершено. Нову квитанцію не додано. /paysupport")
        return billing.message(uid, "📎 Квитанцію додано до заявки. Надходження перевірить власник.")
    except (m.ReviewError, ValueError):
        return billing.message(uid, "Дія недоступна або дані змінилися. Перевір номер заявки та права доступу. /paysupport")
    except SQLAlchemyError:
        return billing.message(uid, "Тимчасова помилка збереження. Не сплачуй повторно. /paysupport")
