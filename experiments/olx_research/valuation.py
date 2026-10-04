"""Explainable research estimates of asking prices, never certified deals.

Real full-page display evidence can support an explicitly experimental asking
price comparison without being relabelled as proven seller-origin currency or a
completed sale. Production modules are not imported or modified.
"""
from collections import Counter
from decimal import Decimal, localcontext
from .fx_policy import normalize
from .observations import asking_price_reasons
from .body_policy import body_policy,comparable_value
from experiments.olx_offline.market import _percentile

METHODS=('median','trimmed_mean','weighted_median')
FIELDS=('brand','model','generation','body','fuel','transmission','engine_cc','drive_type','power_hp')


def screened(car,quote,now):
    reasons=asking_price_reasons(car)
    if car.get('source')!='olx' or not isinstance(car.get('id'),str):reasons.append('identity_invalid')
    for k in FIELDS:
        if car.get(k) is None:reasons.append('missing_'+k)
    for k in ('year','mileage_km'):
        if type(car.get(k)) is not int:reasons.append('missing_'+k)
    if not car.get('research_condition'):reasons.append('condition_unconfirmed')
    checked=car.get('checked_at')
    if type(checked) is not int or not 0<=now-checked<=30*86400:reasons.append('observation_stale_or_future')
    p=normalize(car,quote,now)
    if p['status']!='ready':reasons.append(p['reason'])
    return p,sorted(set(reasons))


