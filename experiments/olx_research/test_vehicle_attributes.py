"""Synthetic evidence-boundary regressions, never real VINs or sale labels."""
import copy,json
import pytest
from experiments.olx_offline.test_observed_price_provenance import AD,page,NOW
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from .observations import enrich
from .valuation import estimate
from .test_research import car,peers,EPOCH

FAKE_VIN='WVWZZZ1JZXW000001'

def vehicle_page(*,visible_drive='Передний',state_drive='front',power='105',visible_power='105 л.с.',vin=FAKE_VIN,visible_vin=FAKE_VIN,wrong_id=False):
    ad=copy.deepcopy(AD)
    ad['params'] += [
        {'key':'drive_type','name':'Тип привода','value':'Передний','normalizedValue':state_drive},
        {'key':'power','name':'Мощность','value':visible_power,'normalizedValue':power},
        {'key':'vin_number','name':'VIN','value':vin,'normalizedValue':vin}]
    if wrong_id:ad['id']=999
    raw=page(ad=ad)
    extra=f'<p>Тип привода: {visible_drive}</p><p>Мощность: {visible_power}</p><p>VIN: {visible_vin}</p>'
    return raw.replace(b'</html>',extra.encode()+b'</html>')

def parsed(**kw):
    raw=vehicle_page(**kw)
    return enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']


def test_drive_power_and_vehicle_hash_require_matching_visible_state():
    c=parsed()
    assert (c.get('drive_type'),c.get('power_hp'))==('front',105)
    assert c.get('vehicle_key') and c['vehicle_key'].startswith('vin-sha256:')
    assert FAKE_VIN not in json.dumps(c)
    assert c['vehicle_identity_verified'] is False

@pytest.mark.parametrize('kw',[
    {'visible_drive':'Полный'}, {'state_drive':'full'}, {'wrong_id':True}])
def test_conflicting_identity_or_drive_cannot_supply_a_matching_attribute(kw):
    c=parsed(**kw)
    assert c.get('drive_type') is None
    assert c.get('attribute_review',{}).get('issues')


def test_power_units_and_vin_masking_are_not_guessed():
    c=parsed(power='77',visible_power='77 кВт',vin='***************',visible_vin='***************')
    assert c.get('power_hp') is None and c.get('vehicle_key') is None
    assert parsed(vin=FAKE_VIN,visible_vin='WVWZZZ1JZXW000002').get('vehicle_key') is None


def test_known_drive_and_power_mismatches_do_not_fill_market_minimum():
    target=car(drive_type='front',power_hp=105)
    candidates=[{**c,'drive_type':'front','power_hp':105} for c in peers()]
    candidates[0]['drive_type']='full';candidates[1]['power_hp']=160
    r=estimate(target,candidates,None,EPOCH)
    assert r['status']=='profitability_unconfirmed' and r['sample']==6
    assert r['exclusions']['mismatch_drive_type']==1 and r['exclusions']['mismatch_power_hp']==1
    candidates[2].pop('power_hp')
    assert 'missing_power_hp' in estimate(target,candidates,None,EPOCH)['exclusions']


def test_source_claimed_vin_dedups_without_claiming_verified_physical_independence():
    target=car(vehicle_key='claim-target',vehicle_identity_verified=False)
    candidates=[{**c,'vehicle_key':'claim-'+c['id'],'vehicle_identity_verified':False} for c in peers()]
    assert estimate(target,candidates,None,EPOCH)['independent_vehicles_verified'] is None


def test_independent_vehicle_count_is_recomputed_after_outlier_removal():
    target=car(vehicle_key='verified-target',vehicle_identity_verified=True)
    candidates=[{**c,'vehicle_key':'verified-'+c['id'],'vehicle_identity_verified':True} for c in peers()]
    candidates.append(car('outlier',99999,vehicle_key='verified-outlier',vehicle_identity_verified=True))
    result=estimate(target,candidates,None,EPOCH)
    assert result['sample']==8 and result['independent_vehicles_verified']==8


def test_only_manufacturer_supported_a5_hatchback_liftback_alias_is_compared():
    target=car(body='liftback')
    candidates=[{**c,'body':'liftback'} for c in peers()]
    candidates[0]['body']='hatchback'
    r=estimate(target,candidates,None,EPOCH)
    assert r['sample']==8 and r['status']=='experimental_asking_estimate'
    assert r['body_policy']['id']=='skoda-octavia-a5-body-v1'
    assert candidates[0]['body']=='hatchback' # preserve source and filter fields
    candidates[0]['body']='sedan'
    assert estimate(target,candidates,None,EPOCH)['sample']==7
    target['generation']='OTHER'
    for c in candidates:c['generation']='OTHER'
    candidates[0]['body']='hatchback'
    assert estimate(target,candidates,None,EPOCH)['sample']==7


def test_explicit_description_power_requires_both_complete_dom_and_state():
    ad=copy.deepcopy(AD);ad['description']='Авто продається цілим. Потужність 160 к.с.'
    raw=page(ad=ad,description='<h3>Описание</h3>'+ad['description'])
    c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert c.get('power_hp')==160
    ad['description']='Авто продається цілим. Потужність 190 к.с.'
    raw=page(ad=ad,description='Потужність 160 к.с.')
    c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert c.get('power_hp') is None


def test_description_tuning_power_cannot_silently_conflict_with_structured_power():
    ad=copy.deepcopy(AD);ad['description']='Після тюнінгу 230 л.с.'
    ad['params'].append({'key':'power','name':'Мощность','value':'220 л.с.','normalizedValue':'220'})
    raw=page(ad=ad,description=ad['description']).replace(b'</html>','<p>Мощность: 220 л.с.</p></html>'.encode())
    c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert c.get('power_hp') is None
    assert 'power_hp_description_conflict' in c['attribute_review']['issues']


def test_known_modification_conflict_cannot_fill_market_minimum():
    target=car(modification='1.6 mpi')
    candidates=[{**c,'modification':'1.6 mpi'} for c in peers()]
    candidates[0]['modification']='1.6 fsi'
    result=estimate(target,candidates,None,EPOCH)
    assert result['sample']==7
    assert result['exclusions']['mismatch_modification']==1
    assert result['status']=='profitability_unconfirmed'
