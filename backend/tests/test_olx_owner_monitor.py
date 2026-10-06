from copy import deepcopy
from dataclasses import replace
import pytest
from sqlalchemy.orm import Session
from backend import olx_owner_monitor as mon,olx_owner_feed as feed
from backend.models import SourceProbe,User,Search
from backend.manual_payment_models import PaymentRequest
from backend.tests.test_olx_ria_probe import live,bench,client
from backend.tests.test_olx_focused_provider import car


@pytest.fixture
def running(live,monkeypatch):
    monkeypatch.setenv(mon.ARM,'true');monkeypatch.setenv('OLX_OWNER_MONITOR_RIA_DAILY_LIMIT','100')
    monkeypatch.setattr(mon.time,'sleep',lambda _:None)
    return live


@pytest.mark.parametrize('why',['off','wrong_owner','unpaid','stop','olx_stop','search_off','source_hold'])
def test_disallowed_never_performs_io(running,monkeypatch,why):
    if why=='off':monkeypatch.setenv(mon.ARM,'false')
    elif why=='wrong_owner':running.settings=replace(running.settings,admin_telegram_id=100)
    else:
        with Session(running.engine) as db:
            if why=='unpaid':db.delete(db.get(PaymentRequest,'test-900'))
            elif why=='stop':db.get(User,900).ready=False
            elif why=='olx_stop':db.add(SourceProbe(id=feed.STATE,status='paused_by_owner',requests=0,checked_at=1,result={}))
            elif why=='search_off':db.get(Search,900).enabled=False
            else:db.add(SourceProbe(id=mon.STATE,status='source_hold',requests=0,checked_at=1,result={}))
            db.commit()
    assert mon.tick(running.engine,running.settings,public=lambda *a:pytest.fail('no IO'),provider=lambda *a:pytest.fail('no paid IO'))=='access_stopped'


def parsed(ids):
    return {'listings':[{'id':i,'url':'https://www.olx.ua/d/uk/obyavlenie/car-ID'+i+'.html','observed_search_reason':'organic','price':'6500','currency':'USD'} for i in ids],
        'summary':{'download_truncated':False,'observed_sort':{'value':'created_at:desc'},'observed_pagination_links':[]}}


def test_baseline_new_item_real_access_send_then_restart_no_duplicates(running,monkeypatch):
    from backend.billing_models import Entitlement
    with Session(running.engine) as db:
        db.get(PaymentRequest,'test-900').expires_at=running.clock[0]+86400
        db.get(Entitlement,900).expires_at=running.clock[0]+86400
        db.commit()
    phase=[0];calls=[]
    monkeypatch.setattr(mon,'parse_page',lambda *a,**k:parsed(['100'] if not phase[0] else ['101','100']))
    def detail(*a,**k):
        c=car(running.clock[0]);c.update(id='101',url='https://www.olx.ua/d/uk/obyavlenie/car-ID101.html',generation=None)
        c['source_date_observations']={'identity_matches':True,'issues':[],'values':{'createdTime':{'epoch':int(running.clock[0])}}}
        return {'listing':c}
    monkeypatch.setattr(mon,'parse_detail_snapshot',detail);monkeypatch.setattr(mon,'enrich',lambda raw,p:p)
    def public(url,cap):calls.append(('public',url));return 200,b'fixture',None
    def provider(settings,path,body,params):
        calls.append(('provider',path))
        if body:return 200,{'statisticData':[{'type':'avgPrice','price':{'USD':9000},'avgValueRange':.05,'quantityAdv':20}]},100
        names={'marks':[(70,'Skoda')],'models':[(652,'Octavia')],'bodystyles':[(2,'Універсал')],'type':[(2,'Дизель')],'gearboxes':[(1,'Ручна / Механіка')],'driverTypes':[(2,'Передній')]}
        return 200,[{'value':i,'name':n} for i,n in names[path.split('/')[-1]]],100
    def sender(token,method,payload,timeout):
        assert payload['chat_id']==900;calls.append(('telegram',method))
        return {'ok':True,'result':{'id':900,'type':'private'}} if method=='getChat' else {'ok':True,'result':{'message_id':55,'chat':{'id':900,'type':'private'}}}
    mon.tick(running.engine,running.settings,public,provider,sender)
    assert not [x for x in calls if x[0]!='public']
    phase[0]=1;running.clock[0]+=3601
    result=mon.tick(running.engine,running.settings,public,provider,sender)
    assert result['accepted']==1 and result['last_receipt']['message_id']==55
    assert len([c for c in calls if c[0]=='provider'])==7
    running.clock[0]+=3601
    mon.tick(running.engine,running.settings,public,provider,sender)
    assert len([x for x in calls if x[0]=='telegram'])==2
    with Session(running.engine) as db:assert db.get(SourceProbe,'olx-monitor-ad-100').status=='baseline'


def test_shared_lease_and_budget_survive_worker_restart(running,monkeypatch):
    mon.initialize(running.engine,running.settings);token=mon.claim(running.engine,running.settings)
    assert token and mon.claim(running.engine,running.settings) is None
    monkeypatch.setenv('OLX_OWNER_MONITOR_RIA_DAILY_LIMIT','1')
    mon.reserve(running.engine,running.settings,token,'ria',100,'auto/type')
    with pytest.raises(ValueError,match='budget'):mon.reserve(running.engine,running.settings,token,'ria',100,'auto/type')
    running.clock[0]+=mon.LEASE+1
    second=mon.claim(running.engine,running.settings);assert second and second!=token
    with pytest.raises(ValueError,match='budget'):mon.reserve(running.engine,running.settings,second,'ria',100,'auto/type')
    with pytest.raises(ValueError,match='access'):mon.reserve(running.engine,running.settings,token,'olx',100,'observed')


