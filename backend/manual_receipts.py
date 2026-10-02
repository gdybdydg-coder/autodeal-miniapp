"""Durable, scoped receipt collection. Images are evidence, never payment approval."""
import time

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing, manual_payments as m, telegram_setup
from .manual_payment_models import (PaymentRequest, PaymentEvidence, ReceiptExpectation,
                                   ReceiptSubmission, ReceiptNotice)

PROMPT = "📸 Надішліть у цей чат скриншот оплати.\nБез підпису та додаткового тексту — просто зображення"
ACK = ("✅ Скриншот отримано! Ваша заявка очікує перевірки.\n"
       "Після підтвердження оплати адміністратором доступ\nбуде активовано. "
       "Повторно надсилати заявку не потрібно")
PREFIX = "ad-receipt:"
MAX_IMAGE = 10*1024*1024


def _expect(db, uid, code, now):
    row = db.get(ReceiptExpectation, uid)
    if row is None:
        row = ReceiptExpectation(user_id=uid, started_at=now)
        db.add(row)
    row.request_id, row.active, row.awaiting_image, row.updated_at = code, True, True, now
    return row


def await_receipt(engine, settings, uid, code, now=None):
    now = m.timestamp(time.time() if now is None else now)
    with m.mutation(engine, settings) as db:
        row = m.request_row(db, code, uid)
        if row.state not in m.OPEN:
            return billing.message(uid, "Цю заявку вже завершено. Перегляньте підписку: /subscription")
        _expect(db, uid, row.id, now)
    return billing.message(uid, PROMPT)


def _choices(uid, rows, submission=None):
    buttons = []
    for index, row in enumerate(rows, 1):
        action = "choose:"+str(submission.update_id)+":"+row.id if submission else "select:"+row.id
        buttons.append([{"text": "Заявка "+str(index)+" · "+billing.date_text(row.created_at)+" · 250 грн",
                         "callback_data": PREFIX+action}])
    return billing.message(uid, "Для якої заявки цей скриншот? Оберіть заявку кнопкою нижче.", buttons)


def _image(message, legacy_pdf=False):
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        candidates = [p for p in photos if isinstance(p, dict) and isinstance(p.get("file_id"), str)]
        if not candidates:
            return None
        def size(p):
            width, height = p.get("width"), p.get("height")
            return (width*height if type(width) is int and type(height) is int else 0,
                    p.get("file_size") if type(p.get("file_size")) is int else 0)
        item, kind = max(candidates, key=size), "photo"
    else:
        item = message.get("document")
        allowed = ("image/jpeg", "image/png", "image/webp", *(["application/pdf"] if legacy_pdf else []))
        if not isinstance(item, dict) or item.get("mime_type") not in allowed:
            return None
        kind = "document"
    length = item.get("file_size")
    if length is not None and (type(length) is not int or not 0 < length <= MAX_IMAGE):
        raise m.ReviewError("receipt_size_invalid", 422)
    return m.text(item.get("file_id"), 512), kind


def _save(db, settings, uid, row, submission, expectation, now):
    if row.state not in m.OPEN:
        return billing.message(uid, "Заявку вже завершено. Новий скриншот не додано. /subscription")
    if submission.state == "saved":
        return billing.message(uid, ACK)
    row.receipt_file_id, row.receipt_kind = submission.file_id, submission.kind
    row.state, row.owner_note, row.updated_at = "review", "", now
    row.revision += 1
    db.add(PaymentEvidence(request_id=row.id, revision=row.revision, at=now,
        reported_amount_minor=row.reported_amount_minor, transfer_note=row.transfer_note,
        receipt_file_id=submission.file_id, receipt_kind=submission.kind))
    m.audit(db, row, uid, "receipt_received", now)
    submission.state, submission.request_id = "saved", row.id
    expectation.request_id, expectation.active = row.id, True
    expectation.awaiting_image, expectation.updated_at = False, now
    username = " · @"+row.username if row.username else ""
    caption = ("💳 Заявка на підписку AutoDeal\n\n👤 Клієнт: "+row.name+username+
        "\n🆔 Telegram ID: "+str(uid)+"\n🧾 Заявка: "+row.id+
        "\n💰 До сплати за тарифом: 250 грн\n📅 Термін: 30 днів"+
        "\n🕒 Отримано: "+billing.date_text(now)+" (Київ)\n⏳ Очікує перевірки"+
        "\n\nПеревірте фактичне надходження у банку перед підтвердженням.")
    notice = m.notice(db, row, settings.admin_telegram_id, "owner", caption)
    db.add(ReceiptNotice(notice_id=notice.id, file_id=submission.file_id, kind=submission.kind))
    return billing.message(uid, ACK)


