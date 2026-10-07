"""Isolated public-detail peer pilot; no network, paid inputs or delivery hooks.

A frozen listing-ID set prevents choosing peers after seeing detail prices.
It does not prove vehicle-level deduplication or independent model quality.
"""
import math
from .public_details import parse_public_details
from .public_detail_crosscheck import crosscheck_details
from .visible_adapter import parse_visible_facts
from .valuation import estimate


def public_peer_record(html, listing_id, received_at):
    if type(received_at) not in (int,float) or not math.isfinite(received_at) or received_at<=0:
        raise ValueError('invalid_observation_time')
    d=parse_public_details(html,listing_id)
    visible=crosscheck_details(html,listing_id)
    region=parse_visible_facts(html,listing_id).region
    if d.availability!='active':
        raise ValueError('active_listing_required')
    if not visible['price_agrees']:
        raise ValueError('currency_not_crosschecked' if d.currency!='USD' else 'visible_price_missing_or_conflicting')
    # No average hint, paid quote, arbitrary annotation, VIN or seller text.
    row={k:getattr(d,k) for k in ('listing_id','brand','model','year','fuel',
                                'transmission','mileage_km','price','currency')}
    row.update(body=visible['body'],generation=visible['generation'],
               engine_cc=visible['engine_cc'],region=region,observed_at=received_at)
    return row


def run_frozen_pilot(subject, peer_records, frozen_ids, as_of):
    if not isinstance(frozen_ids,(list,tuple)) or not frozen_ids or len(frozen_ids)>200:
        raise ValueError('invalid_frozen_peer_set')
    if any(not isinstance(i,str) or not i.isdigit() for i in frozen_ids):
        raise ValueError('invalid_frozen_peer_id')
    if len(set(frozen_ids))!=len(frozen_ids) or subject['listing_id'] in frozen_ids:
        raise ValueError('subject_or_duplicate_in_frozen_set')
    by_id={}
    for row in peer_records:
        ident=row.get('listing_id')
        if ident not in frozen_ids:raise ValueError('unplanned_peer')
        if ident in by_id:raise ValueError('duplicate_peer_record')
        by_id[ident]=row
    result=estimate(subject,list(by_id.values()),as_of).as_dict()
    return {'subject_id':subject['listing_id'],'planned_peers':len(frozen_ids),
            'prepared_peers':len(by_id),'missing_peer_ids':sorted(set(frozen_ids)-set(by_id)),
            'outcome':result,'ready_for_delivery':False,'production_approved':False,
            'independent_quality_validation':False,'vehicle_deduplication_proven':False,
            'freeze_is_caller_contract':True}
