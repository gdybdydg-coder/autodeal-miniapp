"""One owner-only delivery check before the explicitly authorized repeat notice."""
import logging
import os
import time

from . import billing, manual_checkout, manual_payments as m
from .models import SourceProbe

CAMPAIGN = "manual-card-reminder-20261002-v1"
PROBE_ID = "manual-repeat-owner-requisites-20261002-v1"
MARKER = "owner-requisites-v1"
ENV = "MANUAL_PAYMENT_REPEAT_PREFLIGHT"
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())


def proof(code):
    return {"marker": MARKER, "campaign": CAMPAIGN,
            "release": os.getenv("RENDER_GIT_COMMIT", ""), "code": code}


def ready(db):
    row = db.get(SourceProbe, PROBE_ID)
    return bool(row and row.status == "accepted" and isinstance(row.result, dict)
                and row.result.get("marker") == MARKER
                and row.result.get("campaign") == CAMPAIGN
                and row.result.get("code") == "accepted")


def log_result(state, code):
    LOG.info("Manual repeat owner preflight state=%s code=%s", state, code)


def run_once(engine, settings, request, now=None):
    if os.getenv(ENV) != CAMPAIGN:
        return "disabled"
    now = m.timestamp(time.time() if now is None else now)
    # No recipient can come from a request body, callback or database row.
    try:
        m.owner(settings, settings.admin_telegram_id)
    except m.ReviewError:
        log_result("blocked", "owner_configuration_unavailable")
        return "blocked"

    with m.mutation(engine, settings) as db:
        existing = db.get(SourceProbe, PROBE_ID)
        # The previous version stopped before any network operation when the
        # owner had no open client order. A pure preview now covers that case.
        unattempted_owner_block = bool(existing and existing.status == "blocked"
            and existing.requests == 0 and isinstance(existing.result, dict)
            and existing.result.get("marker") == MARKER
            and existing.result.get("campaign") == CAMPAIGN
            and existing.result.get("code") in ("owner_request_missing", "owner_request_not_open"))
        if existing and not unattempted_owner_block:
            # A crash after the durable claim cannot authorize another send.
            if existing.status == "sending" and now-existing.checked_at >= 60:
                existing.status = "uncertain"
                existing.result = proof("interrupted_after_claim")
                log_result("uncertain", "interrupted_after_claim")
            return existing.status
        code = "ready"
        payload = None
        try:
            manual_checkout.require_sales(db)
            if not billing.control(db).enforce:
                raise m.ReviewError("access_enforcement_disabled")
            owner_request = manual_checkout.latest(db, settings.admin_telegram_id)
            if owner_request and owner_request.state in ("created", "clarification", "review"):
                payload = manual_checkout.details(engine, settings, settings.admin_telegram_id,
                                                  owner_request.id)
            else:
                profile = manual_checkout.subscription_preview.receiving_profile()
                if profile is None:
                    raise m.ReviewError("recipient_configuration_unavailable")
                payload = manual_checkout.render_requisites(settings.admin_telegram_id, profile,
                                                            code=None, state="preview")
            if payload.pop("method", None) != "sendMessage":
                raise m.ReviewError("requisites_payload_invalid")
            payload["text"] = "🔎 Перевірка реквізитів для власника\n\n" + payload["text"]
            payload["allow_paid_broadcast"] = False
        except m.ReviewError as exc:
            code = exc.code
        row = existing or SourceProbe(id=PROBE_ID)
        row.checked_at = now
        if code != "ready":
            row.status, row.requests, row.result = "blocked", 0, proof(code)
            db.add(row)
            log_result("blocked", code)
            return "blocked"
        row.status, row.requests, row.result = "sending", 1, proof("claimed")
        db.add(row)
    # The claim is committed before this one network operation.
    try:
        result = request(settings.bot_token, "sendMessage", payload, timeout=5)
    except Exception:
        result = None
    state, code = "uncertain", "transport_uncertain"
    if isinstance(result, dict):
        message = result.get("result")
        if (result.get("ok") is True and isinstance(message, dict)
                and type(message.get("message_id")) is int):
            state, code = "accepted", "accepted"
        elif (result.get("ok") is False and type(result.get("error_code")) is int
              and 400 <= result["error_code"] <= 499):
            state, code = "failed", str(result["error_code"])
    with m.mutation(engine, settings) as db:
        row = db.get(SourceProbe, PROBE_ID)
        if row and row.status == "sending":
            row.status, row.result = state, proof(code)
        else:
            return row.status if row else "uncertain"
    log_result(state, code)
    return state
