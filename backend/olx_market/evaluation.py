"""Frozen holdout and leave-one-out diagnostics for ASKING prices only.

No sale-price labels are manufactured. A finite set of observed adverts cannot
establish market accuracy, physical condition or first-publication semantics.
"""
from decimal import Decimal,localcontext
import hashlib
from .valuation import estimate,screened,METHODS

SPLIT_SEED='olx-stage2323-v1'
STABILITY_LIMIT_PERCENT=Decimal('5')


def partition_id(source_id):
    digest=hashlib.sha256((SPLIT_SEED+':'+str(source_id)).encode()).hexdigest()
    return 'holdout' if int(digest,16)%5==0 else 'reference'


def stability(target,reference,quote,now,*,minimum=8):
    base=estimate(target,reference,quote,now,minimum=minimum)
    out={'assessment':base,'status':'insufficient','minimum_full_sample':minimum,
         'max_allowed_leave_one_shift_percent':str(STABILITY_LIMIT_PERCENT),
         'leave_one_out':[],'methods':{},'physical_independence_verified':False}
    if base['status']!='experimental_asking_estimate':return out
    used={(c['source'],c['id']) for c in base['used_comparables']}
    # n-1 is ONLY a sensitivity diagnostic; it cannot authorize n-1 delivery.
    for source,id in sorted(used):
        reduced=[c for c in reference if (c.get('source'),c.get('id'))!=(source,id)]
        a=estimate(target,reduced,quote,now,minimum=max(3,minimum-1))
        out['leave_one_out'].append({'omitted_source':source,'omitted_id':id,'status':a['status'],
                                    'methods':a['methods'],'reasons':a['reasons']})
    with localcontext() as ctx:
        ctx.prec=50
        for method in METHODS:
            original=Decimal(base['methods'][method]['reference_usd'])
            refs=[Decimal(r['methods'][method]['reference_usd']) for r in out['leave_one_out'] if method in r['methods']]
            maximum=max((abs(v-original)/original*100 for v in refs),default=None)
            complete=len(refs)==len(used)
            out['methods'][method]={'original_reference_usd':str(original),'successful_omissions':len(refs),
                'max_shift_percent':str(maximum) if maximum is not None else None,
                'leave_one_reference_range':{'low':str(min(refs)),'high':str(max(refs))} if refs else None,
                'stable_under_omission':complete and maximum is not None and maximum<=STABILITY_LIMIT_PERCENT}
    out['status']='stable_asking_sample' if all(v['stable_under_omission'] for v in out['methods'].values()) else 'unstable_asking_sample'
    # Distinct source IDs are not proof of distinct physical vehicles.
    out['physical_independence_verified']=base['independent_vehicles_verified'] is not None
    return out


