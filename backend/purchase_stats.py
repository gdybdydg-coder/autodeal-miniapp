"""Lifetime owner-confirmed purchases; never an entitlement/access decision."""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .manual_payment_models import PaymentRequest
from .models import User


def excluded_user_ids(settings=None, *, admin_uid=None):
    # These are explicit application-owned pilot accounts, not name/ID heuristics.
    from .stars_test import OWNER as PAYMENT_TEST_OWNER
    from .subscription_preview import OWNER as PREVIEW_OWNER
    owner = admin_uid if admin_uid is not None else getattr(settings, "admin_telegram_id", 0)
    excluded = {PAYMENT_TEST_OWNER, PREVIEW_OWNER}
    if owner:
        excluded.add(owner)
    configured = getattr(settings, "stats_excluded_user_ids", "")
    for token in configured.split(",") if configured else ():
        token = token.strip()
        if not token.isascii() or not token.isdecimal() or not 0 < int(token) < 2**52:
            raise ValueError("Invalid STATS_EXCLUDED_USER_IDS")
        excluded.add(int(token))
    return tuple(sorted(excluded))


def counts(db, settings=None, *, admin_uid=None):
    # Both historical bank-reference and current receipt-review writers commit
    # approved only after the owner's explicit confirmation. No expiry condition:
    # a former buyer is still a buyer. Synthetic previews/Stars tests, gifts and
    # unconfirmed screenshots live elsewhere or have a different request state.
    purchased = select(PaymentRequest.user_id).where(
        PaymentRequest.state == "approved", PaymentRequest.amount_minor > 0,
        PaymentRequest.currency == "UAH", PaymentRequest.days > 0,
    ).distinct().subquery()
    # A single statement gives both counts the same DB snapshot and client cohort.
    total, buyers = db.execute(select(func.count(User.id), func.count(purchased.c.user_id))
        .select_from(User).outerjoin(purchased, purchased.c.user_id == User.id)
        .where(User.id.not_in(excluded_user_ids(settings, admin_uid=admin_uid)))).one()
    return {"total": total, "buyers": buyers, "not_purchased": total-buyers}


def text(values):
    return ("💳 Підписки\n"
            f"✅ Купили тариф: {values['buyers']}/{values['total']}\n"
            f"⏳ Ще не купили: {values['not_purchased']}")


def log_snapshot(engine, settings):
    """Verify the deployed query/formatter using aggregates only, without sending."""
    if not settings.admin_telegram_id:
        return
    logger = logging.getLogger("uvicorn.error")
    try:
        from .bot_commands import stats_text, STATS_UNAVAILABLE
        with Session(engine) as db:
            values = counts(db, settings)
            command_text = stats_text(db, settings.admin_telegram_id,
                                      settings.admin_telegram_id, settings=settings)
        logger.info("Admin purchase stats %s", json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            **values, "text": text(values), "source": "owner_confirmed_manual_requests",
            "command_render_ok": command_text != STATS_UNAVAILABLE,
        }, ensure_ascii=False))
    except Exception:
        # Never log receipts, SQL parameters or pretend a failed query returned 0.
        logger.error("Admin purchase stats unavailable")
