"""Bounded, expiring SELECT-only access diagnosis; no customer identifiers in logs."""
import json
import logging
import math
import os
import time

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from . import billing
from .billing_models import AccessEvent, BillingControl, CampaignRecipient
from .manual_payment_models import PaymentNotice, PaymentRequest
from .models import Delivery, DeliveryTiming, Search, User

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())
_done = set()
_STATES = frozenset(("pending", "retry", "sending", "sent", "failed", "uncertain", "cancelled",
                    "excluded", "superseded", "created", "review", "clarification", "approved", "rejected"))


def _state(value):
    return value if value in _STATES else "unknown"


def snapshot(db, uid, now):
    if type(uid) is not int or not 0 < uid < 2**52 or not math.isfinite(now):
        raise ValueError("invalid_audit_target")
    user = db.execute(select(User.ready).where(User.id == uid)).one_or_none()
    enforce = db.scalar(select(BillingControl.enforce).where(BillingControl.id == billing.CONTROL))
    expiry = billing.expiry(db, uid)
    events = db.execute(select(AccessEvent.kind, AccessEvent.at, AccessEvent.expires_at)
        .where(AccessEvent.user_id == uid).order_by(AccessEvent.at.desc(), AccessEvent.id.desc()).limit(5)).all()
    requests = db.execute(select(PaymentRequest.state, PaymentRequest.revision, PaymentRequest.created_at,
        PaymentRequest.updated_at, PaymentRequest.expires_at).where(PaymentRequest.user_id == uid)
        .order_by(PaymentRequest.created_at.desc(), PaymentRequest.id.desc()).limit(5)).all()
    notices = db.execute(select(PaymentNotice.state, PaymentNotice.revision, PaymentNotice.attempted_at,
        PaymentNotice.retry_at, PaymentNotice.message_id.is_not(None).label("message_present"))
        .join(PaymentRequest, PaymentRequest.id == PaymentNotice.request_id)
        .where(PaymentNotice.user_id == uid, PaymentNotice.kind == "client")
        .order_by(PaymentRequest.updated_at.desc(), PaymentNotice.revision.desc(), PaymentNotice.attempted_at.desc())
        .limit(10)).all()
    searches = db.execute(select(func.count(), func.sum(case((Search.enabled.is_(True), 1), else_=0)))
                         .select_from(Search).where(Search.user_id == uid)).one()
    counts = {}
    for state, count in db.execute(select(Delivery.state, func.count()).where(Delivery.user_id == uid)
                                  .group_by(Delivery.state)):
        safe = _state(state)
        counts[safe] = counts.get(safe, 0)+count
    last = db.scalar(select(func.max(DeliveryTiming.accepted_at)).join(
        Delivery, Delivery.id == DeliveryTiming.delivery_id).where(Delivery.user_id == uid, Delivery.state == "sent"))
    campaign = db.execute(select(CampaignRecipient.state, CampaignRecipient.error, CampaignRecipient.attempted_at)
        .where(CampaignRecipient.user_id == uid).order_by(CampaignRecipient.attempted_at.desc(),
        CampaignRecipient.campaign_id.desc()).limit(1)).one_or_none()
    campaign_result = None
    if campaign:
        code = campaign.error
        safe_code = int(code) if isinstance(code, str) and len(code) == 3 and code.isascii() and code.isdecimal() and 400 <= int(code) <= 599 else None
        campaign_result = {"state": _state(campaign.state), "error_code": safe_code,
                           "attempted_at": campaign.attempted_at}
    return {"exists": user is not None, "ready": bool(user and user.ready),
        "allowed": bool(not enforce or expiry > now), "enforced": bool(enforce), "expires_at": expiry,
        "access_events": [{"kind": r.kind if r.kind in ("manual_paid", "gift", "paid") else "unknown",
                           "at": r.at, "expires_at": r.expires_at} for r in events],
        "requests": [{"state": _state(r.state), "revision": r.revision, "created_at": r.created_at,
                      "updated_at": r.updated_at, "expires_at": r.expires_at} for r in requests],
        "client_notices": [{"state": _state(r.state), "revision": r.revision, "attempted_at": r.attempted_at,
                            "retry_at": r.retry_at, "message_id_present": r.message_present} for r in notices],
        "searches": {"total": searches[0], "enabled": searches[1] or 0},
        "deliveries": {"states": counts, "last_accepted_at": last}, "recent_campaign": campaign_result}


def log_once(engine, settings):
    if not settings.manual_payment_review_enabled or not billing.verified_admin(settings):
        return "disabled"
    raw_uid, raw_until = os.getenv("MANUAL_PAYMENT_AUDIT_USER_ID", ""), os.getenv("MANUAL_PAYMENT_AUDIT_UNTIL", "")
    if not raw_uid.isascii() or not raw_uid.isdecimal() or len(raw_uid) > 16:
        return "disabled"
    try:
        uid, until, now = int(raw_uid), float(raw_until), time.time()
        if not 0 < uid < 2**52 or not math.isfinite(until) or not now < until <= now+3600:
            return "disabled"
    except (ValueError, OverflowError):
        return "disabled"
    key = (uid, until)
    if key in _done:
        return "already_logged"
    _done.add(key)
    try:
        with Session(engine, autoflush=False) as db, db.begin():
            if engine.dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
            data = snapshot(db, uid, now)
        LOG.info("Manual access audit %s", json.dumps(data, sort_keys=True, allow_nan=False))
        return "logged"
    except Exception:
        # SQL exceptions may contain bound customer IDs; never print the error.
        LOG.warning("Manual access audit unavailable")
        return "unavailable"