def _active_requests(db, uid):
    return list(db.scalars(select(PaymentRequest).where(PaymentRequest.user_id == uid,
                     PaymentRequest.state.in_(m.OPEN)).order_by(PaymentRequest.created_at, PaymentRequest.id)))


def legacy_submission(engine, settings, uid, code, event, now=None):
    """Keep explicit old receipt commands bound to their exact owned request."""
    now = m.timestamp(time.time() if now is None else now)
    msg, update_id = event.get("message") or {}, event.get("update_id")
    message_id = msg.get("message_id")
    if (type(update_id) is not int or not 0 <= update_id < 2**63
            or type(message_id) is not int or not 0 < message_id < 2**63):
        raise m.ReviewError("invalid_receipt_message", 422)
    with m.mutation(engine, settings) as db:
        row = m.request_row(db, code, uid)
        if row.state not in m.OPEN:
            return billing.message(uid, "Заявку вже завершено. Нову квитанцію не додано. /paysupport")
        existing = db.get(ReceiptSubmission, update_id) or db.scalar(select(ReceiptSubmission).where(
            ReceiptSubmission.user_id == uid, ReceiptSubmission.message_id == message_id))
        if existing:
            if existing.user_id != uid or existing.request_id not in (None, code):
                raise m.ReviewError("invalid_receipt_message", 422)
            if existing.state == "saved":
                return billing.message(uid, ACK)
        image = _image(msg, legacy_pdf=True)
        if image is None:
            raise m.ReviewError("receipt_image_or_pdf_required", 422)
        submission = existing or ReceiptSubmission(update_id=update_id, user_id=uid, message_id=message_id,
            file_id=image[0], kind=image[1], created_at=now, state="pending")
        db.add(submission)
        expectation = _expect(db, uid, code, now)
        return _save(db, settings, uid, row, submission, expectation, now)


