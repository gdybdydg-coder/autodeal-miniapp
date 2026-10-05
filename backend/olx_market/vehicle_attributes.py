"""Corroborate public vehicle attributes; retain no raw VIN or seller text."""
import hashlib,html,json,re
from decimal import Decimal,InvalidOperation
from experiments.olx_offline.source_dates import ASSIGNMENT
from .source_tracking import same_detail_url

DRIVES={'Передний':'front','Передній':'front','Полный':'full','Повний':'full',
        'Задний':'back','Задній':'back'}
LABELS={'drive_type':('Тип привода','Тип приводу'),
        'power_hp':('Мощность','Потужність'),
        'modification':('Модификация','Модифікація'),
        'doors':('Кількість дверей','Количество дверей'),
        'vin':('VIN','VIN номер','VIN-код','VIN номер кузова')}

def compact(value):return ' '.join(str(value).split())

def hp(value):
    m=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(?:л\.?\s*с\.?|к\.?\s*с\.?)',compact(value),re.I)
    if not m:return None
    n=Decimal(m[1].replace(',','.'))
    return int(n) if n==int(n) and 1<=n<=2000 else None


POWER_TOKEN=r'(\d{2,3}(?:[.,]\d+)?\s*(?:л\.?\s*с\.?|к\.?\s*с\.?))(?!\w)'


def description_power(nodes,ad):
    desc=[n for n in nodes if n.closed and n.attrs.get('data-testid')=='ad_description']
    source=ad.get('description')
    if len(desc)!=1 or not isinstance(source,str):return set(),None
    text=compact(desc[0].text())
    # The real description container includes the interface heading.
    headings=[n.text() for n in desc[0].nodes() if n.tag in ('h2','h3','h4') and n.closed]
    if headings and headings[0] in ('Описание','Опис') and text.startswith(headings[0]+' '):
        text=text[len(headings[0]):].strip()
    source=compact(html.unescape(re.sub(r'<[^>]*>',' ',source)))
    if text!=source:return set(),None
    values={hp(m[1]) for m in re.finditer(POWER_TOKEN,text,re.I)}-{None}
    explicit=re.search(r'\b(?:потужність|мощность)\s*(?:(?:двигуна|двигателя)\s*)?[:—-]?\s*'+POWER_TOKEN,text,re.I)
    positive=None
    if explicit:
        preceding=text[max(0,explicit.start()-30):explicit.start()]
        if not re.search(r'\b(?:не|нет|без|замість|вместо)\s*$',preceding,re.I):positive=hp(explicit[1])
    return values,positive


def corroborate(nodes,car):
    result={'drive_type':None,'power_hp':None,'modification':None,'doors':None,
            'vehicle_key':None,'vehicle_identity_verified':False,
            'attribute_review':{'version':'visible-state-v1','issues':[],'evidence':{}}}
    review=result['attribute_review'];issues=review['issues'];proof=review['evidence']
    visible={}
    for n in nodes:
        if n.tag!='p' or not n.closed:continue
        label,sep,value=n.text().partition(':')
        if not sep:continue
        for field,labels in LABELS.items():
            if compact(label) in labels:visible.setdefault(field,set()).add(compact(value))
    try:
        scripts=[n for n in nodes if n.tag=='script' and n.closed and n.attrs.get('id')=='olx-init-config']
        if len(scripts)!=1:raise ValueError()
        script=''.join(v for v in scripts[0].children if isinstance(v,str));matches=list(ASSIGNMENT.finditer(script))
        if len(matches)!=1:raise ValueError()
        state,_=json.JSONDecoder().raw_decode(script[matches[0].end():])
        if isinstance(state,str):state=json.loads(state)
        ad=state['ad']['ad']
        if (type(ad.get('id')) not in (int,str) or str(ad['id'])!=car['id']
                or not same_detail_url(ad.get('url'),car.get('url'))
                or ad.get('isActive') is not True or ad.get('status')!='active'):raise ValueError()
        params=ad['params']
        if not isinstance(params,list):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):
        issues.append('public_state_identity_or_activity_unconfirmed');return result
    for field,key in (('drive_type','drive_type'),('power_hp','power'),('modification','modification'),('doors','doors_num'),('vin','vin_number')):
        rows=[r for r in params if isinstance(r,dict) and r.get('key')==key]
        seen=visible.get(field,set())
        if len(rows)!=1 or len(seen)!=1:
            issues.append(field+'_missing_or_ambiguous');continue
        r=rows[0];text=next(iter(seen));normalized=r.get('normalizedValue');value=None
        if compact(r.get('value',''))!=text:
            issues.append(field+'_visible_state_conflict');continue
        if field=='drive_type':
            value=DRIVES.get(text)
            if value!=normalized:value=None
        elif field=='power_hp':
            value=hp(text)
            try:
                if value is None or Decimal(str(normalized))!=value:value=None
            except InvalidOperation:value=None
        elif field=='doors':
            if text in ('2','3','4','5') and str(normalized)==text:value=int(text)
        elif field=='modification':
            # Engine-labelled variants only, not an arbitrary seller text sink.
            if isinstance(normalized,str) and compact(normalized)==text and re.fullmatch(r'\d[\w\s.()+/-]{1,60}',text):
                value=text.casefold()
        elif field=='vin':
            if isinstance(normalized,str) and normalized==text and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',text):
                value='vin-sha256:'+hashlib.sha256(text.encode()).hexdigest()
        if value is None:issues.append(field+'_value_or_units_unconfirmed');continue
        if field=='vin':
            result['vehicle_key']=value
            proof['vehicle_key']={'basis':'matching_visible_and_public_state_vin_claim',
                                  'physical_identity_verified':False,'format_verified':True}
        else:
            result[field]=value
            proof[field]={'basis':'matching_visible_and_public_state','source_key':key}
    mentioned,positive=description_power(nodes,ad)
    known=result['power_hp']
    if known is not None and mentioned and mentioned!={known}:
        result['power_hp']=None;proof.pop('power_hp',None)
        issues.append('power_hp_description_conflict')
    elif (known is None and not any(isinstance(r,dict) and r.get('key')=='power' for r in params)
          and positive is not None and mentioned=={positive}):
        result['power_hp']=positive
        proof['power_hp']={'basis':'explicit_power_claim_in_complete_matching_description'}
        issues[:]=[i for i in issues if i!='power_hp_missing_or_ambiguous']
    return result
