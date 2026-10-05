"""Offline proof of owner-only probe access, persistence and budget boundaries."""
from dataclasses import replace
from copy import deepcopy
import time
import pytest
from sqlalchemy.orm import Session
from backend import olx_ria_probe as probe, olx_owner_feed as feed
from backend.app import Settings
from backend.models import Filters, SourceProbe, User, Search
from backend.manual_payment_models import PaymentRequest
from backend.tests.test_olx_isolated_integration import bench, client


@pytest.fixture
def live(bench,monkeypatch):
    client(bench,900);client(bench,100)
    monkeypatch.setenv('SUBSCRIPTION_EXPECTED_ADMIN_ID','900')
    monkeypatch.setenv('OLX_RIA_PROBE_ENABLED','true')
    monkeypatch.setattr(probe.time,'time',lambda:bench.clock[0])
    monkeypatch.setattr(probe.time,'sleep',lambda _:None)
    bench.settings=Settings(str(bench.engine.url),'test-token','test-secret-'*4,
        source_ready=True,delivery_enabled=True,admin_telegram_id=900,
        auto_ria_api_key='test-key',auto_ria_user_id='123')
    bench.plan={'search_id':900,'fingerprint':Filters().fingerprint(),
        'until':bench.clock[0]+1000,'maximum_olx_gets':2,
        'candidates':[{'id':'936768428','url':'https://www.olx.ua/d/uk/obyavlenie/skoda-octavia-a5-ID11oAgc.html'}]}
    return bench


@pytest.mark.parametrize('reason',['off','other_owner','unpaid','expired','not_ready','search_off','wrong_fingerprint','deadline','olx_stop'])
def test_blocked_owner_never_requests_or_initializes(live,monkeypatch,reason):
    if reason=='off':monkeypatch.setenv('OLX_RIA_PROBE_ENABLED','false')
    elif reason=='other_owner':live.settings=replace(live.settings,admin_telegram_id=100)
    elif reason=='wrong_fingerprint':live.plan['fingerprint']='wrong'
    elif reason=='deadline':live.plan['until']=live.clock[0]
    else:
        with Session(live.engine) as db:
            if reason=='unpaid':db.delete(db.get(PaymentRequest,'test-900'))
            elif reason=='expired':db.get(PaymentRequest,'test-900').expires_at=live.clock[0]
            elif reason=='not_ready':db.get(User,900).ready=False
            elif reason=='search_off':db.get(Search,900).enabled=False
            elif reason=='olx_stop':db.add(SourceProbe(id=feed.STATE,status='paused_by_owner',result={},requests=0,checked_at=live.clock[0]))
            db.commit()
    calls=[]
    probe.run_once(live.engine,live.settings,live.plan,fetch=lambda *a:calls.append(a))
    assert not calls
    with Session(live.engine) as db:assert db.get(SourceProbe,probe.STATE) is None


def catalogs(path):
    if path.endswith('/marks'):return [{'value':70,'name':'Skoda'}]
    if path.endswith('/models'):return [{'value':652,'name':'Octavia'}]
    if path.endswith('/bodystyles'):return [{'value':2,'name':'Універсал'}]
    if path.endswith('/type'):return [{'value':1,'name':'Бензин'}]
    if path.endswith('/gearboxes'):return [{'value':1,'name':'Ручна / Механіка'}]
    return [{'value':1,'name':'example'}]


def test_completed_probe_has_no_sender_no_readiness_and_never_replays(live,monkeypatch):
    calls=[]
    car=dict(source='olx',id='936768428',url=live.plan['candidates'][0]['url'],
        body='wagon',fuel='petrol',transmission='manual',year=2005,mileage_km=254000,
        engine_cc=1600,power_hp=102,price='5100',currency='USD',checked_at=live.clock[0],
        research_condition='seller_declared_running')
    monkeypatch.setattr(probe,'parse_detail_snapshot',lambda *a,**k:{'listing':deepcopy(car)})
    monkeypatch.setattr(probe,'enrich',lambda raw,p:p)
    def fetch(settings,path,body,params):
        with Session(live.engine) as db:
            row=db.get(SourceProbe,probe.STATE)
            assert row.requests==len(calls)+1
            assert row.result['calls'][-1]['status']=='reserved'
        calls.append(path)
        if body:
            assert 'omniId' not in body['params']
            return 200,{'statisticData':[{'type':'avgPrice','price':{'USD':6000}}],
                'similarCars':[{'id':123,'VIN':'PRIVATE','fuel':{'name':'Бензин','VIN':'PRIVATE'}}]},100
        return 200,catalogs(path),100
    result=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch,olx_fetch=lambda u:(200,b'fake',False))
    assert result['ria_calls']==8 and result['ai_calls']==1 and result['olx_gets']==1
    assert result['technical_ready'] is False and result['telegram_calls']==0
    with Session(live.engine) as db:
        row=db.get(SourceProbe,probe.STATE)
        assert 'PRIVATE' not in str(row.result)
        assert row.result['observations'][0]['missing_criteria']
        assert row.result['observations'][0]['provider_range_fraction'] is None
    assert probe.run_once(live.engine,live.settings,live.plan,fetch=fetch) is None
    assert len(calls)==8