def evaluate_holdout(cars,split,quote,now,*,minimum=8,split_provenance=None):
    """Keys may be (source, id), 'source:id', or unambiguous legacy bare IDs.

    A shared numeric ID is not a shared listing across marketplaces. Duplicate
    legacy/namespaced aliases and ambiguous legacy IDs fail closed rather than
    inflating loaded/frozen counts or moving a reference into the controls.
    """
    if any(v not in ('reference','holdout') for v in split.values()):raise ValueError('Explicit frozen split required')
    qualified={};legacy=set()
    for k in split:
        identity=None
        if isinstance(k,tuple):
            if len(k)!=2 or not all(isinstance(v,str) for v in k):raise ValueError('Explicit source and ID membership required')
            identity=k
        elif isinstance(k,str):
            source,sep,id=k.partition(':')
            if sep and source in ('olx','auto_ria'):
                if not id:raise ValueError('Explicit source and ID membership required')
                identity=(source,id)
            else:legacy.add(k)
        else:raise ValueError('Explicit source and ID membership required')
        if identity:
            if identity in qualified:raise ValueError('Ambiguous duplicate frozen membership aliases')
            qualified[identity]=k
    if any(id in legacy for source,id in qualified):raise ValueError('Ambiguous duplicate frozen membership aliases')
    sources={}
    for c in cars:sources.setdefault(c['id'],set()).add(c.get('source'))
    if any(len(sources.get(k,set()))>1 for k in legacy):
        raise ValueError('Ambiguous legacy source ID membership')
    known={};roles={}
    for c in cars:
        key=(c.get('source'),c['id'])
        if key in known:raise ValueError('Resolve duplicate versions before evaluation')
        membership_key=qualified.get(key,c['id'])
        if membership_key not in split:raise ValueError('Every observation needs frozen membership')
        roles[key]=split[membership_key]
        known[key]=c
    reference=[c for key,c in known.items() if roles[key]=='reference']
    control=[c for key,c in known.items() if roles[key]=='holdout']
    # A later discovered known crosspost cannot leak between split partitions.
    control_keys={c['vehicle_key'] for c in control if c.get('vehicle_key')}
    reference=[c for c in reference if not c.get('vehicle_key') or c['vehicle_key'] not in control_keys]
    rows=[];errors={m:[] for m in METHODS};eligible=0
    for c in control:
        price,reasons=screened(c,quote,now)
        if not reasons:eligible+=1
        review=stability(c,reference,quote,now,minimum=minimum)
        benchmark=Decimal(price['usd_amount']) if price['status']=='ready' else None
        error={}
        with localcontext() as ctx:
            ctx.prec=50
            for m,v in review['assessment']['methods'].items():
                delta=abs(Decimal(v['reference_usd'])-benchmark)
                error[m]={'absolute_usd':str(delta),'absolute_percent':str(delta/benchmark*100)}
                errors[m].append((delta,delta/benchmark*100))
        comparable_ids=[v['id'] for v in review['assessment']['used_comparables']]
        rows.append({'target_source':c.get('source'),'target_id':c['id'],'benchmark_kind':'withheld_asking_price_not_sale_or_fair_value',
                     'benchmark_usd':str(benchmark) if benchmark is not None else None,
                     'eligible':not reasons,'error_against_asking':error,
                     'comparison_group_ids':comparable_ids,
                     'comparison_group_keys':[{'source':v['source'],'id':v['id']} for v in review['assessment']['used_comparables']],
                     'same_comparable_group_for_all_methods':(
                         set(error)==set(review['assessment']['methods']) if error else None),
                     'review':review})
    estimated=sum(bool(r['error_against_asking']) for r in rows)
    metrics={}
    with localcontext() as ctx:
        ctx.prec=50
        for m,values in errors.items():
            metrics[m]={'evaluated':len(values),'mean_absolute_usd':str(sum(v[0] for v in values)/len(values)) if values else None,
                        'mean_absolute_percent':str(sum(v[1] for v in values)/len(values)) if values else None}
    provenance=split_provenance or [{'seed':SPLIT_SEED,'basis':'legacy_frozen_partition'}]
    seeds={p.get('seed') for p in provenance}
    frozen=sum(v=='holdout' for v in split.values())
    loaded=len(control)
    missing=frozen-loaded
    known_ineligible=loaded-eligible
    unestimated_eligible=eligible-estimated
    def ratio(n,d):
        if not d:return None
        with localcontext() as ctx:
            ctx.prec=50
            return str(Decimal(n)/d)
    denominators={'frozen_holdout_ads':frozen,'loaded_holdout_ads':loaded,
        'eligible_loaded_ads':eligible,'estimated_loaded_ads':estimated,
        'missing_frozen_ads':missing,'known_ineligible_loaded_ads':known_ineligible,
        'unestimated_eligible_loaded_ads':unestimated_eligible}
    return {'split_seed':next(iter(seeds)) if len(seeds)==1 else None,
            'split_provenance':provenance,'benchmark_kind':'withheld_asking_price_only',
            'frozen_holdout_count':frozen,'loaded_holdout_count':loaded,
            'reference_count':len(reference),'eligible_holdout_count':eligible,'estimated_holdout_count':estimated,
            'missing_holdout_count':missing,'known_ineligible_holdout_count':known_ineligible,
            'unestimated_eligible_holdout_count':unestimated_eligible,
            'frozen_estimate_coverage':ratio(estimated,frozen),
            'loaded_estimate_coverage':ratio(estimated,loaded),
            'eligible_coverage':ratio(estimated,eligible),
            'evaluation_denominators':denominators,
            'unknown_holdout_count':len(control)-estimated,'metrics':metrics,'rows':rows,
            'sale_price_accuracy_verified':False,'profitable_labels':0,'false_positive':None,'false_negative':None,
            'winner_selected':None,'unknown_crossposts_may_remain':True}
