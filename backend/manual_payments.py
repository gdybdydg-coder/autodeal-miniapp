"""Owner-reviewed payments. Public creation requires explicit sales configuration.

All mutations serialize on the SAME BillingControl row as existing access grants.
Request, bank-credit uniqueness, entitlement, audit and notices commit together.
This module never verifies a bank transfer itself and never enables sales/enforcement.
"""
from contextlib import contextmanager
import asyncio
import hashlib
import json
import math
import logging
import os
import secrets
import time
import unicodedata

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import billing
from .billing_models import AccessEvent, Entitlement
from .manual_payment_models import (ManualBase, PaymentRequest, PaymentConfirmation, PaymentEvidence,
                                   BankCredit, PaymentAudit, PaymentNotice)
from .models import User

STATES = ("created", "review", "clarification", "approved", "rejected")
OPEN = STATES[:3]
DAY = 86400


class ReviewError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def public_creation_allowed():
    # The owner explicitly selected bank payments on 2026-10-02. Commercial
    # availability still requires a persisted approved offer and enabled sales.
    return (os.getenv("MANUAL_PAYMENT_PUBLIC_ENABLED") == "true"
            and os.getenv("MANUAL_PAYMENT_REVIEW_ENABLED") == "true")


def initialize(engine, settings):
    if settings.manual_payment_review_enabled:
        ManualBase.metadata.create_all(engine)


def enabled(settings):
    if not settings.manual_payment_review_enabled:
        raise ReviewError("manual_review_disabled", 404)
    if not billing.verified_admin(settings):
        raise ReviewError("owner_configuration_unavailable", 503)


def owner(settings, actor):
    enabled(settings)
    if type(actor) is not int or actor != settings.admin_telegram_id:
        raise ReviewError("owner_only", 403)


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ReviewError("invalid_time", 422)
    return value


def text(value, maximum=500, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(unicodedata.category(c).startswith("C") for c in value)
            or (not empty and not value.strip())):
        raise ReviewError("invalid_text", 422)
    return value.strip()


def amount(value):
    if type(value) is not int or not 0 < value <= 100000000:
        raise ReviewError("invalid_amount_minor", 422)
    return value


def bank_key(account, operation):
    # Keep raw bank identifiers out of audit/logs. This is owner-entered evidence,
    # not proof of credit. Stable receiving-account + operation ID is required.
    parts = [unicodedata.normalize("NFKC", text(v, 120)).upper() for v in (account, operation)]
    if any(len(v) < 3 for v in parts):
        raise ReviewError("invalid_bank_reference", 422)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def mutation(engine, settings):
    enabled(settings)
    with Session(engine) as db, db.begin():
        if not billing.control(db, lock=True):
            raise ReviewError("access_control_unavailable", 503)
        yield db


def request_row(db, code, uid=None):
    row = db.get(PaymentRequest, text(code, 20))
    if row is None or (uid is not None and row.user_id != uid):
        raise ReviewError("request_not_found", 404)
    return row


def public(row):
    return {"code": row.id, "amount_minor": row.amount_minor, "currency": row.currency,
            "days": row.days, "state": row.state, "created_at": row.created_at,
            "revision": row.revision, "owner_note": row.owner_note,
            "receipt_attached": bool(row.receipt_file_id), "expires_at": row.expires_at}


def audit(db, row, actor, action, now, before=None, after=None):
    db.add(PaymentAudit(id=secrets.token_hex(16), request_id=row.id, actor=actor,
                       action=action, at=now, revision=row.revision,
                       before_expiry=before, after_expiry=after))


def notice(db, row, uid, kind, body):
    db.add(PaymentNotice(id=secrets.token_hex(16), request_id=row.id, revision=row.revision,
                        kind=kind, user_id=uid, text=body))


