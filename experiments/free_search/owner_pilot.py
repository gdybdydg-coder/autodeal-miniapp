"""Owner-only LOCAL message preparation from public HTML, never transport.

    public HTML -> details/current USD -> asking comparison -> saved filters

No provider keys, paid cache, FX, Telegram, background worker or fallback.
Production native-lower-bound formula is NOT replaced by this preview.
"""
from dataclasses import dataclass
from decimal import Decimal
from html import escape
import math
import re

from .filter_gate import PublicEvidence, SearchFilter, Span, gate
from .public_peer_pilot import public_peer_record
from .public_detail_crosscheck import crosscheck_details
from .visible_adapter import parse_visible_facts
from .valuation import estimate
from .owner_access import revalidate


@dataclass(frozen=True)
class PublicPage:
    listing_id: str
    url: str
    html: str
    received_at: float


def _page(page, now, max_age):
    if (not isinstance(page, PublicPage)
            or type(page.received_at) not in (int, float)
            or not math.isfinite(page.received_at)
            or not 0 <= now - page.received_at <= max_age
            or not isinstance(page.listing_id, str)
            or not re.fullmatch('[1-9][0-9]{0,11}', page.listing_id)
            or not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                + page.listing_id + r'\.html', page.url)):
        raise ValueError('public_page_identity_or_freshness_invalid')
    row = public_peer_record(page.html, page.listing_id, page.received_at)
    if parse_visible_facts(page.html, page.listing_id).listing_id != page.listing_id:
        raise ValueError('visible_listing_identity_unverified')
    cross = crosscheck_details(page.html, page.listing_id)
    if cross['issues']:
        raise ValueError('public_detail_conflict')
    row['drive'] = cross.get('drive_text')
    return row


def _filters(values):
    # Backend canonical values use the same Ukrainian labels as these parsers.
    def bound(name, edge, multiplier=1):
        value = values.get(name, {}).get(edge)
        return None if value is None else Decimal(str(value)) * multiplier
    regions = values.get('region') or []
    if isinstance(regions, str):
        regions = [regions]
    return SearchFilter(
        regions=frozenset(regions),
        price_usd=Span(bound('price', 'from'), bound('price', 'to')),
        year_min=bound('year', 'from'), year_max=bound('year', 'to'),
        mileage_min_km=bound('mileage', 'from', 1000),
        mileage_max_km=bound('mileage', 'to', 1000),
        bodies=frozenset(values.get('body', [])),
        fuels=frozenset(values.get('fuel', [])),
        transmissions=frozenset(values.get('transmission', [])),
        brand=values.get('brand') or None, model=values.get('model') or None,
        min_discount_percent=Decimal(str(values.get('minDiscount', 15))))


def prepare(engine, settings, snapshot, subject, peers, frozen_peer_ids, now):
    """Recompute only from public inputs and retain every missing/error peer.

    Result is private and only for the verified owner's current saved search.
    New-publication evidence and replacement-method selection remain explicit
    release blockers, including when a local draft can be constructed.
    """
    result = {'scope': 'owner_only', 'status': 'blocked', 'draft': None,
              'ready_for_delivery': False, 'paid_calls': 0,
              'publication_at': None, 'valuation': None,
              'peer_errors': {}, 'planned_peers': len(frozen_peer_ids),
              'release_blockers': ['publication_not_verified',
                                   'replacement_valuation_method_not_selected',
                                   'live_collector_and_atomic_sender_not_integrated']}
    reason = revalidate(engine, settings, snapshot, subject.listing_id, now)
    if reason != 'eligible':
        return {**result, 'reason': reason}
    if (len(frozen_peer_ids) > 200 or len(set(frozen_peer_ids)) != len(frozen_peer_ids)
            or subject.listing_id in frozen_peer_ids):
        raise ValueError('invalid_frozen_peer_set')
    try:
        target = _page(subject, now, 300)
    except ValueError as exc:
        return {**result, 'reason': str(exc)}
    visible = parse_visible_facts(subject.html, subject.listing_id)
    evidence = PublicEvidence(
        listing_id=subject.listing_id, publication_proven=False, active=True,
        category=visible.category, price_usd=Decimal(str(target['price'])),
        region=visible.region, year=target['year'], body=target['body'],
        fuel=target['fuel'], transmission=target['transmission'],
        mileage_km=target['mileage_km'], brand=target['brand'], model=target['model'])
    filters = _filters(snapshot.filters)
    decision = gate(evidence, filters)
    # Publication is STILL a release blocker. Only a LOCAL comparison preview
    # may proceed so that an unverified first observation is never called new.
    blockers = [r for r in decision.blockers if r != 'publication_not_proven']
    if blockers:
        return {**result, 'reason': 'owner_filter_mismatch', 'filter_blockers': blockers}
    known = set()
    selected = []
    for page in peers:
        if page.listing_id in known or page.listing_id not in frozen_peer_ids:
            raise ValueError('unplanned_or_duplicate_peer')
        known.add(page.listing_id)
        try:
            peer = _page(page, now, 30 * 86400)
            # Diagnostic comparability audit is explicit, default model intact.
            for field in ('generation', 'drive'):
                if not target.get(field) or not peer.get(field):
                    raise ValueError(field + '_unknown')
                if peer[field] != target[field]:
                    raise ValueError(field + '_mismatch')
            selected.append(peer)
        except ValueError as exc:
            result['peer_errors'][page.listing_id] = str(exc)
    for ident in set(frozen_peer_ids) - known:
        result['peer_errors'][ident] = 'not_fetched'
    outcome = estimate(target, selected, now)
    result['valuation'] = outcome.as_dict()
    result['prepared_peers'] = len(selected)
    if outcome.status != 'estimated':
        return {**result, 'status': 'unknown', 'reason': outcome.reason}
    if outcome.discount_percent < filters.min_discount_percent:
        return {**result, 'status': 'below_threshold', 'reason': 'owner_discount_threshold'}
    reason = revalidate(engine, settings, snapshot, subject.listing_id, now)
    if reason != 'eligible':
        return {**result, 'reason': reason}
    text = (f"🧪 AUTO.RIA · безкоштовний дослідний пошук\n"
            f"🚘 {escape(target['brand'])} {escape(target['model'])} {target['year']}\n"
            f"Ціна оголошення: ${target['price']}\n"
            f"Орієнтир за цінами аналогів: ${outcome.reference_price:.2f}\n"
            f"Різниця: {outcome.discount_percent:.2f}% · Аналогів: {outcome.sample_count}\n"
            "Експериментальна оцінка пропозицій; стан і витрати на ремонт не підтверджені.\n"
            "Час нової публікації не підтверджений.\n" + escape(subject.url))
    return {**result, 'status': 'local_draft', 'reason': 'review_only', 'draft': text}
