"""Offline owner delivery boundaries, reusing the real confirmed purchase policy."""
from dataclasses import replace
from copy import deepcopy
import pytest
from sqlalchemy.orm import Session
from backend import olx_owner_feed as feed
from backend.app import Settings
from backend.models import User, Search, SourceProbe
from backend.manual_payment_models import PaymentRequest
from backend.tests.test_olx_isolated_integration import bench, client


@pytest.fixture
def live(bench, monkeypatch):
    client(bench, 900); client(bench, 100)
    monkeypatch.setenv('SUBSCRIPTION_EXPECTED_ADMIN_ID', '900')
    monkeypatch.setattr(feed.time, 'time', lambda: bench.clock[0])
    bench.settings=Settings(str(bench.engine.url),'test-token','test-secret-'*4,
        delivery_enabled=True,source_ready=True,admin_telegram_id=900,
        olx_owner_feed_enabled=True, olx_owner_feed_search_id=900,
        olx_owner_feed_until=bench.clock[0]+7200)
    return bench


@pytest.mark.parametrize('why',['other_owner','unpaid','pending_payment','expired_payment','stop','search_off','unpinned_search','not_strict'])
def test_guard_uses_exact_paid_owner_and_one_current_pinned_search(live,monkeypatch,why):
    if why=='other_owner': live.settings=replace(live.settings,admin_telegram_id=100)
    elif why=='unpinned_search': live.settings=replace(live.settings,olx_owner_feed_search_id=100)
    elif why=='not_strict': live.engine.update_execution_options(autodeal_confirmed_paid_sources_only=False)
    else:
        with Session(live.engine) as db:
            p=db.get(PaymentRequest,'test-900')
            if why=='unpaid':db.delete(p)
            elif why=='pending_payment':p.state='review'
            elif why=='expired_payment':p.expires_at=live.clock[0]
            elif why=='stop':db.get(User,900).ready=False
            elif why=='search_off':db.get(Search,900).enabled=False
            db.commit()
    assert not feed.current_search(live.engine,live.settings)
    calls=[]
    assert feed.tick(live.engine,live.settings,fetch=lambda *a:calls.append(a),sender=lambda *a,**k:calls.append(a))=='access_or_search_blocked'
    assert calls==[]


def test_disabled_diagnostic_reads_own_searches_without_transport_or_mutation(live):
    live.settings=replace(live.settings,olx_owner_feed_enabled=False)
    with Session(live.engine) as db:
        before=[(s.id,s.filters,s.enabled) for s in db.query(Search).all()]
    result=feed.preflight(live.engine,live.settings)
    assert result['owner_verified'] and result['confirmed_paid'] and result['ready']
    assert len(result['own_searches'])==1 and result['own_searches'][0]['id']==900
    assert result['enabled'] is False
    assert result['recipient_count']==1
    with Session(live.engine) as db:
        assert [(s.id,s.filters,s.enabled) for s in db.query(Search).all()]==before
        assert db.get(SourceProbe,feed.STATE) is None


def test_no_verified_profile_means_no_source_or_telegram_calls(live):
    calls=[]
    assert feed.tick(live.engine,live.settings,profile={},fetch=lambda *a:calls.append(a),sender=lambda *a,**k:calls.append(a))=='technical_data_pending'
    assert not calls


def test_permission_does_not_satisfy_market_readiness():
    evidence={'owner_authorized':True,'client_authorized':False,'minimum':8,
        'estimated':0,'max_comparables':4,'holdout_estimated':0,
        'independent_vehicles':0,'source_reviewed':False,'stable':False,'selected_method':None}
    result=feed.owner_readiness(evidence)
    assert result['technical_ready'] is False and result['owner_ready'] is False
    assert result['client_ready'] is False
    assert 'insufficient_compatible_analogs' in result['blockers']


