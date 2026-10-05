"""Synthetic cross-source contracts, not live cards or price accuracy proof."""
from copy import deepcopy
from decimal import Decimal
import hashlib,json
import pytest
from backend.olx_market import ria_reference as ria
from backend import olx_owner_feed as feed
from backend.tests.test_olx_owner_feed import live, synthetic_car
from backend.tests.test_olx_isolated_integration import bench


def info(sid='40123456',price=8075,vin='TMBAB1234A1234567'):
    return {'USD':price,'VIN':vin,'title':'Skoda Octavia A5 FL 1.9 TDI',
        'linkToView':'/auto_skoda_octavia_'+sid+'.html','markId':70,'modelId':652,
        'markName':'Skoda','modelName':'Octavia','subCategoryName':'Універсал',
        'technicalCondition':{'id':1},
        'autoInfoBar':{'damage':False,'onRepairParts':False,'abroad':False,'custom':False},
        'autoData':{'autoId':int(sid),'active':True,'isSold':False,'statusId':0,'year':2008,
            'categoryId':1,'bodyId':2,'fuelId':2,'fuelName':'Дизель, 1.9 л.',
            'gearBoxId':1,'gearboxName':'Ручна / Механіка','generationId':3607,
            'driveId':2,'raceInt':300,'modificationId':123,'modificationName':'1.9 TDI MT (105 к.с.)',
            'description':'Продаю автомобіль цілим. На ходу, технічно справний. Українська реєстрація.'}}


def provenance(now,role='reference'):
    return {'role':role,'frozen_commit':'b'*40,'frozen_at':int(now)-1,
        'search_params':{'category_id':1,'marka_id[0]':70,'model_id[0]':652}}


def test_raw_info_projection_retains_real_price_and_no_private_data():
    raw=info();raw['phone']='DO_NOT_RETAIN'
    car=ria.from_full_info(raw,'40123456',10000,provenance(10000))
    assert car['source']=='auto_ria' and car['price']=='8075' and car['currency']=='USD'
    assert car['generation_variant']=='FL' and car['power_hp']==105
    assert car['vehicle_key']=='vin-sha256:'+hashlib.sha256(raw['VIN'].encode()).hexdigest()
    assert raw['VIN'] not in str(car) and 'DO_NOT_RETAIN' not in str(car)
    assert car['identity_review']['distinct_photos_reviewed'] is False
    assert ria.reasons(car)==[]


@pytest.mark.parametrize('why',['unknown_power','sold','damaged','unknown_drive','unknown_variant','no_description','unpaid_installment','masked_vin','parts'])
def test_incomplete_or_forbidden_full_cards_never_become_references(why):
    raw=info()
    if why=='unknown_power':raw['autoData']['modificationName']=None
    elif why=='sold':raw['autoData']['isSold']=True
    elif why=='damaged':raw['autoInfoBar']['damage']=True
    elif why=='unknown_drive':raw['autoData'].pop('driveId')
    elif why=='unknown_variant':raw['autoData']['generationId']=None
    elif why=='no_description':raw['autoData']['description']=''
    elif why=='unpaid_installment':raw['autoData']['description']='Ціна це перший внесок за автомобіль. Продаю цілим.'
    elif why=='masked_vin':raw['VIN']='TMBXXXXXXXXXXXXXX'
    elif why=='parts':raw['autoData']['description']='Авто на розбір, продається частинами.'
    with pytest.raises(Exception):ria.from_full_info(raw,'40123456',10000,provenance(10000))


@pytest.mark.parametrize('cap',['price_ot','state_id[0]','city_id[0]'])
def test_owner_price_and_region_capped_search_is_not_market_sample(cap):
    p=provenance(10000);p['search_params'][cap]=7000
    with pytest.raises(ValueError,match='provenance'):ria.from_full_info(info(),'40123456',10000,p)


