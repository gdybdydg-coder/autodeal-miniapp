"""Offline cohort coverage with frozen, disjoint target and peer manifests.

Selection independence is a caller contract. Asking-price references are not
sale-price labels, and this module cannot attest physical vehicle uniqueness.
"""
from collections import Counter
import math
from .valuation import estimate

FIELDS = frozenset({'listing_id','brand','model','year','generation','fuel',
                    'transmission','body','engine_cc','mileage_km','region',
                    'price','currency','observed_at','price_observed_at',
                    'duplicate_key','price_kind','whole_vehicle','price_context','title',
                    'engine_liters','publication_at','published_at','damaged','repair_parts'})


def _ids(values):
    values=list(values)
    if not 1 <= len(values) <= 200 or any(not isinstance(i,str) or not i.isascii() or not i.isdigit() for i in values) or len(set(values)) != len(values):
        raise ValueError('invalid_frozen_ids')
    return values


def _records(rows, ids):
    out={}
    for row in rows:
        ident=row.get('listing_id')
        if ident not in ids or ident in out:
            raise ValueError('unexpected_or_duplicate_record')
        out[ident]={k:v for k,v in row.items() if k in FIELDS}
    return out


def evaluate_public_holdout(target_ids, targets, peer_ids, peers, as_of):
    if isinstance(as_of,bool) or not isinstance(as_of,(int,float)) or not math.isfinite(as_of) or as_of<=0:
        raise ValueError('invalid_as_of')
    target_ids,peer_ids=_ids(target_ids),_ids(peer_ids)
    if set(target_ids)&set(peer_ids):raise ValueError('holdout_peer_leakage')
    targets=_records(targets,set(target_ids));peers=_records(peers,set(peer_ids))
    # Cross-target physical identity is excluded whenever caller has evidence.
    def group(row):
        value=row.get('duplicate_key')
        return ' '.join(value.casefold().split()) if isinstance(value,str) and value.strip() and len(value)<=200 else None
    target_groups={group(r) for r in targets.values() if group(r) is not None}
    usable=[r for r in peers.values() if group(r) is None or group(r) not in target_groups]
    rows=[]
    for ident in target_ids:
        if ident not in targets:
            rows.append({'listing_id':ident,'status':'not_evaluated','reason':'target_details_unavailable','outcome':None})
            continue
        outcome=estimate(targets[ident],usable,as_of).as_dict()
        rows.append({'listing_id':ident,'status':outcome['status'],'reason':outcome['reason'],'outcome':outcome})
    return {'planned_targets':len(target_ids),'prepared_targets':len(targets),
            'planned_peers':len(peer_ids),'prepared_peers':len(peers),
            'missing_peer_ids':[i for i in peer_ids if i not in peers],
            'known_cross_target_duplicates_removed':len(peers)-len(usable),
            'status_counts':dict(Counter(r['status'] for r in rows)),
            'reason_counts':dict(Counter(r['reason'] for r in rows)),
            'rows':rows,'as_of':as_of,'model_policy_changed':False,
            'selection_independence_attested':False,'physical_identity_proven':False,
            'market_accuracy_validated':False,'ready_for_delivery':False,'production_approved':False}
