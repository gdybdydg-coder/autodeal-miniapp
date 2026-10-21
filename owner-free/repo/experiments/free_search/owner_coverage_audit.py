"""Read-only comparison evidence. Paid IDs are NEVER imported into free discovery.

An overlap of historical samples is not whole-market recall or a live latency SLA.
This module has no transport, queue writer or send authority.
"""
import math

EXCLUDED_MESSAGES=frozenset({27366,28005,28006,28057})


def audit(paid_receipts,public_observations):
    public={row['id']:row for row in public_observations}
    if len(public)!=len(public_observations):raise ValueError('ambiguous_public_identity')
    rows=[];seen=set()
    for paid in paid_receipts:
        if paid['message_id'] in EXCLUDED_MESSAGES:continue
        ident=paid['id']
        if ident in seen:raise ValueError('ambiguous_paid_identity')
        seen.add(ident)
        free=public.get(ident)
        delta=None
        if free is not None and not free['baseline'] and paid['first_seen'] is not None:
            times=(free['first_seen'],paid['first_seen'])
            if any(type(t) not in (float,int) or not math.isfinite(t) for t in times):
                raise ValueError('invalid_observation_time')
            delta=times[0]-times[1]
        rows.append({'id':ident,'paid_message_id':paid['message_id'],
            'public_sample_presence':'not_observed_in_sample' if free is None else
                                     'baseline_only' if free['baseline'] else 'observed',
            'free_minus_paid_first_observed_seconds':delta,
            'same_search_epoch_verified':False})
    matched=sum(r['public_sample_presence']!='not_observed_in_sample' for r in rows)
    return {'scope':'historical_owner_receipts_vs_bounded_public_observations',
            'paid_receipts':len(rows),'public_overlap':matched,'not_observed':len(rows)-matched,
            'whole_market_recall':None,'simultaneous_coverage_proven':False,
            'publication_latency':None,'rows':rows,'send_authorized':False,
            'caveat':'Collector was intermittent; absence is not proof the website omitted the listing.'}
