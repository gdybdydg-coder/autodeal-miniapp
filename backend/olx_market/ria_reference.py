"""Pure AUTO.RIA full-info reference adapter for the separate OLX market.

No transport, credentials or AUTO.RIA state writes. A provider observation is
an asking price, not a sale label. Missing identity/variant/power stays blocked.
"""
import hashlib
import json
import re
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlsplit

VERSION='olx-ria-reference-v2'
MODIFICATION_POLICY='explicit_provider_engine_code-v1'
FIELDS=('source','id','url','price','currency','brand','model','generation',
    'generation_variant','body','fuel','fuel_subtype','transmission','drive_type',
    'engine_cc','power_hp','year','mileage_km','research_condition','checked_at',
    'vehicle_key','vehicle_identity_verified','category','eligibility_review','reference_provenance',
    'modification','source_modification_evidence','condition_evidence')


def engine_modification(label,car):
    """Keep explicit engine codes, never infer them from year/power/catalog ID.

    The field is a provider modification label, not arbitrary seller prose.
    An explicitly labelled engine code is retained verbatim after case folding.
    The only unlabelled vocabulary is literal BKC/BXE directly following the
    explicit '1.9 TDI' descriptor, with matching source fuel/volume. This is a
    token comparison policy, not a claim that all 1.9 TDI engines have that code.
    Trim names (Ambiente/Elegance), transmission words and catalog IDs do not
    become engine identities. Other unlabelled codes remain unassigned.
    """
    if not isinstance(label,str) or not 1<=len(label)<=150:
        raise ValueError('reference_modification_label_pending')
    codes=set(re.findall(r'(?:код\s+двигуна|код\s+двигателя|engine\s+code)\s*[:=-]?\s*([a-z0-9]{2,6})(?!\w)',label,re.I))
    codes={c.casefold() for c in codes}
    if car.get('engine_cc')==1900 and car.get('fuel_id')==2:
        codes.update(c.casefold() for c in re.findall(r'(?<!\w)1[.,]9\s*TDI\s+(BKC|BXE)(?!\w)',label,re.I))
    if len(codes)>1:raise ValueError('reference_modification_conflict')
    key='engine_code:'+next(iter(codes)) if codes else None
    return key,{'policy':MODIFICATION_POLICY,'catalog_id':car.get('modification_id'),
        'label_sha256':hashlib.sha256(label.encode()).hexdigest(),
        'basis':'explicit_provider_engine_code' if key else 'engine_code_not_explicit'}