def test_missing_explicit_daily_paid_budget_makes_zero_api_calls(running,monkeypatch):
    monkeypatch.delenv('OLX_OWNER_MONITOR_RIA_DAILY_LIMIT');mon.initialize(running.engine,running.settings);t=mon.claim(running.engine,running.settings)
    with pytest.raises(ValueError,match='budget'):mon.ria(running.engine,running.settings,t,'auto/type',transport=lambda *a:pytest.fail('paid call'))


@pytest.mark.parametrize('code',[401,403,429])
def test_source_hold_persists_and_no_retry_on_restart(running,code):
    calls=[]
    result=mon.tick(running.engine,running.settings,public=lambda *a:(calls.append(a) or (code,b'',None)))
    assert result['status']=='source_hold' and len(calls)==1
    assert mon.tick(running.engine,running.settings,public=lambda *a:pytest.fail('hold replay'))=='access_stopped'


def test_timezone_schedule_is_hourly_at_night_and_ten_minutes_day():
    from datetime import datetime
    for hour,wanted in ((0,3600),(7,3600),(8,600),(22,600),(23,3600)):
        assert feed.interval(datetime(2026,10,6,hour,tzinfo=feed.KYIV).timestamp())==wanted


def test_explicit_four_region_routes():
    rows=[{'filters':{'region':list(mon.REGIONS)}}]
    urls=mon.routes(rows)
    assert len(urls)==4 and all('created_at' in u for u in urls)
    with pytest.raises(ValueError,match='region'):mon.routes([{'filters':{'region':['unreviewed']}}])


def test_fx_nbu_failure_uses_verified_bank_official_rate(running):
    import json
    mon.initialize(running.engine,running.settings);t=mon.claim(running.engine,running.settings)
    today=mon.datetime.fromtimestamp(running.clock[0],feed.KYIV).strftime('%d.%m.%Y');calls=[]
    def transport(url,cap):
        calls.append(url)
        if len(calls)==1:return 503,b'',None
        return 200,json.dumps({'bank':'PB','baseCurrency':980,'baseCurrencyLit':'UAH','date':today,'exchangeRate':[{'currency':'USD','baseCurrency':'UAH','purchaseRateNB':40,'saleRateNB':40}]}).encode(),None
    q=mon.fx(running.engine,running.settings,t,transport)
    assert q.rate==40 and len(calls)==2
    assert mon.fx(running.engine,running.settings,t,lambda *a:pytest.fail('cached'))==q


def test_pagination_until_overlap_keeps_all_new_candidates(running,monkeypatch):
    mon.initialize(running.engine,running.settings);token=mon.claim(running.engine,running.settings)
    url=mon.routes(mon.searches(running.engine,running.settings))[0]
    with Session(running.engine) as db:
        row=db.get(SourceProbe,mon.STATE);row.result={**row.result,'sources':{url:{'ids':['100']}}};db.commit()
    def page(raw,**kw):
        if raw==b'first':
            p=parsed(['103','102']);p['summary']['observed_pagination_links']=['?currency=UAH&search%5Border%5D=created_at%3Adesc&page=2'];return p
        return parsed(['101','100'])
    monkeypatch.setattr(mon,'parse_page',page);calls=[]
    mon.discover(running.engine,running.settings,token,lambda url,cap:(calls.append(url) or (200,b'second' if 'page=2' in url else b'first',None)))
    with Session(running.engine) as db:
        state=db.get(SourceProbe,mon.STATE).result
        assert state['sources'][url]['pages']==2 and not state['sources'][url]['possible_gap']
        assert all(db.get(SourceProbe,'olx-monitor-ad-'+i) for i in ['101','102','103'])
    assert len(calls)==2


def test_uah_assessment_preserves_original_basis_without_double_usd_conversion():
    from datetime import datetime
    from decimal import Decimal
    from backend.olx_market.fx_policy import Quote,nbu_all_url
    from backend.tests.test_olx_focused_provider import mapping,quote,search
    now=1791285000;c=car(now);c['price']='260000';c['currency']='UAH'
    c['observed_asking_display'].update(amount='260000',currency='UAH')
    day=datetime.fromtimestamp(now,feed.KYIV).date();q=Quote(Decimal(40),nbu_all_url(day),'nbu_official',day,datetime.fromtimestamp(now,feed.KYIV))
    request=mon.focus.prepare(c,mapping(c,now),now)
    a=mon.focus.assess(quote(),request,c,search(),now,quote=q)
    assert a['asking_usd']=='6500' and a['price_basis']['input_currency']=='UAH'
    assert '≈ 6 500 USD' in mon.focus.caption(c,a)
    c=car(now);r=mon.focus.prepare(c,mapping(c,now),now)
    b=mon.focus.assess(quote(),r,c,search(),now,quote=q)
    assert b['asking_usd']=='6500' and b['price_basis']['fx'] is None
