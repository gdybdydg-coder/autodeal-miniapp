"""Bounded discovery audit that cannot label unmatched scopes as recall.

Selection independence and declared scopes remain caller evidence. A matched
query is not proof of source-wide completeness or first publication semantics.
"""
import math
from urllib.parse import parse_qsl

PAGINATION = frozenset({'page', 'limit', 'countpage'})


def explicit_scope(query):
    if not isinstance(query, str) or not query or len(query)>4000:
        raise ValueError('invalid_scope_query')
    try:
        pairs=parse_qsl(query,keep_blank_values=True,strict_parsing=True,max_num_fields=80)
    except ValueError:
        raise ValueError('invalid_scope_query') from None
    if len(dict(pairs))!=len(pairs):
        raise ValueError('duplicate_scope_key')
    scope={k:v for k,v in sorted(pairs) if k not in PAGINATION}
    if not scope or any(not k or not v for k,v in scope.items()):
        raise ValueError('scope_semantics_missing')
    return scope


def scoped_controls(control_ids, observations, *, control_query, collector_queries,
                    frozen_at, collector_started_at, independent_selection,
                    control_source, collector_source, expected_pages, successful_pages):
    ids=list(control_ids);observations=list(observations)
    if not 1<=len(ids)<=200 or len(set(ids))!=len(ids) or any(not isinstance(i,str) or not i.isascii() or not i.isdigit() for i in ids):
        raise ValueError('invalid_controls')
    def stamp(t):return type(t) in (int,float) and math.isfinite(t) and t>0
    if not stamp(frozen_at) or not stamp(collector_started_at):raise ValueError('invalid_freeze_time')
    if type(independent_selection) is not bool:raise ValueError('invalid_independence_flag')
    if not isinstance(control_source,str) or not control_source or not isinstance(collector_source,str) or not collector_source:
        raise ValueError('missing_source_identity')
    pages=list(expected_pages);success=list(successful_pages)
    if not pages or len(set(pages))!=len(pages) or len(set(success))!=len(success) or any(type(p) is not int or p<0 for p in pages+success) or not set(success)<=set(pages):
        raise ValueError('invalid_page_manifest')
    scopes=[explicit_scope(q) for q in collector_queries]
    if len(scopes)!=len(success):raise ValueError('scope_receipt_count_mismatch')
    control=explicit_scope(control_query) if control_query is not None else None
    matches=bool(control is not None and scopes and all(s==control for s in scopes))
    reasons=[]
    if not matches:reasons.append('scope_not_equivalent')
    if not independent_selection or control_source==collector_source:reasons.append('independent_selection_not_attested')
    if frozen_at>=collector_started_at:reasons.append('controls_not_frozen_before_collector')
    if set(pages)!=set(success):reasons.append('collector_cycle_incomplete')
    found={}
    for o in observations:
        if o.get('method')!='collector_scan':continue
        i=o.get('listing_id');at=o.get('observed_at')
        if not isinstance(i,str) or not i.isascii() or not i.isdigit() or not stamp(at) or at<collector_started_at:
            raise ValueError('invalid_collector_observation')
        if i in ids:found[i]=min(found.get(i,at),at)
    rows=[{'listing_id':i,'observed_in_collector':i in found,'collector_detected_at':found.get(i)} for i in ids]
    return {'controls':len(ids),'observed_controls':len(found),'rows':rows,
            'not_observed_control_ids':[i for i in ids if i not in found],
            'scope_equivalent':matches,'scope_comparison_basis':'explicit_parameters_only_not_independent_semantics_proof','control_scope':control,'collector_scopes':scopes,
            'comparison_blockers':reasons,
            'bounded_control_detection_fraction':len(found)/len(ids) if not reasons else None,
            'whole_market_recall':None,'publication_latency_seconds':None,
            'independence_verified_by_module':False,'production_approved':False}