def binding(car):
    return hashlib.sha256(json.dumps({k:car.get(k) for k in FIELDS},sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def public_url(car):
    u=urlsplit(car.get('url',''));sid=car.get('id','')
    return bool(car.get('source')=='auto_ria' and isinstance(sid,str) and sid.isdigit() and int(sid)>0
        and u.scheme=='https' and u.netloc=='auto.ria.com' and not u.query and not u.fragment
        and re.fullmatch(r'/auto_[a-zA-Z0-9_-]+_'+re.escape(sid)+r'\.html',u.path))


def reasons(car):
    out=[];e=car.get('ria_reference_evidence') or {}
    if not isinstance(e,dict):e={}
    if not public_url(car):out.append('ria_reference_identity_invalid')
    if (e.get('version')!=VERSION or e.get('http_status')!=200
            or e.get('method')!='auto/info' or e.get('binding')!=binding(car)):
        out.append('ria_full_info_receipt_unverified')
    p=car.get('reference_provenance') or {}
    if not isinstance(p,dict):p={}
    if (p.get('role') not in ('reference','holdout')
            or not isinstance(p.get('frozen_commit'),str)
            or not re.fullmatch('[a-f0-9]{40}',p.get('frozen_commit',''))
            or type(p.get('frozen_at')) is not int
            or type(car.get('checked_at')) is not int
            or p['frozen_at']>car['checked_at']):out.append('ria_reference_split_unverified')
    params=p.get('search_params',{})
    if (not isinstance(params,dict) or params.get('category_id')!=1
            or params.get('marka_id[0]')!=70 or params.get('model_id[0]')!=652
            or any(not isinstance(k,str) or k.startswith(('price','state','city','region')) for k in params)):
        out.append('ria_reference_search_price_or_region_capped')
    if car.get('currency')!='USD':out.append('ria_reference_usd_basis_unverified')
    if (car.get('category')!='whole_passenger_car'
            or car.get('eligibility_review',{}).get('status')!='allowed'
            or car.get('eligibility_review',{}).get('description_complete') is not True):
        out.append('ria_reference_eligibility_pending')
    if not car.get('generation_variant'):out.append('ria_generation_variant_pending')
    if car.get('fuel')=='gas_petrol' and car.get('fuel_subtype') not in ('propane','methane'):
        out.append('ria_fuel_subtype_pending')
    return out


def from_full_info(data,sid,checked_at,provenance):
    from ..ria_search import parse_car
    from ..valuation import vehicle_key as validate_vin
    from experiments.olx_offline.eligibility import review_listing
    car=parse_car(data,sid);auto=data['autoData']
    if car.get('brand_id')!=70 or car.get('model_id')!=652:raise ValueError('reference_group_not_reviewed')
    if car.get('category_id')!=1 or not car.get('comparable_condition'):raise ValueError('reference_condition_or_category_pending')
    if data.get('technicalCondition',{}).get('id')!=1:raise ValueError('reference_running_condition_not_explicit')
    generation={3133:('A5','pre_FL'),3607:('A5','FL')}.get(car.get('generation_id'))
    body={2:'wagon',307:'liftback',4:'hatchback'}.get(car.get('body_id'))
    fuel={1:('petrol',None),2:('diesel',None),4:('gas_petrol','propane'),8:('gas_petrol','methane')}.get(car.get('fuel_id'))
    drive={1:'full',2:'front',3:'back'}.get(auto.get('driveId'))
    if not generation or not body or not fuel or not drive or car.get('gear_id')!=1:
        raise ValueError('reference_critical_dictionary_identity_pending')
    label=auto.get('modificationName','')
    powers=re.findall(r'\((\d{2,4})\s*(?:к\.?\s*с\.?|л\.?\s*с\.?)\)',label) if isinstance(label,str) else []
    if len(powers)!=1 or not 1<=int(powers[0])<=2000:raise ValueError('reference_power_not_explicit')
    vin=data.get('VIN');key=validate_vin(vin)
    if not key:raise ValueError('reference_vehicle_identity_pending')
    description=auto.get('description')
    eligibility=review_listing({'title':data.get('title'),'category':'whole_passenger_car',
        'description':description,'description_available':isinstance(description,str) and bool(description.strip())})
    if eligibility['status']!='allowed':raise ValueError('reference_full_description_not_eligible')
    # Reuse the OLX condition vocabulary on this complete API description.
    # The adapter node is only an in-memory text interface, not an HTML receipt.
    from .observations import description_damage
    damage=description_damage([SimpleNamespace(attrs={'data-testid':'ad_description'},
        closed=True,text=lambda:description)])
    condition='running_reported_damage:'+'+'.join(damage) if damage else 'seller_declared_running'
    modification,modification_evidence=engine_modification(label,car)
    for clause in re.split(r'[.!?;,\n]',description.casefold()):
        for payment in re.finditer(r'перш\w*\s+внес\w*|перв\w*\s+взнос\w*',clause):
            before=clause[max(0,payment.start()-30):payment.start()]
            if re.search(r'(?:не|без)\s+(?:\w+\s+){0,2}$',before):continue
            if re.search(r'ціна|цена',clause):raise ValueError('reference_initial_payment_not_full_asking')
    out={'source':'auto_ria','id':sid,'url':car['url'],'price':str(car['price_usd']),'currency':'USD',
        'brand':'Skoda','model':'Octavia','generation':generation[0],'generation_variant':generation[1],
        'body':body,'fuel':fuel[0],'fuel_subtype':fuel[1],'transmission':'manual','drive_type':drive,
        'engine_cc':car['engine_cc'],'power_hp':int(powers[0]),'year':car['year'],
        'mileage_km':car['mileage'],'research_condition':condition,'checked_at':checked_at,
        'modification':modification,'source_modification_evidence':modification_evidence,
        'condition_evidence':{'basis':'complete_source_api_description',
            'description_sha256':hashlib.sha256(description.encode()).hexdigest(),
            'description_damage_flags':damage,'independently_verified':False},
        'category':'whole_passenger_car','eligibility_review':{'status':'allowed','description_complete':True},
        'vehicle_key':'vin-sha256:'+hashlib.sha256(vin.strip().upper().encode()).hexdigest(),
        'vehicle_identity_verified':True,'identity_review':{'distinct_photos_reviewed':False},
        'reference_provenance':deepcopy(provenance)}
    out['ria_reference_evidence']={'version':VERSION,'http_status':200,'method':'auto/info','binding':binding(out)}
    if reasons(out):raise ValueError('reference_provenance_not_admissible')
    return out