def profile(live):
    now=int(live.clock[0]);refs=[];controls=[]
    for i,p in enumerate((7800,7900,8000,8050,8100,8150,8200,8300,8075,8075,8075)):
        sid=str(40123456+i);role='reference' if i<8 else 'holdout'
        raw=info(sid,p,'TMBAB1234A'+str(1234567+i))
        c=ria.from_full_info(raw,sid,now,provenance(now,role))
        c['identity_review']={'distinct_photos_reviewed':True} # Explicitly synthetic review.
        (refs if i<8 else controls).append(c)
    membership={c['id']:'reference' for c in refs}|{c['id']:'holdout' for c in controls}
    return {'dataset_kind':'saved_real','owner_authorized':True,
        'source_review':{'bounded_public_channel_reviewed':True},'reference':refs,'holdout':controls,
        'selected_method':'median','method_version':'synthetic-contract-v1','quote':None,
        'split_freeze':{'commit':'b'*40,'frozen_at':now-1,
            'membership_sha256':hashlib.sha256(json.dumps(membership,sort_keys=True,separators=(',',':')).encode()).hexdigest()}}


def test_ria_full_references_can_pass_same_eight_and_three_control_contract(live):
    p=profile(live)
    assert feed.profile_ready(p,live.clock[0])
    target=synthetic_car(live,generation_variant='FL')
    proof=feed.assessment_for(target,p,feed.current_search(live.engine,live.settings),live.clock[0])
    assert proof and proof[1]['sample']==8 and Decimal(proof[1]['reference_usd'])==Decimal('8075')
    assert proof[1]['extra_ria_margin_percent']=='0'
    p['reference'].pop()
    assert not feed.profile_ready(p,live.clock[0])


@pytest.mark.parametrize('why',['no_receipt','changed_price','unreviewed_photos','seven','two_controls','known_target_crosspost'])
def test_permission_or_metadata_cannot_override_reference_proof(live,why):
    p=profile(live);target=synthetic_car(live,generation_variant='FL')
    if why=='no_receipt':p['reference'][0].pop('ria_reference_evidence')
    elif why=='changed_price':p['reference'][0]['price']='12345'
    elif why=='unreviewed_photos':p['reference'][0]['identity_review']['distinct_photos_reviewed']=False
    elif why=='seven':p['reference'].pop()
    elif why=='two_controls':p['holdout'].pop()
    else:target['vehicle_key']=p['reference'][0]['vehicle_key']
    if why=='known_target_crosspost':
        assert feed.assessment_for(target,p,feed.current_search(live.engine,live.settings),live.clock[0]) is None
    else:assert not feed.profile_ready(p,live.clock[0])


def test_unknown_or_conflicting_fl_variant_cannot_use_ria_reference(live):
    p=profile(live)
    for variant in (None,'pre_FL'):
        target=synthetic_car(live,generation_variant=variant)
        assert feed.assessment_for(target,p,feed.current_search(live.engine,live.settings),live.clock[0]) is None


@pytest.mark.parametrize('title,variant',[
    ('Skoda Octavia A5 FL 2010','FL'),('Шкода Октавія A5 рестайлінг','FL'),
    ('Octavia A5 дорестайлінг','pre_FL'),('Octavia A5 pre-FL','pre_FL'),
    ('Octavia A5 2010',None),('Octavia A5 не FL',None),
    ('Octavia A5 дорестайлінг FL',None)])
def test_visible_variant_never_comes_from_year_or_negated_claim(title,variant):
    from backend.olx_market.observations import enrich
    parsed={'summary':{'download_truncated':False},'listing':{'source':'olx',
        'brand':'Skoda','model':'Octavia','title':title}}
    car=enrich(b'<html></html>',parsed)['listing']
    assert car.get('generation_variant')==variant


def test_full_price_negation_is_not_an_installment():
    raw=info();raw['autoData']['description']='Продаю цілим. Ціна повна, не перший внесок. Автомобіль на ходу.'
    car=ria.from_full_info(raw,'40123456',10000,provenance(10000))
    assert ria.reasons(car)==[]


def test_comparison_receipts_keep_source_and_link_and_caption(live):
    p=profile(live);target=synthetic_car(live,generation_variant='FL')
    price,a=feed.assessment_for(target,p,feed.current_search(live.engine,live.settings),live.clock[0])
    assert all(c['source']=='auto_ria' and c['url'].startswith('https://auto.ria.com/auto_') for c in a['used_comparables'])
    assert 'Джерело аналогів: AUTO.RIA.' in feed.caption(target,price,a,'median')
