"""Authenticated customer checkout and owner-only payment review API."""
from html import escape
import time
from urllib.parse import parse_qsl, urlencode
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.exc import SQLAlchemyError

from . import billing, manual_payments as m, manual_checkout, telegram_setup


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paid(Body):
    reported_amount_minor: StrictInt | None = None
    transfer_note: str | None = Field(default=None, max_length=500)


class Create(Body):
    terms_version: str = Field(min_length=1, max_length=40)


class Action(Body):
    code: str = Field(max_length=20)
    state: str
    revision: StrictInt
    note: str = Field(min_length=1, max_length=500)


class Preview(Body):
    code: str = Field(max_length=20)
    revision: StrictInt
    account: str | None = Field(default=None, min_length=3, max_length=120)
    operation: str | None = Field(default=None, min_length=3, max_length=120)
    actual_amount_minor: StrictInt | None = None
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
    def create(body: Create, uid=Depends(available), x_telegram_init_data: str = Header(default="")):
        if not m.public_creation_allowed():
            raise HTTPException(409, "manual_sales_closed")
        # identity already verified HMAC, age, duplicate fields and sender type.
        signed_user = json.loads(dict(parse_qsl(x_telegram_init_data))["user"])
        name = str(signed_user.get("first_name") or "Клієнт")[:100]
        username = signed_user.get("username")
        return call(manual_checkout.create, engine, settings, uid, time.time(), body.terms_version,
                    name=name, username=username)

    @router.get("/offer")
    def offer(uid=Depends(available)):
        return call(manual_checkout.status, engine, settings, uid)

    @router.get("/{code}/requisites")
    def requisites(code: str, uid=Depends(available)):
        return call(manual_checkout.requisites, engine, settings, uid, code)

    @router.get("/admin")
    def queue(state: str = "review", search: str = "", page: int = 1, size: int = 20, uid=Depends(admin)):
        return call(m.queue, engine, settings, uid, state=state, search=search, page=page, size=size)

    @router.get("/admin/{code}")
    def card(code: str, uid=Depends(admin)):
        data = call(m.card, engine, settings, uid, code)
        # This account hint is returned only after both owner authorization and
        # request lookup. Missing configuration must not hide an existing order.
        try:
            profile = manual_checkout.subscription_preview.receiving_profile()
            data["receiving_account"] = profile.get("iban") if isinstance(profile, dict) else None
        except Exception:
            data["receiving_account"] = None
        return data

    @router.get("/admin/{code}/notices")
    def notices(code: str, uid=Depends(admin)):
        return call(m.notices, engine, settings, uid, code)

    @router.get("/admin/{code}/receipt")
    def receipt(code: str, uid=Depends(admin)):
        from . import manual_receipt_proxy
        data = call(m.card, engine, settings, uid, code)
        body, media_type = call(manual_receipt_proxy.fetch_receipt, settings,
                               data.get("receipt_file_id"), data.get("receipt_kind"))
        return Response(body, media_type=media_type, headers={"Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})

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
        from . import manual_receipts
        call(manual_receipts.await_receipt, engine, settings, uid, code, time.time())
        return call(m.get_status, engine, settings, uid, code)

    app.include_router(router)


def handle(engine, settings, event):
    if not settings.manual_payment_review_enabled:
        return None
    from . import manual_receipts
    receipt_reply = manual_receipts.handle(engine, settings, event)
    if receipt_reply is not None:
        return receipt_reply
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
            labels = {"created": "Очікує переказу", "review": "На перевірці", "clarification": "Потрібне уточнення",
                      "approved": "Підтверджено", "rejected": "Відхилено"}
            lines = [f"📋 <b>Заявки · очікують {q['waiting']}</b>",
                     "Вибери заявку нижче. Перевір надходження в банку, потім підтвердь оплату."]
            lines.extend(f"• {escape(r['name'])} · {labels[r['state']]}" for r in q["items"])
            if not q["items"]:
                lines.append("У цій вибірці заявок немає.")
            if q["pages"] > 1:
                lines.extend([f"Сторінка {q['page']}/{q['pages']}",
                              "Наступна: /payments " + state + " " + str(page+1)] if page < q["pages"] else
                             [f"Сторінка {q['page']}/{q['pages']}"])
            url = settings.origin + "/autodeal-miniapp/payment-review.html"
            buttons = [[{"text": "Перевірити оплату · " + r["name"][:50],
                         "web_app": {"url": url+"?"+urlencode({"code": r["code"], "v": "20261002-receipt-1"})}}]
                       for r in q["items"]]
            buttons.append([{"text": "📋 Усі заявки", "web_app": {"url": url+"?v=20261002-receipt-1"}}])
            return billing.message(uid, "\n\n".join(lines), buttons)
        m.enabled(settings)
        if len(parts) != 2:
            raise m.ReviewError("receipt_command_requires_request_code", 422)
        return manual_receipts.legacy_submission(engine, settings, uid, parts[1], event)
    except (m.ReviewError, ValueError):
        return billing.message(uid, "Дія недоступна або дані змінилися. Перевір номер заявки та права доступу. /paysupport")
    except SQLAlchemyError:
        return billing.message(uid, "Тимчасова помилка збереження. Не сплачуй повторно. /paysupport")
