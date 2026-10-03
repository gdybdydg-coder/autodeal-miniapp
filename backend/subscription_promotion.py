"""One persisted, fail-closed rule for first-purchase subscription advertising.

Use at audience construction and again in the transaction immediately before
each send claim, including retries. This rule does not govern vehicle delivery,
support replies, payment acknowledgements, or owner-only previews.
"""
import json
import logging
import time

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import billing
from .billing_models import AccessEvent, BillingCampaign, BillingOrder, CampaignRecipient, Entitlement, MarketingConsent
from .manual_payment_models import BankCredit, PaymentAudit, PaymentRequest
from .models import StarsTestOrder, User

CAMPAIGNS = ("autodeal-launch-20261002-0900-kyiv", "manual-card-launch-20261002-v1",
             "manual-card-reminder-20261002-v1")
PAID_STATES = ("paid", "refunded", "refund_sending", "refund_uncertain")
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())


class EligibilityUnavailable(Exception):
    """No reliable decision: leave the queued advertisement untouched/deferred."""


def blocked_clause(uid, now):
    """Shared authoritative history/checkout exclusions, usable in a bulk query."""
    from .manual_payment_models import ReceiptExpectation
    return or_(
        exists().where(CampaignRecipient.user_id == uid, CampaignRecipient.state == "failed",
                       CampaignRecipient.error == "403"),
        exists().where(Entitlement.user_id == uid, Entitlement.expires_at > 0),
        exists().where(AccessEvent.user_id == uid, or_(AccessEvent.expires_at > now,
                       AccessEvent.kind.in_(("paid", "manual_paid")))),
        exists().where(BillingOrder.user_id == uid, or_(BillingOrder.state.in_((*PAID_STATES, "pending")),
                       BillingOrder.charge_id.is_not(None))),
        exists().where(StarsTestOrder.user_id == uid, or_(StarsTestOrder.state.in_(PAID_STATES),
                       StarsTestOrder.charge_id.is_not(None))),
        exists().where(BankCredit.user_id == uid),
        exists().where(PaymentRequest.user_id == uid, or_(
            PaymentRequest.state.not_in(("created", "rejected")), PaymentRequest.expires_at > 0)),
        exists().where(PaymentAudit.request_id == PaymentRequest.id,
                       PaymentRequest.user_id == uid, PaymentAudit.action == "approved"),
        exists().where(ReceiptExpectation.user_id == uid, ReceiptExpectation.active.is_(True)),
        )


def eligible(db, uid, now=None, *, require_consent=False):
    """Return an authoritative decision; database failures never mean unpaid.

    Queries read scalar values afresh, rather than trusting an ORM identity-map
    copy of a user/subscription loaded at audience-construction time. Any past
    persisted entitlement is conservatively treated as existing access history;
    a first-purchase campaign must not guess it was never a paid grant.
    """
    now = time.time() if now is None else now
    try:
        user_ready = db.scalar(select(User.ready).where(User.id == uid))
        if user_ready is not True:
            return False
        consent = db.execute(select(MarketingConsent.allowed, MarketingConsent.blocked)
                             .where(MarketingConsent.user_id == uid)).one_or_none()
        if consent is None and require_consent:
            return False
        if consent is not None and (not consent.allowed or consent.blocked):
            return False
        blocked = db.scalar(select(blocked_clause(uid, now)))
        return not blocked
    except SQLAlchemyError:
        # Do not log SQL, connection strings, bound identifiers, or treat an
        # unavailable/missing table as evidence that the recipient has not paid.
        raise EligibilityUnavailable("subscription_promotion_state_unavailable") from None


def _restore_unattempted_claim(db, item, prior_state, prior_attempted_at, now):
    # This helper is reachable only when the transport has definitely not begun.
    item.state, item.attempted_at = prior_state, prior_attempted_at
    item.retry_at = max(item.retry_at, now+30)
    item.error = "eligibility_temporarily_unavailable"


def _defer_claim(engine, campaign_id, uid, now, prior_state, prior_attempted_at):
    try:
        with Session(engine) as db, db.begin():
            billing.control(db, lock=True)
            item = db.get(CampaignRecipient, (campaign_id, uid), with_for_update=True)
            if item and item.state == "sending" and item.attempted_at == now:
                _restore_unattempted_claim(db, item, prior_state, prior_attempted_at, now)
    except SQLAlchemyError:
        # If storage itself is unavailable, retain the durable send claim. Its
        # existing recovery path quarantines it; never guess and replay it.
        pass
    return "deferred"


