"""Explain known filter contradictions before capping detail candidates."""
from decimal import Decimal
from .fx_policy import normalize


def filter_reasons(car, filters, quote, now):
    rejected=[]; unknown=[]
    def label(v):return str(v).casefold().removesuffix(' область').strip()
    for key in ('brand','model','region','body','fuel','transmission'):
        wanted=filters.get(key); actual=car.get(key)
        if not wanted: continue
        wanted=wanted if isinstance(wanted,list) else [wanted]
        if actual is None: unknown.append('missing_'+key)
        elif label(actual) not in [label(v) for v in wanted]: rejected.append('filter_'+key)
    for key in ('year','mileage_km','engine_cc'):
        actual=car.get(key)
        for end,op in (('min',lambda a,b:a<b),('max',lambda a,b:a>b)):
            bound=filters.get(key+'_'+end)
            if bound is not None:
                if actual is None:unknown.append('missing_'+key)
                elif op(actual,bound):rejected.append('filter_'+key+'_'+end)
    if filters.get('price_min') is not None or filters.get('price_max') is not None:
        p=normalize(car,quote,now)
        if p['status']!='ready':unknown.append(p['reason'])
        else:
            for end,op in (('min',lambda a,b:a<b),('max',lambda a,b:a>b)):
                bound=filters.get('price_'+end)
                if bound is not None and op(Decimal(p['usd_amount']),Decimal(str(bound))):rejected.append('filter_price_'+end)
    return {'match':not rejected,'reasons':sorted(set(rejected)),'unknown':sorted(set(unknown))}


def select_candidates(cards, searches, quote, now, *, already_seen=(), limit=9):
    if type(limit) is not int or not 0<limit<=100:raise ValueError('bounded limit required')
    selected=[]; reviews=[]; encountered=set();old=set(tuple(x) for x in already_seen)
    for car in sorted(cards,key=lambda c:c.get('observed_search_reason')!='organic'):
        key=(car.get('source'),car.get('id'))
        why=[]
        if key in encountered:why=['duplicate_in_page']
        elif key in old:why=['already_attempted']
        elif car.get('eligibility_review',{}).get('status')=='excluded':why=['forbidden_offer']
        else:
            verdicts=[filter_reasons(car,f,quote,now) for f in searches]
            if not verdicts:why=['no_active_search']
            elif not any(v['match'] for v in verdicts):why=sorted({r for v in verdicts for r in v['reasons']})
        encountered.add(key)
        if why:reviews.append({'source':key[0],'id':key[1],'decision':'excluded','reasons':why});continue
        if len(selected)>=limit:reviews.append({'source':key[0],'id':key[1],'decision':'deferred','reasons':['detail_budget']});continue
        selected.append(car);reviews.append({'source':key[0],'id':key[1],'decision':'selected','reasons':[]})
    return {'selected':selected,'reviews':reviews,'whole_source_complete':False}
