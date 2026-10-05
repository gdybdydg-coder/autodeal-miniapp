"""Offline prelaunch regressions; no actual source or Telegram transports."""
import hashlib
import json

import pytest
from sqlalchemy.orm import Session

from backend import olx_owner_feed as feed
from backend.models import SourceProbe, User
from backend.tests.test_olx_isolated_integration import bench
from backend.tests.test_olx_owner_feed import (
    live, owned_cycle, synthetic_car, synthetic_profile,
)


def plan(live, monkeypatch, **extra):
    profile=synthetic_profile(live)
    car=synthetic_car(live, **extra)
    profile['candidate_urls']=[{'id':car['id'],'url':car['url']}]
    monkeypatch.setattr(feed,'parse_detail_snapshot',lambda *a,**kw:{})
    monkeypatch.setattr(feed,'enrich',lambda *a,**kw:{'listing':dict(car)})
    return profile,car


def test_unestimated_candidate_can_refresh_without_replaying_delivery(live,monkeypatch):
    profile,car=plan(live,monkeypatch,generation=None)
    calls=[]
    fetch=lambda url:(calls.append(url) or (200,b'fixture',False))
    feed.tick(live.engine,live.settings,profile=profile,fetch=fetch)
    live.clock[0]+=601
    car.update(generation='A5',checked_at=int(live.clock[0]),price='5900')
    car['observed_asking_display']={**car['observed_asking_display'],'amount':'5900'}
    sends=[]
    def sender(token,method,payload,**kw):
        sends.append(method)
        if method=='getChat':return {'ok':True,'result':{'id':900,'type':'private'}}
        raise TimeoutError('ambiguous send')
    feed.tick(live.engine,live.settings,profile=profile,fetch=fetch,sender=sender)
    assert len(calls)==2 and sends==['getChat','sendMessage']
    with Session(live.engine) as db:
        state=db.get(SourceProbe,feed.STATE).result
        detail=state['detail_states'][car['id']]
        assert detail['first_seen_at']==int(live.clock[0])-601
        assert 'reprice' in detail['changes']['event_kinds']
        assert state['receipts'][0]['status']=='uncertain'
    live.clock[0]+=601
    feed.tick(live.engine,live.settings,profile=profile,fetch=fetch,sender=sender)
    assert len(calls)==2 and sends==['getChat','sendMessage']


@pytest.mark.parametrize('failure',['503','timeout'])
def test_transient_retry_is_one_then_candidate_cooldown(live,monkeypatch,failure):
    profile,car=plan(live,monkeypatch)
    calls=[]
    def fetch(url):
        calls.append(url)
        if failure=='timeout':raise TimeoutError('fixture source deadline')
        return 503,b'unavailable',False
    for seconds in (0,601,601):
        live.clock[0]+=seconds
        feed.tick(live.engine,live.settings,profile=profile,fetch=fetch)
    assert len(calls)==2
    with Session(live.engine) as db:
        control=db.get(SourceProbe,feed.STATE)
        assert control.status=='active'
        state=control.result['detail_states'][car['id']]
        assert state['status']=='cooldown' and state['attempts']==2
        if failure=='timeout':assert control.result['unknown_byte_receipts']==2


def test_observed_locale_redirect_is_reserved_as_second_get(live,monkeypatch):
    profile,car=plan(live,monkeypatch,generation=None)
    canonical=car['url'];original=canonical.replace('/d/uk/','/d/')
    profile['candidate_urls'][0]['url']=original
    calls=[]
    def fetch(url):
        calls.append(url)
        return (302,b'redirect',False,canonical) if url==original else (200,b'fixture',False,None)
    feed.tick(live.engine,live.settings,profile=profile,fetch=fetch)
    assert calls==[original,canonical]
    with Session(live.engine) as db:
        row=db.get(SourceProbe,feed.STATE)
        assert row.requests==2 and row.result['day_gets']==2
        assert row.result['day_bytes']==2*feed.DETAIL_CAP
        assert row.result['actual_bytes']==len(b'redirectfixture')


@pytest.mark.parametrize('location',[
    None, 'https://example.test/stolen.html',
    'https://www.olx.ua/d/uk/obyavlenie/other-IDother.html',
    'https://www.olx.ua/d/uk/obyavlenie/fixture-IDtarget.html?secret=x',
])
def test_redirect_without_exact_observed_same_ad_locale_is_not_followed(live,monkeypatch,location):
    profile,car=plan(live,monkeypatch)
    profile['candidate_urls'][0]['url']=car['url'].replace('/d/uk/','/d/')
    calls=[]
    feed.tick(live.engine,live.settings,profile=profile,
        fetch=lambda url:(calls.append(url) or (302,b'fixture',False,location)))
    assert len(calls)==1
    with Session(live.engine) as db:assert db.get(SourceProbe,feed.STATE).status=='technical_hold'