def dispatch_claim(engine, settings, campaign_id, uid, request, payload, now, *,
                   prior_state, prior_attempted_at, require_consent=False,
                   eligibility_check=None, dispatch_guard=None, clock=None, result_callback=None):
    """Serialize the final eligibility check + bounded send against access grants.

    A payment may commit after the original durable claim. Reacquire the SAME
    BillingControl lock used by grants/owner confirmations, read the state again,
    and hold the lock through transport and outcome persistence. Once transport
    begins, any uncertainty stays quarantined and is never automatically replayed.
    """
    if prior_state not in ("pending", "retry") or not callable(request):
        raise ValueError("Expected an explicit sender and original queued state")
    transport_started = False
    try:
        with Session(engine) as db, db.begin():
            ctrl = billing.control(db, lock=True)
            item = db.get(CampaignRecipient, (campaign_id, uid), with_for_update=True)
            if item is None or item.state != "sending" or item.attempted_at != now:
                return "uncertain"
            campaign = db.get(BillingCampaign, campaign_id)
            if not ctrl or not ctrl.sales or not ctrl.enforce or not campaign or campaign.status != "running":
                _restore_unattempted_claim(db, item, prior_state, prior_attempted_at, now)
                return "deferred"
            current = clock() if clock is not None else now
            if dispatch_guard is not None and not dispatch_guard(db, settings, campaign, current):
                _restore_unattempted_claim(db, item, prior_state, prior_attempted_at, current)
                return "deferred"
            permitted = (eligibility_check(db, settings, uid, current) if eligibility_check is not None
                         else eligible(db, uid, current, require_consent=require_consent))
            if not permitted:
                item.state, item.error = "excluded", "recipient_no_longer_eligible"
                return "excluded"
            if clock is not None:
                current = clock()
                if not campaign.not_before <= current < campaign.deadline:
                    _restore_unattempted_claim(db, item, prior_state, prior_attempted_at, current)
                    return "deferred"
            transport_started = True
            try:
                result = request(settings.bot_token, "sendMessage", payload, timeout=5)
            except Exception:
                result = {}
            if not isinstance(result, dict):
                result = {}
            from .billing_campaign import apply_send_result
            completed_at = clock() if clock is not None else now
            apply_send_result(item, result, completed_at)
            if result_callback is not None:
                result_callback(db, campaign, item, completed_at)
            if result.get("error_code") == 403:
                consent = db.get(MarketingConsent, uid)
                if consent:
                    consent.blocked = True
            return item.state
    except (EligibilityUnavailable, SQLAlchemyError):
        if transport_started:
            return "uncertain"
        return _defer_claim(engine, campaign_id, uid, now, prior_state, prior_attempted_at)


def audit_queue(engine, settings=None):
    """Read-only startup evidence: aggregate queue and current audience counts."""
    now = time.time()
    result = {"status": "verified", "campaign_states": {}, "queued_states": {},
              "queued_eligible": 0, "queued_excluded": 0,
              "ready_users": 0, "first_purchase_eligible": 0, "first_purchase_excluded": 0}
    try:
        with Session(engine) as db:
            result["campaign_states"] = dict(db.execute(select(BillingCampaign.status, func.count())
                .where(BillingCampaign.id.in_(CAMPAIGNS)).group_by(BillingCampaign.status)).all())
            queued = db.execute(select(CampaignRecipient.campaign_id, CampaignRecipient.user_id,
                CampaignRecipient.state).where(CampaignRecipient.campaign_id.in_(CAMPAIGNS),
                CampaignRecipient.state.in_(("pending", "retry")))).all()
            for campaign, uid, state in queued:
                result["queued_states"][state] = result["queued_states"].get(state, 0)+1
                permitted = eligible(db, uid, now, require_consent=campaign == CAMPAIGNS[0])
                result["queued_eligible" if permitted else "queued_excluded"] += 1
            for uid in db.scalars(select(User.id).where(User.ready.is_(True))):
                result["ready_users"] += 1
                result["first_purchase_eligible" if eligible(db, uid, now)
                       else "first_purchase_excluded"] += 1
    except (EligibilityUnavailable, SQLAlchemyError):
        result = {"status": "deferred", "reason": "subscription_promotion_state_unavailable"}
    LOG.info("Subscription promotion audit %s", json.dumps(result, sort_keys=True))
    return result
