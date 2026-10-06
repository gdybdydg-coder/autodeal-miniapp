import json
import pytest
from sqlalchemy.orm import Session
from backend import olx_owner_monitor as mon
from backend.models import SourceProbe
from backend.olx_market.source_tracking import parse_page
from backend.tests.test_olx_owner_monitor import running,parsed
from backend.tests.test_olx_ria_probe import live,bench,client


def test_discovery_limit_keeps_half_hour_budget_for_details(running):
    mon.initialize(running.engine,running.settings);token=mon.claim(running.engine,running.settings)
    for _ in range(20):mon.reserve(running.engine,running.settings,token,'olx',100,'observed-search',discovery=True)
    with pytest.raises(ValueError,match='olx_discovery_budget_exhausted'):
        mon.reserve(running.engine,running.settings,token,'olx',100,'observed-search',discovery=True)
    for _ in range(20):mon.reserve(running.engine,running.settings,token,'olx',100,'observed-detail')
    with pytest.raises(ValueError,match='olx_budget_exhausted'):
        mon.reserve(running.engine,running.settings,token,'olx',100,'observed-detail')
    assert mon.status(running.engine,running.settings)['budget']['olx_hour']==40


def test_pending_processing_runs_when_discovery_subbudget_is_full(running,monkeypatch):
    calls=[]
    def discovery(*args):raise ValueError('olx_discovery_budget_exhausted')
    monkeypatch.setattr(mon,'discover',discovery)
    monkeypatch.setattr(mon,'process',lambda *args:calls.append('pending_processed'))
    mon.tick(running.engine,running.settings)
    assert calls==['pending_processed']


def test_verified_canonical_search_redirect_cached_without_spending_again(running):
    mon.initialize(running.engine,running.settings);token=mon.claim(running.engine,running.settings)
    original=mon.ROOT+'vin?currency=UAH';canonical=mon.ROOT+'vin/?currency=UAH';calls=[]
    def fetch(url,cap):
        calls.append(url)
        return (302,b'',canonical) if url==original else (200,b'complete',None)
    for _ in range(2):
        assert mon.source_read(running.engine,running.settings,token,original,100,fetch,discovery=True)==b'complete'
    assert calls==[original,canonical,canonical]
    assert mon.status(running.engine,running.settings)['source_requests']==3


def html(ad,*,visible_id='101',visible_url='https://www.olx.ua/d/uk/obyavlenie/car-ID101.html'):
    state=json.dumps(json.dumps({'listing':{'listing':{'ads':[ad]}}}))
    return ('<html><body><div data-testid="l-card" id="'+visible_id+'"><a data-testid="card-title-link" href="'+visible_url+'?search_reason=search%7Corganic">Car</a><p data-testid="ad-price">5000 $</p></div><script id="olx-init-config">window.__PRERENDERED_STATE__ = '+state+';</script></body></html>').encode()


@pytest.mark.parametrize('change',[{}, {'id':102},{'url':'https://www.olx.ua/d/uk/obyavlenie/other-ID101.html'},{'createdTime':'unknown'},{'createdTime':'2030-01-01T10:00:00+03:00'}])
def test_search_creation_requires_matching_visible_identity_and_valid_date(change):
    ad={'id':101,'url':'https://www.olx.ua/d/uk/obyavlenie/car-ID101.html','createdTime':'2026-10-01T10:00:00+03:00',**change}
    result=parse_page(html(ad),fetched_at=1791292000,truncated=False)
    c=result['listings'][0]
    assert (type(c.get('source_created_at')) is int)==(not change)
    assert not c['publication_verified']
    if not change:assert c['source_created_at']==1790838000


def test_saved_pending_old_card_excluded_before_detail_request(running,monkeypatch):
    mon.initialize(running.engine,running.settings);token=mon.claim(running.engine,running.settings)
    url=mon.routes(mon.searches(running.engine,running.settings))[0]
    with Session(running.engine) as db:
        row=db.get(SourceProbe,mon.STATE);row.result={**row.result,'sources':{url:{'ids':['100']}}}
        db.add(SourceProbe(id='olx-monitor-ad-101',status='pending',requests=0,checked_at=1,result={'id':'101','first_seen':int(running.clock[0])}));db.commit()
    p=parsed(['101','102','100']);p['listings'][0]['source_created_at']=int(running.clock[0])-1000
    p['listings'][0]['creation_identity_verified']=True
    p['listings'][1]['source_created_at']=int(running.clock[0])
    p['listings'][1]['creation_identity_verified']=True
    monkeypatch.setattr(mon,'parse_page',lambda *a,**k:p)
    mon.discover(running.engine,running.settings,token,lambda *a:(200,b'fixture',None))
    with Session(running.engine) as db:
        old=db.get(SourceProbe,'olx-monitor-ad-101');new=db.get(SourceProbe,'olx-monitor-ad-102')
        assert old.status=='excluded' and old.result['reason']=='source_created_before_monitor_start'
        assert new.status=='pending' and new.result['source_created_at']==int(running.clock[0])
    assert mon.status(running.engine,running.settings)['source_requests']==1