def create_request(engine, settings, uid, now, *, name="Клієнт", username=None, accepted_terms=None):
    """Internal ledger operation; public entry point additionally checks policy.

    Identity and display fields must come from verified Telegram data, not form JSON.
    """
    timestamp(now)
    # Telegram profile labels are display-only, never payment/identity evidence.
    # Names can contain emoji joiners, invisible marks, or only whitespace;
    # absent usernames can arrive as empty strings. Do not reject checkout.
    def label(value, maximum):
        if not isinstance(value, str):
            return ""
        return " ".join("".join(c if not unicodedata.category(c).startswith("C")
                               else " " for c in value).split())[:maximum].strip()
    name = label(name, 100) or "Клієнт"
    username = label(username, 64) or None
    with mutation(engine, settings) as db:
        if accepted_terms is not None:
            from . import manual_checkout
            manual_checkout.require_sales(db)
            if accepted_terms != manual_checkout.TERMS_VERSION:
                raise ReviewError("terms_changed", 409)
        if type(uid) is not int or uid <= 0 or db.get(User, uid) is None:
            raise ReviewError("known_user_required", 409)
        current = db.scalar(select(PaymentRequest).where(PaymentRequest.active_user_id == uid))
        if current:
            return public(current)
        for _ in range(10):
            code = "AD-" + secrets.token_hex(6).upper()
            if db.get(PaymentRequest, code) is None:
                break
        else:
            raise ReviewError("request_code_unavailable", 503)
        row = PaymentRequest(id=code, user_id=uid, active_user_id=uid, name=name,
                             username=username, amount_minor=25000, currency="UAH", days=30,
                             state="created", created_at=now, updated_at=now, revision=0,
                             transfer_note="", owner_note="")
        if accepted_terms is not None:
            row.terms_version, row.terms_text = accepted_terms, manual_checkout.TERMS
            row.terms_accepted_at = now
        db.add(row)
        audit(db, row, uid, "created", now)
        db.flush()
        return public(row)


def report_paid(engine, settings, uid, code, now, *, reported_amount_minor=None,
                transfer_note=None, receipt_file_id=None, receipt_kind=None):
    """Receipt IDs accepted only from the authenticated Telegram webhook adapter."""
    timestamp(now)
    if reported_amount_minor is not None:
        amount(reported_amount_minor)
    if transfer_note is not None:
        transfer_note = text(transfer_note)
    if receipt_file_id is not None:
        receipt_file_id = text(receipt_file_id, 512)
        if receipt_kind not in ("photo", "document"):
            raise ReviewError("invalid_receipt_kind", 422)
    elif receipt_kind is not None:
        raise ReviewError("receipt_required", 422)
    with mutation(engine, settings) as db:
        row = request_row(db, code, uid)
        if row.state not in OPEN:
            return public(row)
        fields = ("reported_amount_minor", "transfer_note", "receipt_file_id", "receipt_kind")
        values = (reported_amount_minor, transfer_note, receipt_file_id, receipt_kind)
        changed = any(v is not None and getattr(row, k) != v for k, v in zip(fields, values))
        if row.state == "review" and not changed:
            return public(row)
        for key, value in zip(fields, values):
            if value is not None:
                setattr(row, key, value)
        row.state, row.owner_note, row.updated_at = "review", "", now
        row.revision += 1
        db.add(PaymentEvidence(request_id=row.id, revision=row.revision, at=now,
               reported_amount_minor=row.reported_amount_minor, transfer_note=row.transfer_note,
               receipt_file_id=row.receipt_file_id, receipt_kind=row.receipt_kind))
        audit(db, row, uid, "paid_reported", now)
        notice(db, row, settings.admin_telegram_id, "owner",
               f"📥 Заявка {row.id} очікує перевірки. 250 грн / 30 днів. Уся черга: /payments")
        return public(row)


def get_status(engine, settings, uid, code):
    enabled(settings)
    with Session(engine) as db:
        return public(request_row(db, code, uid))


