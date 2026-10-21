"""Independent catalogue cohort; intersection is never mislabeled recall."""
import math
from decimal import Decimal, InvalidOperation


def freeze(cards, size=10):
    cohort, rejected = [], []
    for card in cards:
        if card.promoted or card.issues or not card.url:
            rejected.append({'id':card.listing_id, 'issues':list(card.issues),
                             'promoted':card.promoted})
        elif len(cohort) < size:
            cohort.append({'id':card.listing_id, 'url':card.url,
                           'preview_usd':None if card.preview_usd is None else str(card.preview_usd),
                           'publication_at':None})
    return {'cohort':cohort, 'excluded_cards':rejected, 'requested_n':size,
            'selected_n':len(cohort), 'selection':'first_clean_catalogue_cards_before_feed_read'}


def compare(cohort, catalogue_ids, feed_ids):
    catalogue_ids, feed_ids = set(catalogue_ids), set(feed_ids)
    def price_gate(row):
        try:
            price = Decimal(str(row.get('preview_usd')))
            if not price.is_finite() or price <= 0:
                return 'unknown'
            return 'within_budget' if 13000 <= price <= 18000 else 'outside_budget'
        except InvalidOperation:
            return 'unknown'
    rows = [{'id':row['id'], 'catalogue_reobserved':row['id'] in catalogue_ids,
             'feed_observed':row['id'] in feed_ids, 'details_status':'not_fetched',
             'owner_price_preview_gate':price_gate(row), 'all_owner_filters_verified':False,
             'valuation_status':'unknown', 'delivery_status':'not_attempted'} for row in cohort]
    return {'rows':rows, 'n':len(rows), 'overlap_n':sum(r['feed_observed'] for r in rows),
            'not_observed_in_feed_ids':[r['id'] for r in rows if not r['feed_observed']],
            'recall':None, 'proven_misses':None,
            'reason':'catalogue_and_last_hour_populations_not_proven_equivalent'}


def summary(values):
    values = list(values)
    if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('invalid_duration')
    ordered = sorted(values)
    def percentile(p):
        return ordered[max(0, math.ceil(p*len(ordered))-1)] if ordered else None
    return {'n':len(ordered), 'p50':percentile(.5), 'p95':percentile(.95),
            'max':max(ordered) if ordered else None, 'method':'nearest_rank'}
