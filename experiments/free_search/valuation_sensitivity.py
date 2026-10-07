"""Offline deletion sensitivity. Robustness is not independent price accuracy."""
from decimal import Decimal
from .valuation import estimate


def audit_sensitivity(subject, peers, as_of, thresholds=(5, 10, 15)):
    peers = list(peers)
    ids = [str(p.get('listing_id', '')) for p in peers]
    if not peers or len(peers) > 200 or any(not i.isdigit() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('unique_bounded_peer_ids_required')
    cuts = [Decimal(str(x)) for x in thresholds]
    if any(not x.is_finite() or not 0 <= x <= 100 for x in cuts):
        raise ValueError('invalid_threshold')

    def result(rows):
        out = estimate(subject, rows, as_of).as_dict()
        discount = out['discount_percent']
        out['diagnostic_thresholds'] = {
            str(c): None if discount is None else Decimal(discount) >= c for c in cuts}
        return out

    baseline = result(peers)
    singles = [{'removed_id': i, 'outcome': result([p for p in peers if str(p['listing_id']) != i])} for i in ids]
    # Unknown region is retained as its own explicit deletion scenario.
    regions = sorted({p.get('region') or '' for p in peers})
    groups = [{'removed_region': r or None,
               'removed_count': sum((p.get('region') or '') == r for p in peers),
               'outcome': result([p for p in peers if (p.get('region') or '') != r])} for r in regions]

    def summary(runs):
        values = [Decimal(r['outcome']['reference_price']) for r in runs if r['outcome']['reference_price'] is not None]
        return {'scenarios': len(runs), 'unknown': sum(r['outcome']['status'] == 'unknown' for r in runs),
                'reference_min': str(min(values)) if values else None,
                'reference_max': str(max(values)) if values else None,
                'threshold_changes': {str(c): sum(r['outcome']['diagnostic_thresholds'][str(c)] != baseline['diagnostic_thresholds'][str(c)] for r in runs) for c in cuts}}
    return {'baseline': baseline, 'leave_one_listing_out': singles, 'leave_one_region_out': groups,
            'listing_summary': summary(singles), 'region_summary': summary(groups),
            'independent_accuracy_validation': False, 'production_approved': False,
            'ready_for_delivery': False, 'thresholds_are_diagnostics_not_client_filters': True}
