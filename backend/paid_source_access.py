"""Production API work requires a current owner-confirmed manual purchase.

This policy does not edit entitlements or payment history. The production factory
enables it on the Engine, shared by every monitor/scanner/source and delivery.
Explicit legacy/offline applications retain their separately tested access policy.
"""
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from . import billing
from .billing_models import Entitlement
from .manual_payment_models import PaymentRequest
from .models import Search, User

OPTION = "autodeal_confirmed_paid_sources_only"


def strict(bind):
    return bind.get_execution_options().get(OPTION) is True


def confirmed_clause(uid, now=None):
    now = time.time() if now is None else now
    purchase = exists(select(PaymentRequest.id).where(
        PaymentRequest.user_id == uid, PaymentRequest.state == "approved",
        PaymentRequest.amount_minor > 0, PaymentRequest.currency == "UAH",
        PaymentRequest.days > 0, PaymentRequest.expires_at > now))
    # A later revocation/expiry of operational access also blocks the purchase.
    entitlement = exists(select(Entitlement.user_id).where(
        Entitlement.user_id == uid, Entitlement.expires_at > now))
    return purchase & entitlement


def allowed(db, uid, now=None):
    if not strict(db.get_bind()):
        return billing.allowed(db, uid, now)
    return bool(db.scalar(select(confirmed_clause(uid, now))))


def ready_clause(uid):
    return exists(select(User.id).where(User.id == uid, User.ready.is_(True)))


def any_paid(db, now):
    return bool(db.scalar(select(exists(select(User.id).where(
        confirmed_clause(User.id, now))))))


def log_snapshot(engine):
    """Read-only aggregate verification; no IDs, filters, receipts or API calls."""
    if not strict(engine):
        return
    logger = logging.getLogger("uvicorn.error")
    try:
        with Session(engine) as db:
            now = time.time()
            clients = db.scalar(select(func.count(User.id)))
            paid = db.scalar(select(func.count(User.id)).where(confirmed_clause(User.id, now)))
            searches = select(func.count(Search.id)).join(User, User.id == Search.user_id).where(
                Search.enabled.is_(True), User.ready.is_(True))
            all_searches = db.scalar(searches)
            paid_searches = db.scalar(searches.where(confirmed_clause(User.id, now)))
            groups = db.scalar(select(func.count(func.distinct(Search.fingerprint)))
                .join(User, User.id == Search.user_id).where(Search.enabled.is_(True),
                    User.ready.is_(True), confirmed_clause(User.id, now)))
        logger.info("Paid source access guard %s", json.dumps({
            "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "policy": "current_owner_confirmed_manual_purchase",
            "enabled": True, "automatic_paid_startup_checks": False,
            "all_user_records": clients, "current_paid_clients": paid,
            "ready_enabled_searches": all_searches,
            "paid_ready_enabled_searches": paid_searches,
            "excluded_ready_enabled_searches": all_searches-paid_searches,
            "paid_filter_fingerprints": groups,
        }))
    except Exception:
        logger.error("Paid source access guard snapshot unavailable")
