"""Additional source-declared evidence from complete already acquired HTML.
No inferred generation by year. No contacts/VIN/description content exported.
"""
import copy,hashlib,re,json,html
from experiments.olx_offline.source_dates import ASSIGNMENT
from experiments.olx_offline.html_snapshot import clean_url
from experiments.olx_offline.html_snapshot import SearchParser
from .vehicle_attributes import corroborate
from .source_tracking import same_detail_url


VARIANT_WORDS=r'(?:дорестайл\w*|до\s*рестайл\w*|pre[ -]?fl|fl|рестайл\w*|restyling|facelift)'
OWN_VARIANT_WORDS=r'(?:дорестайл(?:інг|инг)?|до\s*рестайл(?:інг|инг)?|pre[ -]?fl|fl|рестайл(?:інг|инг)?|restyling|facelift)'


def _description_variant(nodes,car):
    """One complete, matching own-car claim; engine variants are not car variants.

    The narrowly observed sale clause ties brand/model/family directly to the
    variant. A different car, a later engine claim, or a negated/ambiguous claim
    cannot supply a missing field. Full seller prose stays in memory.
    """
    desc=[n for n in nodes if n.closed and n.attrs.get('data-testid')=='ad_description']
    scripts=[n for n in nodes if n.tag=='script' and n.closed and n.attrs.get('id')=='olx-init-config']
    if len(desc)!=1 or len(scripts)!=1:return None,None,False
    try:
        script=''.join(v for v in scripts[0].children if isinstance(v,str))
        assignments=list(ASSIGNMENT.finditer(script))
        if len(assignments)!=1:raise ValueError()
        state,_=json.JSONDecoder().raw_decode(script[assignments[0].end():])
        if isinstance(state,str):state=json.loads(state)
        ad=state['ad']['ad']
        if (str(ad.get('id'))!=car.get('id') or not same_detail_url(ad.get('url'),car.get('url'))
                or ad.get('isActive') is not True or ad.get('status')!='active'
                or not isinstance(ad.get('description'),str)):raise ValueError()
        text=' '.join(desc[0].text().split())
        headings=[n.text() for n in desc[0].nodes() if n.tag in ('h2','h3','h4') and n.closed]
        if headings and headings[0] in ('Опис','Описание') and text.startswith(headings[0]+' '):
            text=text[len(headings[0]):].strip()
        source=html.unescape(re.sub(r'<[^>]*>',' ',ad['description']))
        if text!=' '.join(source.split()):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):return None,None,False
    variants=set();ambiguous=False
    for clause in re.split(r'[.!?;\n]',source):
        own=re.match(r'^\s*(?:продам|продаю)\s+(?:власну|свою|свій|мой|свой|собственную)\s+'
                     r'(?:skoda|шкода)\s+(?:octavia|октавія|октавия)\s+[aа]\s?5\s+',clause,re.I)
        if not own:continue
        suffix=clause[own.end():].strip()
        explicit=re.match(r'(?:не\s+|not\s+|без\s+)?'+OWN_VARIANT_WORDS+r'(?!\w)',suffix,re.I)
        if not explicit:continue
        # A restyled engine/version is not an explicit restyled vehicle.
        following=suffix[explicit.end():].strip()
        if re.match(r'(?:(?:версі\w*|верси\w*)\s+)?(?:двигун\w*|двигател\w*|engine)(?!\w)',following,re.I):continue
        variant=octavia_variant(suffix)
        if variant:variants.add(variant)
        else:ambiguous=True
    if not variants and not ambiguous:return None,None,False
    proof={'basis':'explicit_own_car_complete_matching_description','independently_verified':False,
           'description_sha256':hashlib.sha256(desc[0].text().encode()).hexdigest(),
           'identity_basis':'matching_active_public_state_source_id_and_url'}
    return (next(iter(variants)) if len(variants)==1 and not ambiguous else None,
            proof,ambiguous or len(variants)>1)