def estimate(target,candidates,quote,now,*,minimum=8):
    if type(minimum) is not int or minimum<3:raise ValueError('Research minimum >=3 required')
    price,reasons=screened(target,quote,now)
    base={'status':'profitability_unconfirmed','target_id':target.get('id'),'reasons':reasons,
          'methods':{},'reference_usd':None,'sample':0,'independent_vehicles_verified':None,
          'sample_unit':'distinct_ads_after_known_vehicle_dedup','real_world_accuracy_verified':False,
          'price_basis':'corroborated_full_page_asking_display_not_completed_sale',
          'seller_claims_independently_verified':False,'recommended':None,
          'extra_ria_margin_percent':'0','required_sample':minimum,'used_comparables':[],
          'exclusions':{},'excluded':[],'selected_method':None,'body_policy':body_policy(target)}
    if reasons:return base
    # Keep latest source+ID; same-time conflicting versions are held together.
    versions={}
    for c in candidates:versions.setdefault((c.get('source'),c.get('id')),[]).append(c)
    rows=[];excluded=[];seen_vehicles=set();target_key=target.get('vehicle_key')
    vehicle_latest={}; vehicle_held=set(); vehicle_signatures={}
    for vs in versions.values():
        for c in vs:
            v=c.get('vehicle_key');checked=c.get('checked_at')
            if v and type(checked) is int:vehicle_latest[v]=max(checked,vehicle_latest.get(v,0))
    for vs in versions.values():
        for c in vs:
            v=c.get('vehicle_key')
            if v and c.get('checked_at')==vehicle_latest[v]:
                if screened(c,quote,now)[1]:vehicle_held.add(v)
                signature=tuple(str(c.get(k)) for k in (*FIELDS,'modification','year','mileage_km','research_condition','price','currency'))
                vehicle_signatures.setdefault(v,set()).add(signature)
    vehicle_held.update(v for v,s in vehicle_signatures.items() if len(s)>1)
    for key,vs in sorted(versions.items(),key=lambda x:str(x[0])):
        dated=[v for v in vs if type(v.get('checked_at')) is int]
        if not dated:excluded.append({'id':key[1],'reasons':['observation_missing']});continue
        latest=max(v['checked_at'] for v in dated); newest=[v for v in dated if v['checked_at']==latest]
        c=newest[0];why=[]
        if any(v!=c for v in newest):why.append('conflicting_same_id_observations')
        if key==(target.get('source'),target.get('id')) or target_key and c.get('vehicle_key')==target_key:why.append('self_or_known_crosspost')
        vehicle=c.get('vehicle_key')
        if vehicle in vehicle_held:why.append('known_vehicle_current_evidence_invalid')
        if vehicle and c.get('checked_at',0)<vehicle_latest[vehicle]:why.append('known_vehicle_superseded')
        p,screen=screened(c,quote,now);why+=screen
        if not why:
            why+=['mismatch_'+f for f in FIELDS if str(comparable_value(c,f)).casefold()!=str(comparable_value(target,f)).casefold()]
            if (c.get('modification') and target.get('modification')
                    and c['modification'].casefold()!=target['modification'].casefold()):
                why.append('mismatch_modification')
            if abs(c['year']-target['year'])>1:why.append('mismatch_year')
            if abs(c['mileage_km']-target['mileage_km'])>30000:why.append('mismatch_mileage')
            if c['research_condition']!=target['research_condition']:why.append('mismatch_condition')
            if c.get('vehicle_key') and c['vehicle_key'] in seen_vehicles:why.append('known_crosspost')
        if why:excluded.append({'id':key[1],'reasons':sorted(set(why))});continue
        if c.get('vehicle_key'):seen_vehicles.add(c['vehicle_key'])
        rows.append((c,Decimal(p['usd_amount']),p))
    base['excluded']=excluded;base['exclusions']=dict(Counter(r for e in excluded for r in e['reasons']))
    base['sample']=len(rows)
    base['used_comparables']=[{'id':c['id'],'asking_usd':str(v),'checked_at':c['checked_at'],'fx_basis':p['fx_basis']} for c,v,p in rows]
    base['independent_vehicles_verified']=len(rows) if (target_key and target.get('vehicle_identity_verified') is True and all(c.get('vehicle_key') and c.get('vehicle_identity_verified') is True for c,_,_ in rows)) else None
    if len(rows)<minimum:base['reasons']=['insufficient_comparables'];return base
    with localcontext() as ctx:
        ctx.prec=50
        vals=[v for _,v,_ in rows];q1=_percentile(vals,Decimal('.25'));q3=_percentile(vals,Decimal('.75'));iqr=q3-q1
        accepted=[r for r in rows if q1-Decimal('1.5')*iqr<=r[1]<=q3+Decimal('1.5')*iqr]
        outliers=[{'id':c['id'],'reasons':['price_outlier']} for c,v,p in rows if not q1-Decimal('1.5')*iqr<=v<=q3+Decimal('1.5')*iqr]
        base['excluded']+=outliers
        base['exclusions']=dict(Counter(r for e in base['excluded'] for r in e['reasons']))
        base['sample']=len(accepted)
        base['used_comparables']=[{'id':c['id'],'asking_usd':str(v),'checked_at':c['checked_at'],'fx_basis':p['fx_basis']} for c,v,p in accepted]
        base['independent_vehicles_verified']=len(accepted) if (target_key and target.get('vehicle_identity_verified') is True and all(c.get('vehicle_key') and c.get('vehicle_identity_verified') is True for c,_,_ in accepted)) else None
        if len(accepted)<minimum:
            base['sample']=len(accepted);base['reasons']=['insufficient_after_outliers']
            base['used_comparables']=[{'id':c['id'],'asking_usd':str(v),'checked_at':c['checked_at'],'fx_basis':p['fx_basis']} for c,v,p in accepted]
            return base
        rows=accepted;vals=sorted(v for _,v,_ in rows);median=_percentile(vals,Decimal('.5'))
        if (max(vals)-min(vals))/median>Decimal('.4'):base['reasons']=['wide_dispersion'];return base
        trim=max(1,len(vals)//10);trimmed=vals[trim:-trim]
        robust_mean=sum(trimmed)/len(trimmed)
        weights=[]
        for c,v,p in rows:
            distance=Decimal(abs(c['year']-target['year']))+Decimal(abs(c['mileage_km']-target['mileage_km']))/30000
            age=Decimal(now-c['checked_at'])/(30*86400)
            region=Decimal(0) if c.get('region') and c.get('region')==target.get('region') else Decimal('.5')
            weights.append((v,Decimal(1)/(1+distance+age+region)))
        half=sum(w for _,w in weights)/2;acc=Decimal(0);weighted=vals[-1]
        for v,w in sorted(weights):
            acc+=w
            if acc>=half:weighted=v;break
        refs={'median':median,'trimmed_mean':robust_mean,'weighted_median':weighted}
        actual=Decimal(price['usd_amount'])
        low,high=(_percentile(vals,q) for q in (Decimal('.25'),Decimal('.75')))
        base.update(status='experimental_asking_estimate',sample=len(rows),
            reference_usd=str(median),selected_method='median',
            selection_reason='Predeclared transparent research baseline; no accuracy winner established',
            range_usd={'low':str(low),'high':str(high)},range_kind='empirical_IQR_not_confidence_or_sale_interval',
            methods={m:{'reference_usd':str(v),'discount_percent':str((v-actual)/v*100)} for m,v in refs.items()},
            used_comparables=[{'id':c['id'],'asking_usd':str(v),'checked_at':c['checked_at'],'fx_basis':p['fx_basis']} for c,v,p in rows],
            data_date_range={'oldest':min(c['checked_at'] for c,_,_ in rows),'newest':max(c['checked_at'] for c,_,_ in rows)},
            reasons=['asking_not_sale','source_declared_attributes','independent_holdout_not_validated',
                     'unknown_crossposts_may_remain','unobserved_repair_costs_and_options'])
        # Quote spread sensitivity must remain visible even with one shared basis.
        base['fx_basis']=quote.payload() if any(p['fx'] for _,_,p in rows) or price['fx'] else None
        base['fx_sensitive']=bool(base['fx_basis'] and quote.kind=='bank_derived_midpoint')
    return base


def assess_dataset(cars,quote,now,*,minimum=8,labels=None,dataset_kind='real_observations'):
    results=[estimate(c,cars,quote,now,minimum=minimum) for c in cars]
    evaluated=[r for r in results if r['status']=='experimental_asking_estimate']
    labels=labels or {}
    verified={k:v for k,v in labels.items() if v.get('independent') is True and type(v.get('profitable')) is bool and v.get('annotation_basis')}
    confusion={m:{'tp':0,'fp':0,'tn':0,'fn':0,'unresolved':0} for m in METHODS}
    for r in results:
        label=verified.get(r['target_id'])
        if label is None:continue
        for m in METHODS:
            if m not in r['methods']:confusion[m]['unresolved']+=1;continue
            prediction=Decimal(r['methods'][m]['discount_percent'])>=Decimal(str(label['threshold']))
            confusion[m][('t' if prediction==label['profitable'] else 'f')+('p' if prediction else 'n')]+=1
    return {'dataset_kind':dataset_kind,'total':len(cars),'estimated':len(evaluated),
            'coverage':str(Decimal(len(evaluated))/len(cars)) if cars else None,
            'independently_labeled_cases':len(verified),'confusion':confusion if verified else None,
            'accuracy_verified':False,'results':results}