def test_stop_between_calls_cannot_spend_another_request(live):
    calls=[]
    def fetch(settings,path,body,params):
        calls.append(path)
        with Session(live.engine) as db:db.get(User,900).ready=False;db.commit()
        return 200,catalogs(path),10
    result=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch)
    assert result['ria_calls']==1 and len(calls)==1
    assert result['error']=='owner_access_or_stop_blocked'


@pytest.mark.parametrize('code',[401,403,429])
def test_provider_hold_is_persistent_and_not_retried(live,code):
    calls=[]
    def fetch(*args):calls.append(args[1]);return code,None,0
    result=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch)
    assert result['status']=='source_hold' and result['ria_calls']==1
    probe.run_once(live.engine,live.settings,live.plan,fetch=fetch)
    assert len(calls)==1


def test_reservations_remain_spent_and_total_and_ai_caps_hold(live):
    assert probe.initialize(live.engine,live.settings,live.plan)
    for i in range(6):probe.reserve(live.engine,live.settings,live.plan,'ai','auto/ai-avarage-price/',{})
    with pytest.raises(ValueError,match='budget_exhausted'):
        probe.reserve(live.engine,live.settings,live.plan,'ai','auto/ai-avarage-price/',{})
    for i in range(24):probe.reserve(live.engine,live.settings,live.plan,'dictionary','auto/type')
    with pytest.raises(ValueError,match='budget_exhausted'):
        probe.reserve(live.engine,live.settings,live.plan,'dictionary','auto/type')
    with Session(live.engine) as db:assert db.get(SourceProbe,probe.STATE).requests==30


def test_documented_generation_shape_preserves_generation_not_model_id():
    data=[{'id':652,'name':'Octavia','generations':[{'generationId':123,'name':'II/A5'}]}]
    assert probe.catalog_rows(data)==[(123,'II/A5')]


def test_olx_receipt_survives_later_card_parse_failure(live,monkeypatch):
    def fetch(settings,path,body,params):return 200,catalogs(path),20
    def bad_parse(*args,**kwargs):raise ValueError('parse_failed')
    monkeypatch.setattr(probe,'parse_detail_snapshot',bad_parse)
    result=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch,
                          olx_fetch=lambda u:(200,b'complete-but-unparseable',False))
    assert result['error']=='parse_failed'
    with Session(live.engine) as db:
        receipt=db.get(SourceProbe,probe.STATE).result['olx_calls'][0]
        assert receipt['status']=='http_200' and receipt['bytes']==24


def test_reviewed_gas_correction_reuses_spent_ledger_and_never_defaults_fuel(live,monkeypatch):
    car=dict(source='olx',id='936768428',url=live.plan['candidates'][0]['url'],
        body='wagon',fuel='gas_petrol',transmission='manual',year=2005,mileage_km=254000,
        engine_cc=1600,power_hp=102,price='5100',currency='USD',checked_at=live.clock[0],
        research_condition='seller_declared_running',generation='A5',drive_type='front')
    monkeypatch.setattr(probe,'parse_detail_snapshot',lambda *a,**k:{'listing':deepcopy(car)})
    monkeypatch.setattr(probe,'enrich',lambda raw,p:p)
    calls=[]
    def fetch(settings,path,body,params):
        calls.append(path)
        if body:
            assert 'fuelId' not in body['params']
            assert body['params']['generationId']=='3133' and body['params']['driveId']=='2'
            return 200,{'statisticData':[{'type':'avgPrice','price':{'USD':6000}}]},40
        return 200,catalogs(path),40
    first=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch,olx_fetch=lambda u:(200,b'x',False))
    assert first['ria_calls']==7 and first['ai_calls']==0
    assert first['error']=='dictionary_mapping_ambiguous_or_missing'
    live.plan['phase']=2
    paths={'brands':'auto/categories/1/marks','models':'auto/categories/1/marks/70/models',
        'body':'auto/categories/1/bodystyles','fuel':'auto/type','gear':'auto/categories/1/gearboxes',
        'drive':'auto/categories/1/driverTypes','generation':'generations/by/models/652/generations'}
    live.plan['dictionary_evidence']={k:{'path':p,'checked_at':live.clock[0],
        'receipt_log_id':'synthetic-receipt','items':probe.catalog_rows(catalogs(p))} for k,p in paths.items()}
    live.plan['dictionary_evidence']['generation']['items']=[(3133,'II покоління/A5')]
    live.plan['dictionary_evidence']['drive']['items']=[(2,'Передній')]
    second=probe.run_once(live.engine,live.settings,live.plan,fetch=fetch,olx_fetch=lambda u:(200,b'x',False))
    assert second['ria_calls']==8 and second['ai_calls']==1 and second['olx_gets']==2
    assert second['technical_ready'] is False and len(calls)==8
    with Session(live.engine) as db:
        r=db.get(SourceProbe,probe.STATE)
        assert r.result['phase']==2
        assert 'fuel_subtype_mapping' in r.result['observations'][0]['missing_criteria']
    probe.run_once(live.engine,live.settings,live.plan,fetch=fetch)
    assert len(calls)==8