def test_client_authorization_is_never_inferred_from_owner():
    r=feed.owner_readiness({'owner_authorized':True,'minimum':8,'estimated':3,
        'max_comparables':10,'holdout_estimated':3,'independent_vehicles':10,
        'source_reviewed':True,'stable':True,'selected_method':'median','method_version':'checked-real-v1'})
    assert r['technical_ready'] and r['owner_ready'] and not r['client_ready']

# Synthetic full cohorts below exercise the boundary; they are never live proof.
import hashlib
import json
from decimal import Decimal
from datetime import date, datetime, timezone
from backend.olx_market.fx_policy import Quote, RateBook, normalize, privat_url


def synthetic_car(live, id='target', price='6000', **extra):
    car=dict(source='olx', id=id, price=price, currency='USD', title='Skoda Octavia A5',
        brand='Skoda', model='Octavia', generation='A5', year=2008, body='wagon',
        fuel='diesel', transmission='manual', engine_cc=1900, drive_type='front', power_hp=105,
        mileage_km=300000, region='Хмельницька область', research_condition='seller_declared_running',
        category='whole_passenger_car', checked_at=int(live.clock[0]),
        eligibility_review={'status':'allowed'}, price_review={'reasons':['full_price_unconfirmed']},
        observed_asking_display={'status':'corroborated_display','amount':price,'currency':'USD',
            'description_reviewed_in_full':True,'reasons':[]},field_conflicts=[],price_conflicts=[],photos=[],
        url='https://www.olx.ua/d/uk/obyavlenie/fixture-ID'+id+'.html',
        vehicle_key='vin-sha256:'+hashlib.sha256(id.encode()).hexdigest(),
        identity_review={'distinct_photos_reviewed':True})
    car.update(extra)
    return car


def synthetic_profile(live):
    refs=[synthetic_car(live,'r'+str(i),str(p)) for i,p in enumerate((7800,7900,8000,8050,8100,8150,8200,8300))]
    holdout=[synthetic_car(live,'c'+str(i),'8075') for i in range(3)]
    membership={c['id']:'reference' for c in refs}|{c['id']:'holdout' for c in holdout}
    return {'dataset_kind':'saved_real','owner_authorized':True,'source_review':{'bounded_public_channel_reviewed':True},
        'reference':refs,'holdout':holdout,'selected_method':'median','method_version':'fixture-v1',
        'quote':None,'split_freeze':{'commit':'a'*40,'frozen_at':int(live.clock[0])-1,
          'membership_sha256':hashlib.sha256(json.dumps(membership,sort_keys=True,separators=(',',':')).encode()).hexdigest()},
        'candidate_urls':[]}


def owned_cycle(live):
    search=feed.current_search(live.engine,live.settings)
    feed.initialize(live.engine,live.settings,search)
    return feed.claim(live.engine)


def test_recomputes_control_scores_and_keeps_eight_minimum(live):
    profile=synthetic_profile(live)
    assert feed.profile_ready(profile,live.clock[0])
    target=synthetic_car(live)
    search=feed.current_search(live.engine,live.settings)
    proof=feed.assessment_for(target,profile,search,live.clock[0])
    assert proof and proof[1]['sample']==8 and Decimal(proof[1]['reference_usd'])==Decimal('8075')
    profile['reference'].pop()
    assert not feed.profile_ready(profile,live.clock[0])
    assert feed.assessment_for(target,profile,search,live.clock[0]) is None


@pytest.mark.parametrize('failure',['duplicate_vehicle','duplicate_id','missing_photos_review','invalid_key','late_freeze','wrong_method','insufficient_controls','stale','permission_only'])
def test_readiness_rejects_invalid_or_leaking_real_profile(live,failure):
    p=synthetic_profile(live)
    if failure=='duplicate_vehicle':p['reference'][0]['vehicle_key']=p['holdout'][0]['vehicle_key']
    elif failure=='duplicate_id':p['reference'][0]['id']=p['holdout'][0]['id']
    elif failure=='missing_photos_review':p['reference'][0]['identity_review']={}
    elif failure=='invalid_key':p['reference'][0]['vehicle_key']='vin-sha256:anything'
    elif failure=='late_freeze':p['split_freeze']['frozen_at']=int(live.clock[0])+1
    elif failure=='wrong_method':p['selected_method']='weighted_median'
    elif failure=='insufficient_controls':p['holdout'].pop()
    elif failure=='stale':p['reference'][0]['checked_at']-=86401
    elif failure=='permission_only':p={'owner_authorized':True,'technical_ready':True}
    assert not feed.profile_ready(p,live.clock[0])


