from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest
from sqlalchemy.orm import Session
from backend import olx_focused_provider as focus,olx_ria_probe as probe
from backend.models import SourceProbe,User
from backend.tests.test_olx_ria_probe import live,bench,client


def car(now):
    return dict(source='olx',id='936658970',url='https://www.olx.ua/d/uk/obyavlenie/skoda-octavia-a5-1-6-tdi-ID11o7MK.html',brand='Skoda',model='Octavia',year=2010,mileage_km=270000,engine_cc=1600,power_hp=105,fuel='diesel',transmission='manual',body='wagon',drive_type='front',generation='A5',generation_variant=None,research_condition='not_running',price='6500',currency='USD',checked_at=int(now),region='Хмельницька область',category='whole_passenger_car',eligibility_review={'status':'allowed'},observed_asking_display={'status':'corroborated_display','amount':'6500','currency':'USD','description_reviewed_in_full':True})


def mapping(c,now):
    return {k:{'value':c[k],'id':v,'dictionary_url':'https://developers.ria.com/auto/test','checked_at':int(now)} for k,v in [('brand',70),('model',652),('body',2),('fuel',2),('transmission',1),('drive_type',2)]}


def quote():return {'statisticData':[{'type':'avgPrice','price':{'USD':7386},'avgValueRange':.05,'quantityAdv':1841}],'similarCars':[{'id':40409242,'VIN':'PRIVATE'}]}
def search():return {'filters':{},'threshold':10}


@pytest.mark.parametrize('condition',[None,'not_running','running_body_repair','running_reported_damage:body_dents+windshield_crack'])
def test_condition_never_rejects_and_unknown_generation_not_invented(condition):
    c=car(1000);c['research_condition']=condition;r=focus.prepare(c,mapping(c,1000),1000)
    assert 'generationId' not in r['body']['params'] and 'damage' not in r['body']['params']
    a=focus.assess(quote(),r,c,search(),1000)
    assert not a['eligible_deal'] and a['reference_usd']=='6665.8650' and a['extra_margin_percent']==5
    assert a['independently_reviewed_compatible_analogs'] is None
    assert 'PRIVATE' not in json.dumps(a)


@pytest.mark.parametrize('mutation',['forbidden','stale','binding','quantity','currency','range','threshold','filter'])
def test_invalid_or_nonqualifying_estimates_cannot_send(mutation):
    c=car(1000);r=focus.prepare(c,mapping(c,1000),1000);q=quote();s=search()
    if mutation=='forbidden':c['eligibility_review']['status']='excluded'
    elif mutation=='stale':c['checked_at']=600
    elif mutation=='binding':c['price']='6400'
    elif mutation=='quantity':q['statisticData'][0]['quantityAdv']=7
    elif mutation=='currency':c['currency']='EUR'
    elif mutation=='range':q['statisticData'][0]['avgValueRange']=1
    elif mutation=='filter':s['filters']={'price_max':6000}
    elif mutation=='threshold':s['threshold']=20
    if mutation=='threshold':assert not focus.assess(q,r,c,s,1000)['eligible_deal']
    else:
        with pytest.raises(ValueError):focus.assess(q,r,c,s,1000)


def phase6(live,monkeypatch):
    monkeypatch.setenv(focus.ARM,'true')
    live.plan.update(phase=6,owner_authorized=True,client_authorized=False)
    with Session(live.engine) as db:
        db.add(SourceProbe(id=probe.STATE,status='completed_observations_not_admitted',requests=23,checked_at=live.clock[0],result={'phase':5,'error':None,'owner_id':900,'fingerprint':live.plan['fingerprint'],'calls':[{'kind':'ai' if i<3 else 'dictionary','status':'http_200','bytes':10} for i in range(23)],'olx_calls':[{'status':'http_200','bytes':10}]*5,'observations':[]}));db.commit()
    live.plan['maximum_olx_gets']=6
    live.plan['candidates']=[{'id':car(1000)['id'],'url':car(1000)['url']}]
    live.plan['dictionary_evidence']=json.loads(Path('backend/olx_ria_probe_plan.json').read_text())['dictionary_evidence']
    for e in live.plan['dictionary_evidence'].values():e['checked_at']=int(live.clock[0])


def test_phase6_explicit_arm_keeps_spent_budget_and_never_replays(live,monkeypatch):
    phase6(live,monkeypatch)
    monkeypatch.setattr(probe,'parse_detail_snapshot',lambda *a,**k:{'listing':car(live.clock[0])})
    monkeypatch.setattr(probe,'enrich',lambda raw,p:p)
    receipts=[];calls=[]
    monkeypatch.setattr(focus,'send',lambda *a:receipts.append(a[3]['id']) or {'source_id':a[3]['id'],'method':'sendMessage','status':'accepted'})
    result=probe.run_once(live.engine,live.settings,live.plan,fetch=lambda *a:(calls.append(a) or (200,{'statisticData':[{'type':'avgPrice','price':{'USD':9000},'avgValueRange':.05,'quantityAdv':20}]},100)),olx_fetch=lambda u:(200,b'fake',False))
    assert result['ria_calls']==24 and result['ai_calls']==4 and result['olx_gets']==6
    assert len(calls)==len(receipts)==1
    assert probe.run_once(live.engine,live.settings,live.plan,fetch=lambda *a:pytest.fail('restart I/O')) is None
    with Session(live.engine) as db:assert db.get(SourceProbe,probe.STATE).status=='completed_focused_owner'


