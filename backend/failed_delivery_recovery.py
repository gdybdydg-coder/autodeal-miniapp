"""One explicitly selected failed delivery for the configured owner, once only.

No direct send, new recipient, /start, epoch change, or sent/uncertain reset.
The normal worker refreshes expired source evidence and checks current filters.
"""
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Delivery, DeliveryTiming, Listing, SourceProbe, User
from .notification_diagnostic import validate_id
from .worker import matching_searches
from .delivery_diagnostic import receipt

log = logging.getLogger(__name__)


def key(source_id, uid):
    validate_id(source_id)
    return 'failed-delivery-recovery-v1-' + str(uid) + '-' + source_id


def recover_once(monitor):
    settings = monitor.settings
    source_id, uid = settings.ria_failed_delivery_recovery_id, settings.admin_telegram_id
    if not source_id or not uid or not settings.live or not settings.monitor_enabled:
        return
    claim = key(source_id, uid)
    with Session(monitor.engine) as db:
        # Same user-first lock order as /stop and ordinary delivery.
        user = db.scalar(select(User).where(User.id == uid).with_for_update())
        if not monitor.owned(db) or db.get(SourceProbe, claim):
            return
        listing = db.scalar(select(Listing).where(Listing.source == 'auto_ria', Listing.source_id == source_id))
        delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
            Delivery.listing_id == listing.id).with_for_update()) if listing else None
        timing = db.get(DeliveryTiming, delivery.id) if delivery else None
        retry = bool(user and user.ready and delivery and delivery.state == 'failed'
                     and delivery.message_id is None and (not timing or timing.accepted_at is None)
                     and db.scalar(matching_searches(uid, listing.id).limit(1)) is not None)
        if retry:
            delivery.state, delivery.retry_at = 'pending', time.time()
        db.add(SourceProbe(id=claim, requests=0, checked_at=time.time(),
            status='queued' if retry else 'not_eligible', result={'source_id': source_id,
                'previous_state': 'failed' if retry else None}))
        db.commit()


def report(engine, settings):
    source_id, uid = settings.ria_failed_delivery_recovery_id, settings.admin_telegram_id
    if not source_id or not uid:
        return
    with Session(engine) as db:
        probe = db.get(SourceProbe, key(source_id, uid))
        if not probe:
            return
        listing = db.scalar(select(Listing).where(Listing.source == 'auto_ria', Listing.source_id == source_id))
        delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
            Delivery.listing_id == listing.id)) if listing else None
        timing = db.get(DeliveryTiming, delivery.id) if delivery else None
        status = {'source_id': source_id, 'scope': 'configured_admin', 'recovery': probe.status,
                  'state': delivery.state if delivery else None,
                  'message_id': delivery.message_id if delivery else None,
                  'accepted_at': timing.accepted_at if timing else None,
                  'response': receipt(db, delivery.id) if delivery else None}
        if probe.result.get('report') == status:
            return
        probe.result = {**probe.result, 'report': status}
        db.commit()
    log.warning('Failed delivery recovery %s', json.dumps(status, sort_keys=True))