@pytest.mark.parametrize('response_kind',['accepted','rejected','timeout','wrong_chat','wrong_type','stop_before_send'])
def test_exact_chat_receipt_and_no_retry_or_photo_fallback(live,response_kind):
    profile=synthetic_profile(live);token,state=owned_cycle(live)
    car=synthetic_car(live,photos=['https://images.example.test/car.jpg'])
    calls=[]
    def sender(token,method,payload,**kw):
        calls.append((method,payload))
        if method=='getChat':
            if response_kind=='stop_before_send':
                with Session(live.engine) as db:
                    db.get(User,900).ready=False;db.commit()
            return {'ok':True,'result':{'id':100 if response_kind=='wrong_chat' else 900,
                'type':'group' if response_kind=='wrong_type' else 'private'}}
        if response_kind=='timeout':raise TimeoutError('ambiguous')
        if response_kind=='rejected':return {'ok':False,'error_code':400}
        return {'ok':True,'result':{'message_id':123,'chat':{'id':900,'type':'private'}}}
    assert feed.send_card(live.engine,live.settings,token,state,car,profile,sender)==(response_kind=='accepted')
    assert all(p['chat_id']==900 for _,p in calls)
    sends=[c for c in calls if c[0] in ('sendPhoto','sendMessage')]
    assert len(sends)==(0 if response_kind in ('wrong_chat','wrong_type','stop_before_send') else 1)
    if sends:
        payload=sends[0][1]
        assert 'OLX • тест лише для власника' in payload.get('caption',payload.get('text',''))
        assert payload['reply_markup']['inline_keyboard'][0][0]['url']==car['url']
    before=list(calls)
    assert not feed.send_card(live.engine,live.settings,token,state,car,profile,sender)
    assert calls==before
    with Session(live.engine) as db:
        r=db.get(SourceProbe,feed.STATE).result['receipts'][0]
        assert r['status']=={'accepted':'accepted','rejected':'rejected','timeout':'uncertain'}.get(response_kind,'not_attempted')


def test_old_batch_ledger_prevents_replay_and_other_search_is_untouched(live):
    profile=synthetic_profile(live);token,state=owned_cycle(live);car=synthetic_car(live)
    key='olx-owner-car-'+hashlib.sha256(('900:'+car['id']).encode()).hexdigest()[:32]
    with Session(live.engine) as db:
        before=(db.get(Search,100).filters,db.get(User,100).ready)
        db.add(SourceProbe(id=key,status='uncertain',checked_at=live.clock[0],requests=1,result={'source_id':car['id']}));db.commit()
    assert not feed.send_card(live.engine,live.settings,token,state,car,profile,lambda *a,**kw:pytest.fail('old packet replay'))
    with Session(live.engine) as db:assert before==(db.get(Search,100).filters,db.get(User,100).ready)


def test_olx_stop_changes_only_new_source_state(live):
    token,state=owned_cycle(live)
    event={'message':{'chat':{'id':900,'type':'private'},'from':{'id':900},'text':'/olx_stop'}}
    assert feed.handle(live.engine,live.settings,event)
    assert not feed.current_search(live.engine,live.settings)
    with Session(live.engine) as db:
        assert db.get(User,900).ready and db.get(Search,900).enabled
        assert db.get(SourceProbe,feed.STATE).status=='paused_by_owner'
        assert db.get(PaymentRequest,'test-900').state=='approved'
    event['message']['from']['id']=100
    assert feed.handle(live.engine,live.settings,event) is None