def test_stop_before_redirect_hop_prevents_second_source_get(live,monkeypatch):
    profile,car=plan(live,monkeypatch)
    canonical=car['url'];profile['candidate_urls'][0]['url']=canonical.replace('/d/uk/','/d/')
    calls=[]
    def fetch(url):
        calls.append(url)
        with Session(live.engine) as db:db.get(User,900).ready=False;db.commit()
        return 302,b'fixture',False,canonical
    feed.tick(live.engine,live.settings,profile=profile,fetch=fetch)
    assert len(calls)==1


def test_lease_expiry_during_getchat_prevents_send(live):
    profile=synthetic_profile(live);token,state=owned_cycle(live);car=synthetic_car(live)
    calls=[]
    def sender(token,method,payload,**kw):
        calls.append(method)
        live.clock[0]+=61
        return {'ok':True,'result':{'id':900,'type':'private'}}
    assert not feed.send_card(live.engine,live.settings,token,state,car,profile,sender)
    assert calls==['getChat']


@pytest.mark.parametrize('overlap',['native_id','vehicle_key'])
def test_reference_target_cannot_leak_into_method_controls(live,overlap):
    profile=synthetic_profile(live);target=synthetic_car(live,'target-reference','6000')
    reference=dict(target)
    if overlap=='vehicle_key':reference['id']='reference-crosspost'
    profile['reference'].append(reference)
    roles={c['id']:'reference' for c in profile['reference']}|{c['id']:'holdout' for c in profile['holdout']}
    profile['split_freeze']['membership_sha256']=hashlib.sha256(
        json.dumps(roles,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert feed.profile_ready(profile,live.clock[0])
    assert feed.assessment_for(target,profile,feed.current_search(live.engine,live.settings),live.clock[0]) is None


def test_persisted_refresh_detail_removes_raw_private_accidental_fields(live,monkeypatch):
    private='TMBAB1234A1234567 +380671234567 PRIVATE-DESCRIPTION'
    profile,car=plan(live,monkeypatch,generation=None)
    car.update(description=private,vin=private,phone=private,public_params={'vin':private},
        title='Skoda Octavia TMBAB1234A1234567 +380671234567')
    car['observed_asking_display']['description']=private
    car['eligibility_review']['evidence']=[{'snippet':private}]
    car['research_condition_evidence']={'version':'condition-v1','binding':'a'*64,
        'description_sha256':'b'*64,'description_damage_flags':['body_dents'],
        'independently_verified':False,'raw_description':private}
    car['price_conflicts']=[{'private':private}]
    feed.tick(live.engine,live.settings,profile=profile,fetch=lambda url:(200,b'fixture',False))
    with Session(live.engine) as db:
        stored=db.get(SourceProbe,feed.STATE).result['detail_states'][car['id']]['listing']
        text=json.dumps(stored)
        assert 'TMBAB1234A1234567' not in text and '+380671234567' not in text
        assert 'PRIVATE-DESCRIPTION' not in text
        assert not {'description','vin','phone','public_params'}.intersection(stored)
        assert stored['research_condition_evidence']['binding']=='a'*64
        assert stored['research_condition_evidence']['description_damage_flags']==['body_dents']
        assert stored['price_conflicts']==['unclassified_observed_conflict']


def test_repeated_locale_302_has_two_reserved_gets_without_third(live,monkeypatch):
    profile,car=plan(live,monkeypatch)
    canonical=car['url'];profile['candidate_urls'][0]['url']=canonical.replace('/d/uk/','/d/')
    calls=[]
    feed.tick(live.engine,live.settings,profile=profile,
        fetch=lambda url:(calls.append(url) or (302,b'fixture',False,canonical)))
    assert len(calls)==2
    with Session(live.engine) as db:
        row=db.get(SourceProbe,feed.STATE)
        assert row.requests==2 and row.status=='technical_hold'


def fake_http(monkeypatch,body,status=200,location=None):
    class Response:
        status_code=status
        headers={'Location':location} if location else {}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_bytes(self):yield from body
    class Client:
        def __init__(self,**kw):assert kw['follow_redirects'] is False
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def stream(self,method,url):assert method=='GET';return Response()
    monkeypatch.setattr(feed.httpx,'Client',Client)


def test_source_transport_caps_retained_body_without_following_redirect(monkeypatch):
    url='https://www.olx.ua/d/obyavlenie/fixture-IDexample.html'
    fake_http(monkeypatch,[b'x'*(feed.DETAIL_CAP+100)])
    code,body,truncated=feed.source_fetch(url)
    assert code==200 and len(body)==feed.DETAIL_CAP and truncated is True
    observed=url.replace('/d/','/d/uk/')
    fake_http(monkeypatch,[b'redirect'],302,observed)
    assert feed.source_fetch(url,include_location=True)==(302,b'redirect',False,observed)


def test_source_transport_checks_deadline_even_on_empty_final_chunk(monkeypatch):
    fake_http(monkeypatch,[])
    times=iter([0,21]);monkeypatch.setattr(feed.time,'monotonic',lambda:next(times))
    with pytest.raises(TimeoutError,match='source_deadline'):
        feed.source_fetch('https://www.olx.ua/d/obyavlenie/fixture-IDexample.html')
