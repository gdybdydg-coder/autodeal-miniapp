"""Reuse compatible public observations across catalogues, without price ranking."""
from .valuation import _mismatch, estimate
from .owner_valuation_policy import OWNER_VALUATION_POLICY


def pool(subject, cache, now):
    snapshot = cache.snapshot(now, OWNER_VALUATION_POLICY.max_peer_age_seconds)
    rows, rejected = [], dict(snapshot['unavailable'])
    subject_check = estimate(subject, [], now, OWNER_VALUATION_POLICY)
    if subject_check.reason.startswith('subject_'):
        return {'rows': [], 'rejected': rejected, 'subject_error':subject_check.reason,
                'selection':'subject_requires_valid_fresh_evidence',
                'estimate':subject_check.as_dict(), 'paid_api_calls':0,
                'ready_for_delivery':False}
    for row in snapshot['rows']:
        ident = row['listing_id']
        if ident == subject['listing_id']:
            continue
        reason = None
        # Unknown drivetrain/generation must not manufacture compatible peers.
        for field in ('generation', 'drive'):
            if not subject.get(field) or not row.get(field):
                reason = field+'_unknown'; break
            if subject[field] != row[field]:
                reason = field+'_mismatch'; break
        reason = reason or _mismatch(subject, row, OWNER_VALUATION_POLICY)
        if reason:
            rejected[ident] = reason
        else:
            rows.append(row)
    return {'rows': rows, 'rejected': rejected,
            'selection': 'all_fresh_compatible_cached_ids_no_price_ranking',
            'estimate': estimate(subject, rows, now, OWNER_VALUATION_POLICY).as_dict(),
            'paid_api_calls': 0, 'ready_for_delivery': False}
