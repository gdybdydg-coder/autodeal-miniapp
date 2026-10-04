"""Additional source-declared evidence from complete already acquired HTML.
No inferred generation by year. No contacts/VIN/description content exported.
"""
import copy,hashlib,re,json
from experiments.olx_offline.source_dates import ASSIGNMENT
from experiments.olx_offline.html_snapshot import clean_url
from experiments.olx_offline.html_snapshot import SearchParser


def enrich(data, parsed):
    out=copy.deepcopy(parsed); car=out['listing']
    if out['summary']['download_truncated']:return out
    p=SearchParser();p.feed(data.decode('utf-8',errors='replace'));p.close()
    nodes=[n for root in p.roots for n in root.nodes()]
    old=car.get('observed_asking_display',{})
    sale_only={'ordinary_sale_not_corroborated','visible_sale_terms_missing_or_conflicting'}
    if old.get('reasons') and set(old['reasons']) <= sale_only:
        scripts=[n for n in nodes if n.tag=='script' and n.closed and n.attrs.get('id')=='olx-init-config']
        visible=[n.text().partition(':')[2].strip() for n in nodes if n.tag=='p' and n.closed and n.text().partition(':')[0]=='Умови продажу']
        try:
            if len(scripts)!=1 or not visible:raise ValueError()
            script=''.join(v for v in scripts[0].children if isinstance(v,str))
            matches=list(ASSIGNMENT.finditer(script))
            if len(matches)!=1:raise ValueError()
            state,_=json.JSONDecoder().raw_decode(script[matches[0].end():])
            if isinstance(state,str):state=json.loads(state)
            ad=state['ad']['ad']
            if str(ad['id'])!=car['id'] or clean_url(ad['url'])!=car['url']:raise ValueError()
            terms=[r for r in ad['params'] if r.get('key')=='sale_terms']
            wanted={'Звичайний продаж','Можливий обмін'}
            if (len(terms)!=1 or set(terms[0]['normalizedValue'])!={'regular_sale','possible_exchange'}
                    or set(terms[0]['value'].split(', '))!=wanted
                    or any(set(v.split(', '))!=wanted for v in visible)):raise ValueError()
            car['research_asking_display']={**old,'status':'corroborated_display','reasons':[],
                'basis':'regular_sale_with_optional_exchange_corroborated',
                'previous_reasons':old['reasons'],'full_price_verified':False}
        except (ValueError,KeyError,TypeError,AttributeError):
            pass
    fields={};conflicts=[]
    for root in p.roots:
        for n in root.nodes():
            if n.tag!='p' or not n.closed:continue
            key,sep,value=n.text().partition(':')
            if sep and key in ('Тип кузова','Технічний стан','Лакофарбове покриття','Покоління'):
                value=value.strip()
                if key in fields and fields[key]!=value:conflicts.append(key)
                fields[key]=value
    evidence={}
    body={'Ліфтбек':'liftback','Позашляховик / Кросовер':'suv','Мінівен':'minivan','Купе':'coupe','Кабріолет':'convertible','Пікап':'pickup'}.get(fields.get('Тип кузова'))
    if body and not car.get('body') and 'Тип кузова' not in conflicts:
        car['body']=body;evidence['body']={'basis':'visible_source_attribute','label':fields['Тип кузова']}
    # One explicit, observed family vocabulary; no approximate year-to-generation mapping.
    title=car.get('title','')
    if car.get('brand')=='Skoda' and car.get('model')=='Octavia':
        a5=bool(re.search(r'(?<!\w)[aа]\s?5(?!\w)',title,re.I))
        other=bool(re.search(r'(?<!\w)(?:[aа]\s?[47]|tour|тур)(?!\w)',title,re.I))
        if a5 and not other:
            car['generation']='A5'
            evidence['generation']={'basis':'seller_declared_title','independently_verified':False,
                                    'title_sha256':hashlib.sha256(title.encode()).hexdigest()}
        elif a5 and other:conflicts.append('generation')
    tech=fields.get('Технічний стан',''); paint=fields.get('Лакофарбове покриття','')
    if tech and 'Технічний стан' not in conflicts and 'Лакофарбове покриття' not in conflicts:
        if re.search(r'не на ходу',tech,re.I):condition='not_running'
        elif re.search(r'На ходу, технічно справна',tech,re.I):
            condition='running_body_repair' if paint.startswith(('Потрібно відновлення','Не відремонтовані')) else 'seller_declared_running' if paint else None
        else:condition=None
        if condition:
            car['research_condition']=condition
            evidence['condition']={'basis':'visible_source_attributes','technical':tech,'paint':paint,
                                   'independently_verified':False}
    car['research_evidence']=evidence
    car['research_field_conflicts']=conflicts
    return out


def asking_price_reasons(car):
    reasons=[];o=car.get('research_asking_display',car.get('observed_asking_display',{}))
    if car.get('eligibility_review',{}).get('status')!='allowed':reasons.append('eligibility_not_allowed')
    if car.get('category')!='whole_passenger_car':reasons.append('whole_car_unconfirmed')
    if o.get('status')!='corroborated_display':reasons.extend(o.get('reasons') or ['asking_display_unconfirmed'])
    if not o.get('description_reviewed_in_full'):reasons.append('description_incomplete')
    if o.get('amount')!=car.get('price') or o.get('currency')!=car.get('currency'):reasons.append('asking_display_conflict')
    reasons.extend(r for r in car.get('price_review',{}).get('reasons',[]) if r!='full_price_unconfirmed')
    reasons.extend('field_conflict_'+str(k) for k in car.get('field_conflicts',[]))
    reasons.extend('field_conflict_'+str(k) for k in car.get('research_field_conflicts',[]))
    if car.get('price_conflicts'):reasons.append('price_conflict')
    return sorted(set(reasons))
