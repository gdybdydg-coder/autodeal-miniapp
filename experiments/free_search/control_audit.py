"""Join preselected controls to collector evidence, keeping failures in denominator.

Inputs are caller evidence, not an attestation of independent selection. This
module never equates missing controls to market-wide recall, or first observation
to publication. Known-ID lookups are deliberately separate from discovery.
"""
import math


def audit_controls(control_ids, discoveries, details, valuations):
    ids = list(control_ids)
    if not 1 <= len(ids) <= 200 or any(not isinstance(i, str) or not i.isdigit() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid_frozen_controls')
    allowed = set(ids)
    def index(rows):
        result = {}
        for row in rows:
            ident = row.get('listing_id')
            if ident not in allowed or ident in result:
                raise ValueError('unexpected_or_duplicate_result')
            result[ident] = row
        return result
    ds, vs = index(details), index(valuations)
    if any(r.get('status') not in {'prepared', 'error', 'not_attempted'} for r in ds.values()):
        raise ValueError('invalid_detail_status')
    if any(r.get('status') not in {'estimated', 'unknown', 'not_attempted'} for r in vs.values()):
        raise ValueError('invalid_valuation_status')
    found = set()
    for row in discoveries:
        if row.get('method') == 'collector_scan':
            if not isinstance(row.get('listing_id'), str):
                raise ValueError('invalid_discovery_id')
            stamp = row.get('observed_at')
            if isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or not math.isfinite(stamp) or stamp <= 0:
                raise ValueError('invalid_discovery_time')
            found.add(row['listing_id'])
    rows = []
    for ident in ids:
        d, v = ds.get(ident, {}), vs.get(ident, {})
        rows.append({'listing_id': ident, 'discovered': ident in found,
                     'detail_status': d.get('status', 'not_attempted'),
                     'detail_reason': d.get('reason'),
                     'valuation_status': v.get('status', 'not_attempted'),
                     'valuation_reason': v.get('reason'),
                     'publication_at': None, 'ready_for_delivery': False})
    return {'control_count': len(ids), 'discovered_count': sum(r['discovered'] for r in rows),
            'missing_ids': [r['listing_id'] for r in rows if not r['discovered']],
            'detail_ready_count': sum(r['detail_status'] == 'prepared' for r in rows),
            'estimated_count': sum(r['valuation_status'] == 'estimated' for r in rows),
            'unknown_count': sum(r['valuation_status'] == 'unknown' for r in rows),
            'valuation_not_attempted_count': sum(r['valuation_status'] == 'not_attempted' for r in rows),
            'rows': rows, 'whole_market_recall': None, 'publication_to_ready_seconds': None,
            'independent_selection_verified_by_module': False, 'production_approved': False}