def _running_paint_condition(paint):
    """Observed source enums only; an arbitrary nonempty label is not clean."""
    paint=' '.join(paint.replace('\u200b','').split())
    repair={
        'Потрібно відновлення (рихтування, фарбування, заміна деталей, зварювання)',
        'Требуется восстановление (рихтовка, покраска, замена деталей, сварка)',
        'Требует восстановления (рихтовка, покраска, замена деталей, сварка)',
        "Не відремонтовані сліди експлуатації (подряпини, вм'ятини і т.д.)",
        'Не отремонтированные следы эксплуатации (царапины, вмятины и т.д.)',
    }
    running={
        'Як нове, без видимих слідів експлуатації',
        'Как новое, без видимых следов эксплуатации',
        'Незначні сліди експлуатації (дрібні подряпини, сколи)',
        'Незначительные следы эксплуатации (мелкие царапины, сколы)',
        'Професійно відремонтовані сліди експлуатації',
        'Профессионально отремонтированные следы эксплуатации',
    }
    return 'running_body_repair' if paint in repair else 'seller_declared_running' if paint in running else None


def octavia_variant(text):
    """Explicit seller vocabulary only; A5 or a year alone never implies FL."""
    text=text.casefold()
    pre=list(re.finditer(r'(?<!\w)(?:дорестайл\w*|до\s*рестайл\w*|pre[ -]?fl)(?!\w)',text))
    rest=list(re.finditer(r'(?<!\w)(?:fl|рестайл\w*|restyling|facelift)(?!\w)',text))
    rest=[m for m in rest if not any(p.start()<=m.start()<p.end() for p in pre)]
    if any(re.search(r'(?:не|not|без)\s+$',text[max(0,m.start()-8):m.start()]) for m in pre+rest):return None
    if pre and not rest:return 'pre_FL'
    if rest and not pre:return 'FL'
    return None


def description_damage(nodes):
    """High precision visible claims; no repair-cost or physical-condition inference.

    Only complete descriptions are used. Negations and explicitly completed
    repairs are excluded. Distinct remaining damage types form distinct cohorts.
    Text and contacts stay in memory; only codes and a digest survive.
    """
    descriptions=[n for n in nodes if n.attrs.get('data-testid')=='ad_description']
    if len(descriptions)!=1 or not descriptions[0].closed:return []
    text=descriptions[0].text().casefold().replace('’',"'").replace('`',"'")
    flags=set()
    for clause in re.split(r'[.!?;,\n]',text):
        matches=[]
        if re.search(r'лобов\w*|вітров\w*',clause):
            matches+= [('windshield_crack',m) for m in re.finditer(r'тріщин\w*|трещин\w*',clause)]
        matches+= [('body_dents',m) for m in re.finditer(r"вм'?ят(?:ин|і?н|инки)\w*",clause)]
        for kind,match in matches:
            before=clause[max(0,match.start()-45):match.start()]
            if re.search(r'(?:без|немає|нема|нет|не\s+має|не\s+було)\s+(?:\w+\s+){0,3}$',before):continue
            if re.search(r'(?:була|були|было|были|раніше|ранее)\s+(?:\w+\s+){0,3}$',before):continue
            completed=list(re.finditer(r'замінено|заменено|відремонтован\w*|отремонтирован\w*|усунен\w*|устранен\w*|устранён\w*|устранены|прибран\w*|виправлен\w*',clause))
            # A negated repair ("не відремонтовані") still describes damage.
            completed=[m for m in completed if not re.search(r'не\s+$',clause[max(0,m.start()-5):m.start()])]
            if completed and not re.search(r'залишил\w*|остал\w*|досі|сейчас',clause):continue
            flags.add(kind)
    return sorted(flags)