def test_persistent_source_hold_isolated_from_paid_searches(live):
    p=synthetic_profile(live);car=synthetic_car(live)
    p['candidate_urls']=[{'id':car['id'],'url':car['url']}]
    calls=[]
    assert feed.tick(live.engine,live.settings,profile=p,fetch=lambda url:(calls.append(url) or (403,b'blocked',False)))=='cycle_complete'
    assert len(calls)==1
    assert feed.tick(live.engine,live.settings,profile=p,fetch=lambda *a:pytest.fail('held source retry'))=='access_or_search_blocked'
    with Session(live.engine) as db:
        assert db.get(SourceProbe,feed.STATE).status=='source_hold'
        assert db.get(User,900).ready and db.get(Search,900).enabled and db.get(Search,100).enabled


def test_current_fx_fallback_keeps_usd_and_shared_currency_basis(tmp_path):
    at=datetime.fromisoformat('2026-10-05T10:00:00+03:00');calls=[]
    body=json.dumps({'date':'05.10.2026','bank':'PB','baseCurrency':980,'baseCurrencyLit':'UAH',
        'exchangeRate':[{'baseCurrency':'UAH','currency':'USD','purchaseRateNB':'45','saleRateNB':'45'}]}).encode()
    def transport(url):
        calls.append(url)
        return (200,body) if url==privat_url(date(2026,10,5)) else (403,b'blocked')
    book=RateBook(tmp_path/'fx.sqlite')
    quote,trace=book.select(at,transport)
    assert len(calls)==2 and quote.rate==Decimal('45')
    assert normalize({'price':'450000','currency':'UAH'},quote,at)['usd_amount']=='10000'
    assert normalize({'price':'10000','currency':'USD'},quote,at)['usd_amount']=='10000'
    assert normalize({'price':'10000','currency':'???'},quote,at)['status']=='pending'
    assert book.select(at,transport)[0].basis==quote.basis and len(calls)==2
    book.close()

@pytest.mark.parametrize('actual',['Хмельницкая область','Хмельницька область','Винницкая область','Тернопольская область','Черновицкая область'])
def test_observed_olx_russian_region_names_match_saved_ukrainian_regions(live,actual):
    from backend.olx_market.candidates import filter_reasons
    c=synthetic_car(live,region=actual)
    f={'region':['вінницька','тернопільська','хмельницька','чернівецька']}
    assert filter_reasons(c,f,None,live.clock[0])['match']

def test_region_translation_does_not_expand_saved_geography(live):
    from backend.olx_market.candidates import filter_reasons
    f={'region':['вінницька','тернопільська','хмельницька','чернівецька']}
    assert not filter_reasons(synthetic_car(live,region='Львовская область'),f,None,live.clock[0])['match']
    assert filter_reasons(synthetic_car(live,region=None),f,None,live.clock[0])['unknown']==['missing_region']


def test_independent_claim_get_caps_and_three_attempt_cap(live):
    token,state=owned_cycle(live)
    assert feed.claim(live.engine) is None
    for _ in range(feed.MAX_HOURLY_GETS):assert feed.reserve_get(live.engine,live.settings,token,state)
    assert not feed.reserve_get(live.engine,live.settings,token,state)
    with Session(live.engine) as db:
        s=db.get(SourceProbe,feed.STATE)
        assert s.requests==12 and s.result['day_bytes']==12*feed.DETAIL_CAP
        s.result={**s.result,'attempts':3};db.commit()
    p=synthetic_profile(live)
    assert not feed.send_card(live.engine,live.settings,token,state,synthetic_car(live),p,lambda *a,**kw:pytest.fail('packet exceeded'))


@pytest.mark.parametrize('hour,expected',[(0,3600),(7,3600),(8,600),(22,600),(23,3600)])
def test_olx_own_night_interval(hour,expected):
    from zoneinfo import ZoneInfo
    at=datetime(2026,10,5,hour,tzinfo=ZoneInfo('Europe/Kyiv')).timestamp()
    assert feed.interval(at)==expected