def _handle(engine, settings, event, now, request=None):
    cb = event.get("callback_query")
    callback = isinstance(cb, dict)
    msg = cb.get("message", {}) if callback else event.get("message", {})
    if not isinstance(msg, dict):
        return None
    sender, chat = (cb.get("from") if callback else msg.get("from")), msg.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    uid = sender.get("id")
    if type(uid) is not int or not 0 < uid < 2**52 or sender.get("is_bot") or chat.get("type") != "private" or chat.get("id") != uid:
        return None
    update_id = event.get("update_id")
    if type(update_id) is not int or not 0 <= update_id < 2**63:
        return {"ok": True}
    if callback:
        action = cb.get("data")
        if not isinstance(action, str) or not action.startswith(PREFIX):
            return None
        if not isinstance(cb.get("id"), str):
            return {"ok": True}
        try:
            (request or telegram_setup.call)(settings.bot_token, "answerCallbackQuery",
                                            {"callback_query_id": cb["id"]}, timeout=3)
        except Exception:
            pass  # Spinner acknowledgement never controls receipt persistence.
    else:
        content = msg.get("text") or ""
        # Existing commands, including explicit legacy receipt captions, keep
        # their original handlers and can never be swallowed by image waiting.
        if isinstance(content, str) and content.lstrip().startswith("/"):
            return None
        caption = msg.get("caption") or ""
        if (isinstance(caption, str) and caption.split()
                and caption.split()[0].partition("@")[0] == "/payment_receipt"):
            return None
    with Session(engine) as read:
        expected = read.get(ReceiptExpectation, uid)
        if expected is None or not expected.active:
            return None if not callback else billing.message(uid, "Відкрийте оплату через /subscription")
    with m.mutation(engine, settings) as db:
        expectation = db.get(ReceiptExpectation, uid)
        if expectation is None or not expectation.active:
            return None if not callback else billing.message(uid, "Відкрийте оплату через /subscription")
        rows = _active_requests(db, uid)
        if not rows:
            expectation.active = expectation.awaiting_image = False
            return billing.message(uid, "Заявку вже завершено. Перегляньте статус: /subscription")
        if callback:
            pieces = action.removeprefix(PREFIX).split(":")
            if len(pieces) == 2 and pieces[0] == "select":
                row = next((r for r in rows if r.id == pieces[1]), None)
                if row is None:
                    raise m.ReviewError("request_not_found", 404)
                _expect(db, uid, row.id, now)
                return billing.message(uid, PROMPT)
            if len(pieces) != 3 or pieces[0] != "choose" or not pieces[1].isascii() or not pieces[1].isdecimal():
                raise m.ReviewError("invalid_receipt_choice", 422)
            submission = db.get(ReceiptSubmission, int(pieces[1]))
            row = next((r for r in rows if r.id == pieces[2]), None)
            if submission is None or submission.user_id != uid or row is None:
                raise m.ReviewError("request_not_found", 404)
            return _save(db, settings, uid, row, submission, expectation, now)
        duplicate = db.get(ReceiptSubmission, update_id)
        message_id = msg.get("message_id")
        if type(message_id) is not int or not 0 < message_id < 2**63:
            return {"ok": True}
        if duplicate is None:
            duplicate = db.scalar(select(ReceiptSubmission).where(ReceiptSubmission.user_id == uid,
                                                   ReceiptSubmission.message_id == message_id))
        if duplicate:
            if duplicate.user_id != uid:
                return {"ok": True}
            return billing.message(uid, ACK) if duplicate.state == "saved" else _choices(uid, rows, duplicate)
        image = _image(msg)
        if image is None:
            return billing.message(uid, PROMPT) if expectation.awaiting_image else None
        submission = ReceiptSubmission(update_id=update_id, user_id=uid, message_id=message_id,
            file_id=image[0], kind=image[1], created_at=now, state="pending")
        db.add(submission)
        selected = next((r for r in rows if r.id == expectation.request_id), None)
        if selected is None and len(rows) == 1:
            selected = rows[0]
        if selected is None:
            return _choices(uid, rows, submission)
        return _save(db, settings, uid, selected, submission, expectation, now)


def handle(engine, settings, event, now=None, *, request=None):
    if not settings.manual_payment_review_enabled:
        return None
    try:
        return _handle(engine, settings, event, time.time() if now is None else now, request)
    except m.ReviewError:
        msg = event.get("message") or (event.get("callback_query") or {}).get("message") or {}
        uid = (msg.get("chat") or {}).get("id")
        return billing.message(uid, "Надішліть зображення JPG, PNG або WebP до 10 МБ. "+PROMPT) if type(uid) is int else {"ok": True}
    except SQLAlchemyError:
        msg = event.get("message") or (event.get("callback_query") or {}).get("message") or {}
        uid = (msg.get("chat") or {}).get("id")
        return billing.message(uid, "Скриншот поки не збережено. Спробуйте ще раз трохи пізніше. /paysupport") if type(uid) is int else {"ok": True}


def capabilities(engine, app=None):
    """Sanitized startup evidence of additive storage and registered API paths."""
    tables = ("manual_receipt_expectations", "manual_receipt_submissions",
              "manual_receipt_notices", "manual_owner_confirmations")
    database = inspect(engine)
    # FastAPI can retain an included APIRouter as a nested route object; its
    # OpenAPI path registry resolves both nested and flat router versions.
    paths = set(app.openapi().get("paths", {})) if app is not None else set()
    return {"receipt_tables_ready": all(database.has_table(table) for table in tables),
            "photo_handler_available": callable(handle), "image_document_supported": True,
            "caption_required": False, "durable_expectations": True,
            "webhook_route_registered": "/telegram/webhook" in paths,
            "owner_receipt_route_registered": "/api/manual-payments/admin/{code}/receipt" in paths,
            "owner_confirmation_route_registered": "/api/manual-payments/admin/confirm" in paths}