@pytest.mark.parametrize('mutation',['off','other_owner','stop_after_getChat','timeout','accepted'])
def test_transport_owner_current_stop_unknown_and_dedup(live,monkeypatch,mutation):
    phase6(live,monkeypatch)
    c=car(live.clock[0]);r=focus.prepare(c,mapping(c,live.clock[0]),live.clock[0]);a=focus.assess({'statisticData':[{'type':'avgPrice','price':{'USD':9000},'avgValueRange':.05,'quantityAdv':20}]},r,c,search(),live.clock[0]);calls=[]
    if mutation=='off':monkeypatch.setenv(focus.ARM,'false')
    elif mutation=='other_owner':live.settings=replace(live.settings,admin_telegram_id=100)
    def sender(token,method,payload,timeout):
        calls.append(method);assert payload['chat_id']==900
        if method=='getChat':
            if mutation=='stop_after_getChat':
                with Session(live.engine) as db:db.get(User,900).ready=False;db.commit()
            return {'ok':True,'result':{'id':900,'type':'private'}}
        if mutation=='timeout':raise TimeoutError()
        return {'ok':True,'result':{'message_id':1,'chat':{'id':900,'type':'private'}}}
    out=focus.send(live.engine,live.settings,live.plan,c,a,probe.allowed,sender)
    if mutation in ('off','other_owner'):assert calls==[] and out is None
    elif mutation=='stop_after_getChat':assert calls==['getChat'] and out['status']=='not_attempted'
    else:assert len(calls)==2 and out['status']==('accepted' if mutation=='accepted' else 'uncertain')
    focus.send(live.engine,live.settings,live.plan,c,a,probe.allowed,sender)
    assert len(calls)<=2


def test_photo_timeout_has_no_text_fallback(live,monkeypatch):
    phase6(live,monkeypatch);c=car(live.clock[0]);c['photos']=['https://images.example/car.jpg'];r=focus.prepare(c,mapping(c,live.clock[0]),live.clock[0]);a=focus.assess({'statisticData':[{'type':'avgPrice','price':{'USD':9000},'avgValueRange':.05,'quantityAdv':20}]},r,c,search(),live.clock[0]);calls=[]
    def sender(token,method,payload,timeout):
        calls.append(method)
        if method=='getChat':return {'ok':True,'result':{'id':900,'type':'private'}}
        assert method=='sendPhoto';raise TimeoutError()
    out=focus.send(live.engine,live.settings,live.plan,c,a,probe.allowed,sender)
    assert calls==['getChat','sendPhoto'] and out['status']=='uncertain'
    assert focus.send(live.engine,live.settings,live.plan,c,a,probe.allowed,sender) is None


@pytest.mark.parametrize('mutation',['payment','olx_stop','arm'])
def test_final_payment_stop_and_disable_recheck(live,monkeypatch,mutation):
    from backend.manual_payment_models import PaymentRequest
    phase6(live,monkeypatch);c=car(live.clock[0]);r=focus.prepare(c,mapping(c,live.clock[0]),live.clock[0]);a=focus.assess({'statisticData':[{'type':'avgPrice','price':{'USD':9000},'avgValueRange':.05,'quantityAdv':20}]},r,c,search(),live.clock[0]);calls=[]
    def sender(token,method,payload,timeout):
        calls.append(method)
        assert method=='getChat'
        with Session(live.engine) as db:
            if mutation=='payment':db.get(PaymentRequest,'test-900').expires_at=live.clock[0]
            elif mutation=='olx_stop':db.add(SourceProbe(id=focus.feed.STATE,status='paused_by_owner',requests=0,checked_at=live.clock[0],result={}))
            else:monkeypatch.setenv(focus.ARM,'false')
            db.commit()
        return {'ok':True,'result':{'id':900,'type':'private'}}
    out=focus.send(live.engine,live.settings,live.plan,c,a,probe.allowed,sender)
    assert calls==['getChat'] and out['status']=='not_attempted'


def test_owner_corrected_lower_bound_then_five_percent_formula():
    c=car(1000);r=focus.prepare(c,mapping(c,1000),1000)
    q=quote();q['statisticData'][0].update(price={'USD':7386.315789473684},avgValueRange=.05)
    a=focus.assess(q,r,c,search(),1000)
    assert abs(float(a['reference_usd'])-6666.15)<.000001
    assert abs(float(a['discount_percent'])-2.492443164345)<.00001
    assert not a['eligible_deal']


def test_actual_sent_provider_quote_is_not_a_deal_under_correct_formula():
    c=car(1000);r=focus.prepare(c,mapping(c,1000),1000)
    q=quote();q['statisticData'][0]['price']['USD']=7431
    a=focus.assess(q,r,c,search(),1000)
    assert a['reference_usd']=='6706.4775'
    assert not a['eligible_deal']