def enrich(data, parsed):
    out=copy.deepcopy(parsed); car=out['listing']
    if out['summary']['download_truncated']:return out
    p=SearchParser();p.feed(data.decode('utf-8',errors='replace'));p.close()
    nodes=[n for root in p.roots for n in root.nodes()]
    old=car.get('observed_asking_display',{})
    sale_only={'ordinary_sale_not_corroborated','visible_sale_terms_missing_or_conflicting'}
    if old.get('reasons') and set(old['reasons']) <= sale_only:
        scripts=[n for n in nodes if n.tag=='script' and n.closed and n.attrs.get('id')=='olx-init-config']
        visible=[n.text().partition(':')[2].strip() for n in nodes if n.tag=='p' and n.closed and n.text().partition(':')[0] in ('Умови продажу','Условия продажи')]
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
            if len(terms)!=1:raise ValueError('sale_terms_unavailable_or_ambiguous')
            wanted={'Простая продажа','Возможен обмен'} if set(terms[0].get('value','').split(', '))=={'Простая продажа','Возможен обмен'} else {'Звичайний продаж','Можливий обмін'}
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
            key={'Техническое состояние':'Технічний стан','Лакокрасочное покрытие':'Лакофарбове покриття','Поколение':'Покоління'}.get(key,key)
            if sep and key in ('Тип кузова','Технічний стан','Лакофарбове покриття','Покоління'):
                value=value.strip()
                if key in fields and fields[key]!=value:conflicts.append(key)
                fields[key]=value
    evidence={}
    body={'Ліфтбек':'liftback','Лифтбек':'liftback','Позашляховик / Кросовер':'suv','Внедорожник / Кроссовер':'suv','Мінівен':'minivan','Минивэн':'minivan','Купе':'coupe','Кабріолет':'convertible','Кабриолет':'convertible','Пікап':'pickup','Пикап':'pickup'}.get(fields.get('Тип кузова'))
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
            car.pop('generation_variant',None)
            visible_variant=title+' '+fields.get('Покоління','')
            variant=octavia_variant(visible_variant)
            described,description_proof,ambiguous=_description_variant(nodes,car)
            visible_ambiguous=not variant and bool(re.search(r'(?<!\w)'+VARIANT_WORDS+r'(?!\w)',visible_variant,re.I))
            if ambiguous or visible_ambiguous or (variant and described and variant!=described):
                conflicts.append('generation_variant');variant=None
            elif described and not variant:
                variant=described
            if variant:
                car['generation_variant']=variant
                evidence['generation_variant']=description_proof if described and not octavia_variant(visible_variant) else {'basis':'explicit_visible_seller_vocabulary',
                    'independently_verified':False,'title_sha256':hashlib.sha256(title.encode()).hexdigest()}
                if description_proof and described==variant:
                    evidence['generation_variant']['description_sha256']=description_proof['description_sha256']
        elif a5 and other:conflicts.append('generation')
    tech=fields.get('Технічний стан',''); paint=fields.get('Лакофарбове покриття','')
    car.pop('research_condition',None)
    if tech and 'Технічний стан' not in conflicts and 'Лакофарбове покриття' not in conflicts:
        if re.search(r'не на ходу',tech,re.I):condition='not_running'
        elif re.search(r'На ходу, (?:технічно справна|технически исправна)',tech,re.I):
            condition=_running_paint_condition(paint)
        else:condition=None
        if condition:
            damage=description_damage(nodes)
            if damage and condition in ('seller_declared_running','running_body_repair'):
                condition=('running_reported_damage' if condition=='seller_declared_running' else condition)+':'+'+'.join(damage)
            car['research_condition']=condition
            evidence['condition']={'basis':'visible_source_attributes','technical':tech,'paint':paint,
                                   'independently_verified':False}
            if damage:
                evidence['condition']['description_damage_flags']=damage
                evidence['condition']['description_basis']='complete_visible_seller_claim_not_physical_review'
                description=next(n.text() for n in nodes if n.attrs.get('data-testid')=='ad_description')
                evidence['condition']['description_sha256']=hashlib.sha256(description.encode()).hexdigest()
    car['research_evidence']=evidence
    car['research_field_conflicts']=conflicts
    car.update(corroborate(nodes,car))
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
