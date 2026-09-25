"""Explicit owner-only media edit of selected existing text cards, once each."""
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ria_market_range
from .auto_ria import RiaError
from .delivery_diagnostic import receipt
from .models import Car, Delivery, DeliveryTiming, Listing, SourceProbe, User
from .notification_diagnostic import validate_id
from .worker import TelegramSender, matching_searches

log = logging.getLogger(__name__)


def ids(value):
    if not value:
        return []
    result = value.split(',')
    if len(result) > 5 or len(set(result)) != len(result):
        raise ValueError('Select at most five distinct photo repair listings')
    for item in result:
        validate_id(item)
        if not item:
            raise ValueError('Invalid photo repair listing ID')
    return result


def retryable(probe):
    # photo_unavailable is returned before editMessageMedia is called.
    # Allow one later preflight retry; never retry an issued/ambiguous edit.
    return bool(probe and probe.status == 'photo_unavailable'
                and probe.result.get('attempts', 1) < 2
                and time.time() - probe.checked_at >= 60)


def legacy_preparation_failure(probe, car, timing):
    """Old formatter necessarily raised before HTTP on this expired snapshot.

    New claims are tagged prepared and can never enter this compatibility path.
    Issued/uncertain edits and every Delivery claim remain untouched.
    """
    proof = car.valuation_evidence or {}
    quote = proof.get('source_range') or {}
    return bool(probe and probe.status == 'editing'
        and set(probe.result) == {'source_id', 'message_id', 'attempts'}
        and probe.result.get('attempts') == 2 and timing and timing.accepted_at
        and proof.get('version') == ria_market_range.VERSION
        and (car.pipeline or {}).get('evaluated_at') == timing.evaluated_at
        and isinstance(quote.get('observed_at'), (int, float))
        and probe.checked_at - quote['observed_at'] > ria_market_range.MAX_AGE
        and ria_market_range.evidence_valid(car, timing.accepted_at))


def run_once(monitor, sender=None):
    settings, uid = monitor.settings, monitor.settings.admin_telegram_id
    if not uid or not settings.live or not settings.monitor_enabled:
        return
    for source_id in ids(settings.ria_photo_repair_ids):
        key = 'owner-photo-repair-v1-' + str(uid) + '-' + source_id
        with Session(monitor.engine) as db:
            previous = db.get(SourceProbe, key)
            if previous and not retryable(previous) and previous.status != 'editing':
                continue
            user = db.get(User, uid)
            listing = db.scalar(select(Listing).where(Listing.source == 'auto_ria', Listing.source_id == source_id))
            delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
                Delivery.listing_id == listing.id)) if listing else None
            timing = db.get(DeliveryTiming, delivery.id) if delivery else None
            if not (user and user.ready and delivery and delivery.state == 'sent' and delivery.message_id
                    and timing and timing.accepted_at and time.time() - timing.accepted_at < 48 * 3600
                    and db.scalar(matching_searches(uid, listing.id).limit(1)) is not None):
                continue
            car = Car.model_validate(listing.car)
            legacy = legacy_preparation_failure(previous, car, timing)
            if previous and not retryable(previous) and not legacy:
                continue
            # Only reconstruct the exact snapshot used by the original receipt.
            if (car.pipeline or {}).get('evaluated_at') != timing.evaluated_at:
                continue
            historical_at = timing.accepted_at
            had_photo = bool(car.photo)
            attempts = (receipt(db, delivery.id) or {}).get('attempts', [])
            known_text = bool(attempts and attempts[-1].get('method') == 'sendMessage'
                              and attempts[-1].get('accepted') is True)
            if had_photo and not known_text:
                # A sent photo may simply be above the visible screenshot.
                # Never replace a successfully sent card without evidence.
                db.add(SourceProbe(id=key, requests=0, checked_at=time.time(),
                    status='text_not_confirmed', result={'source_id': source_id}))
                db.commit()
                log.warning('Owner photo repair source_id=%s state=text_not_confirmed', source_id)
                continue
        refreshed_photo, requests_used = None, 0
        # A missing optional photo must not have hidden the original alert.
        # Fetch it once through the normal accounted provider client now.
        if not car.photo:
            source = monitor.search_factory(monitor.engine, settings.auto_ria_api_key)
            try:
                source.acquire()
            except RiaError:
                return  # Another source batch owns the lease; try a later tick.
            try:
                source.request_limit = 1
                refreshed_photo = source.car(source_id, force=True).get('image')
            except RiaError:
                pass
            finally:
                requests_used = source.requests_made
                source.release()
            if refreshed_photo:
                car = car.model_copy(update={'photo': refreshed_photo})
        # Validate the original receipt snapshot BEFORE committing an edit claim.
        TelegramSender.card(car, historical_at=historical_at)
        with Session(monitor.engine) as db:
            user = db.scalar(select(User).where(User.id == uid).with_for_update())
            previous = db.get(SourceProbe, key)
            if not monitor.owned(db) or (previous and not retryable(previous)
                    and not legacy_preparation_failure(previous, car, timing)):
                continue
            delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
                Delivery.listing_id == listing.id).with_for_update())
            if not (user and user.ready and delivery and delivery.state == 'sent' and delivery.message_id
                    and db.scalar(matching_searches(uid, listing.id).limit(1)) is not None):
                continue
            message_id = delivery.message_id
            attempts = previous.result.get('attempts', 1) + 1 if previous else 1
            if previous is None:
                previous = SourceProbe(id=key, requests=0)
                db.add(previous)
            previous.requests += requests_used
            previous.checked_at, previous.status = time.time(), 'editing'
            previous.result = {'source_id': source_id, 'message_id': message_id, 'attempts': attempts, 'prepared': True}
            db.commit()  # Never repeat an ambiguous edit after restart.
        with Session(monitor.engine) as db:
            # Respect /stop immediately before the network edit, same lock as delivery.
            user = db.scalar(select(User).where(User.id == uid).with_for_update())
            delivery = db.scalar(select(Delivery).where(Delivery.user_id == uid,
                Delivery.listing_id == listing.id).with_for_update())
            if not (user and user.ready and delivery and delivery.state == 'sent'
                    and delivery.message_id == message_id
                    and db.scalar(matching_searches(uid, listing.id).limit(1)) is not None):
                result = {'cancelled': True}
            else:
                result = (sender or TelegramSender(settings.bot_token)).add_photo(uid, message_id, car, historical_at=historical_at)
            message = result.get('result')
            confirmed = bool(result.get('ok') is True and isinstance(message, dict)
                             and message.get('message_id') == message_id and message.get('photo'))
            state = ('edited' if confirmed else 'cancelled' if result.get('cancelled') else
                     'photo_unavailable' if result.get('photo_unavailable') else
                     'rejected' if result.get('ok') is False else 'uncertain')
            report = {'scope': 'configured_admin', 'source_id': source_id, 'message_id': message_id,
                      'state': state, 'photo_confirmed': confirmed, 'stored_photo': had_photo,
                      'refreshed_photo': bool(refreshed_photo), 'attempts': attempts}
            probe = db.get(SourceProbe, key)
            probe.status, probe.result, probe.checked_at = state, report, time.time()
            db.commit()
        log.warning('Owner photo repair %s', json.dumps(report, sort_keys=True))