def queue(engine, settings, actor, *, state="review", search="", page=1, size=20):
    owner(settings, actor)
    if state not in (*STATES, "all") or type(page) is not int or not 1 <= page <= 100000:
        raise ReviewError("invalid_page_or_state", 422)
    if type(size) is not int or not 1 <= size <= 50:
        raise ReviewError("invalid_page_size", 422)
    search = text(search, 100, empty=True)
    with Session(engine) as db:
        counts = dict(db.execute(select(PaymentRequest.state, func.count()).group_by(PaymentRequest.state)).all())
        conditions = []
        if state != "all":
            conditions.append(PaymentRequest.state == state)
        if search:
            escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped + "%"
            matches = [func.lower(col).like(pattern, escape="\\") for col in
                       (PaymentRequest.id, PaymentRequest.name, PaymentRequest.username)]
            if search.isascii() and search.isdecimal() and len(search) <= 16:
                matches.append(PaymentRequest.user_id == int(search))
            conditions.append(or_(*matches))
        total = db.scalar(select(func.count()).select_from(PaymentRequest).where(*conditions)) or 0
        rows = db.scalars(select(PaymentRequest).where(*conditions)
                          .order_by(PaymentRequest.created_at, PaymentRequest.id)
                          .offset((page-1)*size).limit(size))
        return {"items": [dict(public(r), user_id=r.user_id, name=r.name, username=r.username) for r in rows],
                "counts": {s: counts.get(s, 0) for s in STATES}, "waiting": counts.get("review", 0),
                "total": total, "page": page, "pages": max(1, (total+size-1)//size)}


def card(engine, settings, actor, code):
    owner(settings, actor)
    with Session(engine) as db:
        row = request_row(db, code)
        history = db.scalars(select(PaymentAudit).where(PaymentAudit.request_id == code)
                             .order_by(PaymentAudit.at, PaymentAudit.id))
        return dict(public(row), user_id=row.user_id, name=row.name, username=row.username,
                    reported_amount_minor=row.reported_amount_minor, transfer_note=row.transfer_note,
                    receipt_file_id=row.receipt_file_id, current_expiry=billing.expiry(db, row.user_id),
                    receipt_kind=row.receipt_kind,
                    history=[{"actor": h.actor, "action": h.action, "at": h.at,
                              "before": h.before_expiry, "after": h.after_expiry} for h in history])


def notices(engine, settings, actor, code):
    owner(settings, actor)
    with Session(engine) as db:
        request_row(db, code)
        rows = db.scalars(select(PaymentNotice).where(PaymentNotice.request_id == code)
                         .order_by(PaymentNotice.revision, PaymentNotice.kind))
        return [{"id": r.id, "kind": r.kind, "state": r.state, "revision": r.revision} for r in rows]


def change_state(engine, settings, actor, code, state, note, revision, now):
    owner(settings, actor)
    timestamp(now)
    note = text(note)
    if state not in ("clarification", "rejected") or type(revision) is not int:
        raise ReviewError("invalid_action", 422)
    with mutation(engine, settings) as db:
        row = request_row(db, code)
        if row.state == state and row.owner_note == note and row.revision == revision+1:
            return public(row)  # Exact retry after a committed response was lost.
        if row.state not in OPEN or row.revision != revision:
            raise ReviewError("request_changed")
        row.state, row.owner_note, row.updated_at = state, note, now
        row.revision += 1
        if state == "rejected":
            row.active_user_id = None
        audit(db, row, actor, state, now)
        label = "Потрібне уточнення" if state == "clarification" else "Заявку відхилено"
        suffix = " Відхилення не означає повернення коштів." if state == "rejected" else ""
        notice(db, row, row.user_id, "client", f"{label}: {row.id}. {note}{suffix}")
        return public(row)


def preview(engine, settings, actor, code, revision, now, *, account, operation,
            actual_amount_minor, bank_verified):
    owner(settings, actor)
    timestamp(now)
    amount(actual_amount_minor)
    if bank_verified is not True or type(revision) is not int:
        raise ReviewError("explicit_owner_verification_required", 422)
    key = bank_key(account, operation)
    with mutation(engine, settings) as db:
        row = request_row(db, code)
        if row.state != "review" or row.revision != revision:
            raise ReviewError("request_changed")
        if row.amount_minor != actual_amount_minor:
            raise ReviewError("amount_requires_clarification")
        if db.get(BankCredit, key):
            raise ReviewError("bank_credit_already_used")
        before = billing.expiry(db, row.user_id)
        token = secrets.token_urlsafe(24)
        db.add(PaymentConfirmation(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                request_id=code, actor=actor, revision=revision, before_expiry=before,
                bank_key=key, amount_minor=actual_amount_minor, deadline=now+300))
        return {"confirmation": token, "code": code, "user_id": row.user_id, "name": row.name,
                "days": row.days, "before": before, "estimated_expiry": max(now, before)+row.days*DAY,
                "valid_until": now+300}


def confirm(engine, settings, actor, token, now):
    owner(settings, actor)
    timestamp(now)
    token_hash = hashlib.sha256(text(token, 100).encode()).hexdigest()
    with mutation(engine, settings) as db:
        review = db.get(PaymentConfirmation, token_hash)
        if review is None or review.actor != actor:
            raise ReviewError("invalid_confirmation", 403)
        if review.result is not None:
            return {"code": review.request_id, "expires_at": review.result, "replayed": True}
        row = request_row(db, review.request_id)
        before = billing.expiry(db, row.user_id)
        if (now >= review.deadline or row.state != "review" or row.revision != review.revision
                or before != review.before_expiry or review.amount_minor != row.amount_minor):
            raise ReviewError("request_or_access_changed")
        if db.get(BankCredit, review.bank_key):
            raise ReviewError("bank_credit_already_used")
        until = max(now, before) + row.days*DAY
        db.add(BankCredit(bank_key=review.bank_key, request_id=row.id, user_id=row.user_id,
                         amount_minor=row.amount_minor, actor=actor, at=now,
                         before_expiry=before, after_expiry=until))
        db.merge(Entitlement(user_id=row.user_id, expires_at=until, updated_at=now))
        db.add(AccessEvent(id="manual:"+row.id, user_id=row.user_id, actor=actor, kind="manual_paid",
                           at=now, expires_at=until, reason="Owner explicitly confirmed bank credit"))
        row.state, row.active_user_id, row.expires_at, row.updated_at = "approved", None, until, now
        row.revision += 1
        review.result = until
        audit(db, row, actor, "approved", now, before, until)
        notice(db, row, row.user_id, "client",
               f"✅ Заявку {row.id} підтверджено власником. Доступ до {billing.date_text(until)} (Київ). "
               "Автоматичного продовження немає. Зупинені сповіщення залишаються зупиненими.")
        return {"code": row.id, "expires_at": until, "replayed": False}


def retry_notice(engine, settings, actor, notice_id):
    owner(settings, actor)
    with mutation(engine, settings) as db:
        row = db.get(PaymentNotice, text(notice_id, 32))
        if row is None or row.state != "failed":
            raise ReviewError("only_known_failure_can_retry")
        row.state, row.retry_at, row.claim = "pending", 0, ""


def deliver_notice(engine, settings, request, now=None):
    """Injected transport; app scheduler requires a separate opt-in setting.

    Crash/timeout leaves uncertain acceptance, never blind duplicate retries.
    """
    enabled(settings)
    now = timestamp(time.time() if now is None else now)
    claim = secrets.token_hex(16)
    with mutation(engine, settings) as db:
        db.execute(update(PaymentNotice).where(PaymentNotice.state == "sending",
                    PaymentNotice.attempted_at < now-60).values(state="uncertain"))
        newer_request = select(PaymentRequest.id).where(
            PaymentRequest.id == PaymentNotice.request_id,
            PaymentRequest.revision > PaymentNotice.revision).exists()
        # Keep historical notices, but do not deliver obsolete queued states
        # (e.g. "clarification needed" after the owner already approved access).
        # Already attempted/uncertain sends remain available for reconciliation.
        db.execute(update(PaymentNotice).where(
            PaymentNotice.state.in_(("pending", "retry")), newer_request
        ).values(state="superseded"))
        row = db.scalar(select(PaymentNotice).where(PaymentNotice.state.in_(("pending", "retry")),
                        PaymentNotice.retry_at <= now).order_by(PaymentNotice.id).limit(1))
        if row is None:
            return "empty"
        row.state, row.claim, row.attempted_at = "sending", claim, now
        key = row.id
        uid = settings.admin_telegram_id if row.kind == "owner" else row.user_id
        payload = {"chat_id": uid, "text": row.text, "allow_paid_broadcast": False}
    try:
        response = request(settings.bot_token, "sendMessage", payload, timeout=5)
    except Exception:
        response = {}
    if not isinstance(response, dict):
        response = {}
    with mutation(engine, settings) as db:
        row = db.get(PaymentNotice, key)
        if row.state != "sending" or row.claim != claim:
            return "uncertain"
        from .billing_campaign import apply_send_result
        apply_send_result(row, response, now)
        return row.state


async def run_notices(engine, settings, stop):
    from . import telegram_setup, manual_launch, manual_repeat_notice, manual_repeat_preflight
    next_progress = 0
    while not stop.is_set():
        try:
            await asyncio.to_thread(deliver_notice, engine, settings, telegram_setup.call)
            await asyncio.to_thread(manual_repeat_preflight.run_once, engine, settings, telegram_setup.call)
            await asyncio.to_thread(manual_repeat_notice.initialize, engine, settings)
            outcome = await asyncio.to_thread(manual_launch.tick, engine, settings, telegram_setup.call)
            repeat_outcome = await asyncio.to_thread(manual_repeat_notice.tick, engine, settings, telegram_setup.call)
            if time.monotonic() >= next_progress:
                await asyncio.to_thread(manual_launch.log_progress, engine, outcome)
                await asyncio.to_thread(manual_repeat_notice.log_progress, engine, repeat_outcome, settings)
                next_progress = time.monotonic() + 30
        except Exception:
            logging.getLogger(__name__).error("Manual notice delivery unavailable; durable state retained")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
