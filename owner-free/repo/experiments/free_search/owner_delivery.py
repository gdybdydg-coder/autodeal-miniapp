"""Reviewable one-shot owner bridge; NOT imported or enabled by production.

Uses the ordinary RIA identity/unique delivery constraint, but never pending:
the ordinary worker must not turn public evidence into a paid refresh job.
No scheduler, provider client or automatic retry. SQLite tests do not prove
PostgreSQL row-lock behavior; deployment requires that separate verification.
"""
import time
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from backend import billing, paid_source_access
from backend.models import (Car, Delivery, DeliveryTiming, Filters, Listing,
                            MonitorWatch, Search, User)
from .owner_pilot import prepare, _page


def _access(db, settings, snapshot, clock):
    user = db.scalar(select(User).where(User.id == settings.admin_telegram_id).with_for_update())
    now = clock()
    if not user or not user.ready:
        return 'owner_stopped'
    if not paid_source_access.allowed(db, user.id, now):
        return 'owner_confirmed_payment_required'
    search = db.get(Search, snapshot.search_id)
    watch = db.get(MonitorWatch, snapshot.search_id)
    if (not search or not watch or search.user_id != user.id or not search.enabled
            or watch.epoch != snapshot.epoch or search.fingerprint != snapshot.fingerprint
            or Filters.model_validate(search.filters).canonical() != snapshot.filters
            or Filters.model_validate(search.filters).fingerprint() != snapshot.fingerprint):
        return 'owner_search_changed'
    return 'eligible'


def send_one(engine, settings, snapshot, subject, peers, frozen_peer_ids,
             observations, transport, *, enabled=False, clock=time.time, browser_mean=None,
             observation_max_age=300, session_all_ukraine=False):
    """Transport(uid, HTML text) returns Telegram sendMessage JSON, once only.

    All failures after the committed claim remain terminal, including crashes.
    This favors no duplicates over retries/loss recovery. Caller must explicitly
    enable after deployment review; there is no env/default activation path.
    """
    if not enabled:
        return {'state': 'disabled'}
    if type(observation_max_age) is not int or not 1<=observation_max_age<=900:
        return {'state':'blocked','reason':'invalid_observation_age_limit'}
    if not settings.live or not billing.verified_admin(settings) or not paid_source_access.strict(engine):
        return {'state': 'blocked', 'reason': 'owner_policy_required'}
    signal = observations.signal(subject.listing_id, clock(),max_age=observation_max_age)
    if signal['reason'] != 'eligible':
        return {'state': 'blocked', 'reason': signal['reason']}
    options = {} if browser_mean is None else {'browser_mean': browser_mean}
    if session_all_ukraine is True:
        options['session_all_ukraine'] = True
    draft = prepare(engine, settings, snapshot, subject, peers, frozen_peer_ids, clock(), **options)
    if draft['status'] != 'local_draft':
        return {'state': 'blocked', 'reason': draft['reason']}
    text = draft['draft'].replace('Час нової публікації не підтверджений.',
        'Нове для нашого спостереження. Перша публікація не підтверджена.')
    text += f"\nВперше помічено {int((clock()-signal['first_seen'])/60)} хв тому."
    target = _page(subject, clock(), 300)
    # A valid shared Listing, never overwrite an existing paid-path record.
    car = Car(source='auto_ria', source_id=subject.listing_id, url=subject.url,
        brand=target['brand'], model=target['model'], year=target['year'],
        region=target.get('region') or '', body=target.get('body') or '',
        fuel=target.get('fuel') or '', transmission=target.get('transmission') or '',
        mileage=target.get('mileage_km'), price=target['price'], market=None,
        comparables=0, observed_at=subject.received_at,
        pipeline={'discovery_kind': 'owner_public_first_observed',
                  'public_mean_evidence': draft['valuation']})
    try:
        with Session(engine) as db, db.begin():
            reason = _access(db, settings, snapshot, clock)
            if reason != 'eligible':
                return {'state': 'blocked', 'reason': reason}
            listing = db.scalar(select(Listing).where(Listing.source == 'auto_ria',
                                                      Listing.source_id == subject.listing_id))
            if listing is None:
                listing = Listing(source='auto_ria', source_id=subject.listing_id,
                                  car=car.model_dump(mode='json'))
                db.add(listing); db.flush()
            if db.scalar(select(Delivery.id).where(Delivery.user_id == settings.admin_telegram_id,
                                                   Delivery.listing_id == listing.id)) is not None:
                return {'state': 'blocked', 'reason': 'existing_owner_delivery'}
            claim = Delivery(user_id=settings.admin_telegram_id, listing_id=listing.id,
                             state='free_claimed', retry_at=clock())
            db.add(claim); db.flush()
            ident = claim.id
            db.add(DeliveryTiming(delivery_id=ident, queued_at=clock(),
                discovered_at=signal['first_seen'], evaluated_at=clock(), source_added_at=None))
    except IntegrityError:
        return {'state': 'blocked', 'reason': 'concurrent_identity_claim'}
    # Claim is durable BEFORE transport: rollback/crash cannot authorize replay.
    with Session(engine) as db, db.begin():
        reason = _access(db, settings, snapshot, clock)
        claim = db.scalar(select(Delivery).where(Delivery.id == ident).with_for_update())
        if claim.state != 'free_claimed':
            return {'state': 'blocked', 'reason': 'claim_changed'}
        now = clock()
        last = db.scalar(select(func.max(DeliveryTiming.send_started_at)).join(
            Delivery, Delivery.id == DeliveryTiming.delivery_id).where(
                Delivery.user_id == settings.admin_telegram_id, Delivery.id != ident))
        if reason == 'eligible' and observations.signal(subject.listing_id, now,max_age=observation_max_age)['reason'] != 'eligible':
            reason = 'stale_observation'
        if reason == 'eligible' and not 0 <= now-subject.received_at <= 300:
            reason = 'stale_subject'
        if reason == 'eligible' and browser_mean is not None:
            from .owner_public_mean import browser_quote
            try:
                fresh_quote = browser_quote(browser_mean, subject, now)
                if fresh_quote['capture_sha256'] != draft['valuation']['capture_sha256']:
                    reason = 'browser_mean_changed'
            except ValueError:
                reason = 'browser_mean_stale_or_invalid'
        if reason == 'eligible' and last is not None and now < last+1.05:
            reason = 'chat_interval'
        if reason != 'eligible':
            claim.state = 'free_cancelled'
            return {'state': claim.state, 'reason': reason}
        timing = db.get(DeliveryTiming, ident)
        timing.send_started_at = now
        try:
            response = transport(settings.admin_telegram_id, text)
        except Exception:
            response = None
        result = response.get('result') if isinstance(response, dict) else None
        if (isinstance(response, dict) and response.get('ok') is True
                and isinstance(result, dict) and type(result.get('message_id')) is int
                and result['message_id'] > 0):
            claim.state, claim.message_id = 'sent', result['message_id']
            timing.accepted_at = clock()
            timing.telegram_date = result.get('date') if type(result.get('date')) is int else None
        else:
            claim.state = 'free_uncertain'
        return {'state': claim.state, 'delivery_id': ident, 'publication_at': None,
                'signal': 'first_observed', 'paid_api_calls': 0}
