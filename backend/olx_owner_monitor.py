"""Persistent owner-only OLX discovery. Default off; separate queue and budgets.
A daily RIA allowance is required explicitly; old probe budgets are never reset.
"""
import asyncio,copy,hashlib,json,logging,os,time,uuid,unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit,urlencode,urljoin,parse_qs
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .models import SourceProbe,User
from . import billing,paid_source_access,olx_owner_feed as feed,olx_focused_provider as focus,olx_ria_probe as probe
from .olx_market.source_tracking import parse_page
from .olx_market.observations import enrich,asking_price_reasons
from .olx_market.fx_policy import Quote,normalize,parse_quote,nbu_all_url,privat_url
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot

STATE='olx-owner-monitor-20261006-v1'
ARM='OLX_OWNER_MONITOR_ENABLED'
LOG=logging.getLogger('uvicorn.error')
ROOT='https://www.olx.ua/uk/transport/legkovye-avtomobili/'
# Explicit oblast links observed in complete public category HTML 2026-10-06.
REGIONS={'вінницька':'vin','тернопільська':'ter','хмельницька':'khm','чернівецька':'chv'}
SEARCH_CAP=4*1024*1024
DETAIL_CAP=2*1024*1024
OLX_DAY=600
OLX_HOUR=40
BYTE_DAY=2*1024**3
RIA_HOUR=20
FX_DAY=12
LEASE=240


def enabled():return os.getenv(ARM)=='true'
def allowance():
    try:return max(0,min(1000,int(os.getenv('OLX_OWNER_MONITOR_RIA_DAILY_LIMIT','0'))))
    except ValueError:return 0

def searches(engine,settings):
    if not enabled() or not settings.live or not billing.verified_admin(settings) or not paid_source_access.strict(engine):return []
    with Session(engine) as db:
        stop=db.get(SourceProbe,feed.STATE);state=db.get(SourceProbe,STATE)
        if stop and stop.status=='paused_by_owner':return []
        if state and state.status in ('source_hold','paused_by_owner'):return []
        user=db.get(User,settings.admin_telegram_id)
        if not user or not user.ready or not paid_source_access.allowed(db,user.id,time.time()):return []
        return feed.own_searches(db,settings)


def initialize(engine,settings):
    if not searches(engine,settings):return False
    with Session(engine) as db:
        if db.get(SourceProbe,STATE):return True
        db.add(SourceProbe(id=STATE,status='active',checked_at=time.time(),requests=0,result={
            'started_at':int(time.time()),'lease':'','lease_until':0,'next_at':0,
            'sources':{},'budget':{},'cycles':0,'accepted':0,'receipts':[],
            'source_requests':0,'ria_requests':0,'fx_requests':0,'received_bytes':0}))
        try:db.commit();return True
        except IntegrityError:db.rollback();return False


def claim(engine,settings):
    if not searches(engine,settings):return None
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True);now=time.time()
        if not row or row.status!='active' or row.result['lease_until']>now or row.result['next_at']>now:return None
        token=uuid.uuid4().hex;row.result={**row.result,'lease':token,'lease_until':now+LEASE};db.commit();return token


def access(engine,settings,plan):
    rows=searches(engine,settings)
    if not rows or time.time()>=plan['until']:return False
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE)
        if not row or row.status!='active' or row.result['lease']!=plan['token'] or row.result['lease_until']<=time.time():return False
    return any(s['id']==plan['search_id'] and s['fingerprint']==plan['fingerprint'] for s in rows)


def live_plan(engine,settings,token):
    rows=searches(engine,settings)
    if not rows:raise ValueError('access_stopped')
    return {'token':token,'until':time.time()+LEASE,'search_id':rows[0]['id'],'fingerprint':rows[0]['fingerprint']}


def reserve(engine,settings,token,kind,cap,url):
    plan=live_plan(engine,settings,token)
    if not access(engine,settings,plan):raise ValueError('access_stopped')
    now=time.time();local=datetime.fromtimestamp(now,feed.KYIV);day=local.strftime('%Y-%m-%d');hour=local.strftime('%Y-%m-%dT%H%z')
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        if not row or row.status!='active' or row.result['lease']!=token or row.result['lease_until']<=now:raise ValueError('lease_expired')
        d=copy.deepcopy(row.result);b=d['budget']
        if b.get('day')!=day:b={'day':day,'olx':0,'ria':0,'fx':0,'bytes':0}
        if b.get('hour')!=hour:b.update(hour=hour,olx_hour=0,ria_hour=0)
        if kind=='olx' and (b['olx']>=OLX_DAY or b['olx_hour']>=OLX_HOUR or b['bytes']+cap>BYTE_DAY):raise ValueError('olx_budget_exhausted')
        if kind=='ria' and (b['ria']>=allowance() or b['ria_hour']>=RIA_HOUR):raise ValueError('ria_budget_not_authorized_or_exhausted')
        if kind=='fx' and b['fx']>=FX_DAY:raise ValueError('fx_budget_exhausted')
        b[kind]+=1;b['bytes']+=cap
        if kind in ('olx','ria'):b[kind+'_hour']+=1
        d['budget']=b;d[{'olx':'source_requests','ria':'ria_requests','fx':'fx_requests'}[kind]]+=1
        d['last_reservation']={'kind':kind,'url_or_path':url,'cap':cap,'at':now,'status':'reserved'}
        row.result=d;row.requests+=1;db.commit()
    return plan


def public_fetch(url,cap):
    try:
        with build_opener(probe.NoRedirect()).open(Request(url,headers={'Accept':'text/html,application/json','User-Agent':'AutoDeal owner monitor'}),timeout=20) as r:
            raw=r.read(cap+1);return r.status,raw,r.headers.get('Location')
    except HTTPError as e:return e.code,b'',e.headers.get('Location')


def record_io(engine,token,code,size):
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        if row and row.result['lease']==token:
            row.result={**row.result,'received_bytes':row.result['received_bytes']+size,
                'last_reservation':{**row.result['last_reservation'],'status':'http_'+str(code),'bytes':size}};db.commit()


def fetch(engine,settings,token,url,cap,kind='olx',transport=public_fetch):
    plan=reserve(engine,settings,token,kind,cap,url)
    if not access(engine,settings,plan):raise ValueError('access_stopped')
    code,raw,location=transport(url,cap);record_io(engine,token,code,len(raw))
    if code in (401,403,429):raise ValueError(('source_hold_' if kind=='olx' else 'fx_unavailable_')+str(code))
    if len(raw)>cap:raise ValueError('source_body_cap')
    if kind=='olx' and any(x in raw.lower() for x in (b'challenge-platform',b'robot verification',b'captcha-page')):raise ValueError('source_hold_challenge')
    time.sleep(4)
    return code,raw,location


def routes(rows):
    selected=set()
    for row in rows:
        wanted=row['filters'].get('region') or []
        if isinstance(wanted,str):wanted=[wanted]
        if not wanted:selected.add('')
        for region in wanted:
            key=region.casefold().removesuffix(' область').strip()
            if key not in REGIONS:raise ValueError('region_route_not_reviewed')
            selected.add(REGIONS[key])
    return [ROOT+r+'?'+urlencode({'currency':'UAH','search[order]':'created_at:desc'}) for r in sorted(selected)]


def source_read(engine,settings,token,url,cap,transport):
    code,raw,location=fetch(engine,settings,token,url,cap,transport=transport)
    if code in (301,302,307,308) and location:
        target=urljoin(url,location);a=urlsplit(url);b=urlsplit(target)
        same=(b.scheme=='https' and b.netloc=='www.olx.ua' and not b.fragment and
              a.path.replace('/uk/','/').rstrip('/')==b.path.replace('/uk/','/').rstrip('/') and parse_qs(a.query)==parse_qs(b.query))
        if not same:raise ValueError('source_redirect_unreviewed')
        code,raw,_=fetch(engine,settings,token,target,cap,transport=transport)
    if code!=200:raise ValueError('source_http_'+str(code))
    return raw


def discover(engine,settings,token,transport=public_fetch):
    urls=routes(searches(engine,settings))
    for url in urls:
        with Session(engine) as db:old=copy.deepcopy(db.get(SourceProbe,STATE).result['sources'].get(url))
        cards=[];page_url=url;seen_pages=set();overlap=False;more=False
        for page_number in range(1,4):
            if page_url in seen_pages:raise ValueError('pagination_cycle')
            seen_pages.add(page_url)
            raw=source_read(engine,settings,token,page_url,SEARCH_CAP,transport)
            parsed=parse_page(raw,fetched_at=int(time.time()),truncated=False);s=parsed['summary']
            if s['download_truncated'] or (s.get('observed_sort') or {}).get('value')!='created_at:desc':raise ValueError('source_page_not_verified')
            current=[c for c in parsed['listings'] if c.get('observed_search_reason')=='organic']
            cards.extend(current)
            overlap=bool(old and set(c['id'] for c in cards)&set(old['ids']))
            following=[]
            for link in s.get('observed_pagination_links',[]):
                target=urljoin(page_url,link);u=urlsplit(target);base=urlsplit(url)
                if (u.scheme=='https' and u.netloc=='www.olx.ua' and not u.fragment
                    and u.path.replace('/uk/','/').rstrip('/')==base.path.replace('/uk/','/').rstrip('/')
                    and parse_qs(u.query).get('page')==[str(page_number+1)]
                    and parse_qs(u.query).get('search[order]')==['created_at:desc']):following.append(target)
            more=bool(following)
            if old is None or overlap or not more:break
            page_url=sorted(set(following))[0]
        ids=list(dict.fromkeys(c['id'] for c in cards))
        if not ids:raise ValueError('empty_source_window_unverified')
        with Session(engine) as db:
            row=db.get(SourceProbe,STATE,with_for_update=True)
            if row.result['lease']!=token or row.status!='active':raise ValueError('lease_expired')
            for c in cards:
                key='olx-monitor-ad-'+c['id']
                if db.get(SourceProbe,key):continue
                baseline=old is None
                db.add(SourceProbe(id=key,status='baseline' if baseline else 'pending',requests=0,checked_at=time.time(),
                    result={'source':'olx','id':c['id'],'url':c['url'],'first_seen':int(time.time()),
                            'snapshot_price':c.get('price'),'snapshot_currency':c.get('currency'),'event':'first_seen',
                            'publication_verified':False}))
            sources=copy.deepcopy(row.result['sources']);sources[url]={'ids':ids,'checked_at':time.time(),
                'overlap':len(set(ids)&set(old['ids'])) if old else None,
                'coverage':'newest_window_up_to_three_pages','pages':len(seen_pages),'possible_gap':bool(old and more and not overlap)}
            row.result={**row.result,'sources':sources};db.commit()


def fx(engine,settings,token,transport=public_fetch):
    with Session(engine) as db:cached=db.get(SourceProbe,STATE).result.get('fx_quote')
    try:
        q=Quote.restore(cached)
        if q.validate(time.time()) is None:return q
    except (ValueError,TypeError,KeyError,AttributeError):pass
    day=datetime.fromtimestamp(time.time(),feed.KYIV).date()
    for name,url in (('nbu_all',nbu_all_url(day)),('privat_nbu',privat_url(day))):
        try:
            code,raw,_=fetch(engine,settings,token,url,262144,'fx',transport)
            if code!=200:continue
            q=parse_quote(name,raw,time.time())
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True);row.result={**row.result,'fx_quote':q.payload()};db.commit()
            return q
        except (ValueError,OSError,TimeoutError):continue
    return None


def ria(engine,settings,token,path,body=None,transport=probe.transport):
    plan=reserve(engine,settings,token,'ria',probe.CAP,path)
    if not access(engine,settings,plan):raise ValueError('access_stopped')
    code,data,size=transport(settings,path,body,None);record_io(engine,token,code,size)
    if code in (401,403,429):raise ValueError('source_hold_ria_'+str(code))
    if code!=200:raise ValueError('ria_http_'+str(code))
    time.sleep(4);return data


def catalogue(engine,settings,token,path,transport):
    key='olx-monitor-dict-'+hashlib.sha256(path.encode()).hexdigest()[:40]
    with Session(engine) as db:
        r=db.get(SourceProbe,key)
        if r and 0<=time.time()-r.checked_at<=30*86400:return r.result['rows'],int(r.checked_at)
    rows=probe.catalog_rows(ria(engine,settings,token,path,transport=transport));now=int(time.time())
    with Session(engine) as db:
        db.merge(SourceProbe(id=key,status='cached',requests=1,checked_at=now,result={'path':path,'rows':rows}));db.commit()
    return rows,now


def identity(value):return ''.join(c for c in unicodedata.normalize('NFKD',value).casefold() if c.isalnum() and not unicodedata.combining(c))
def exact(rows,labels):
    found={i for i,n in rows if identity(n) in {identity(s) for s in labels}}
    if len(found)!=1:raise ValueError('dictionary_mapping_unverified')
    return found.pop()


def mapping(engine,settings,token,car,transport):
    out={}
    labels={'body':{'wagon':['Універсал'],'sedan':['Седан'],'hatchback':['Хетчбек'],'liftback':['Ліфтбек'],'suv':['Позашляховик / Кросовер'],'minivan':['Мінівен'],'coupe':['Купе'],'convertible':['Кабріолет'],'pickup':['Пікап']},
        'fuel':{'diesel':['Дизель'],'petrol':['Бензин'],'electric':['Електро']},
        'transmission':{'manual':['Ручна / Механіка'],'automatic':['Автомат'],'cvt':['Варіатор'],'robotized':['Робот'],'tiptronic':['Типтронік']},
        'drive_type':{'front':['Передній'],'rear':['Задній'],'all':['Повний']}}
    paths={'brand':'auto/categories/1/marks','body':'auto/categories/1/bodystyles','fuel':'auto/type','transmission':'auto/categories/1/gearboxes','drive_type':'auto/categories/1/driverTypes'}
    for field in ('brand','model','body','fuel','transmission','drive_type'):
        if car.get(field) is None:continue
        path='auto/categories/1/marks/'+str(out['brand']['id'])+'/models' if field=='model' else paths[field]
        rows,at=catalogue(engine,settings,token,path,transport)
        names=[car[field]] if field in ('brand','model') else labels[field].get(car[field],[])
        out[field]={'value':car[field],'id':exact(rows,names),'dictionary_url':'https://developers.ria.com/'+path,'checked_at':at}
    return out


def process(engine,settings,token,public=public_fetch,provider=probe.transport,sender=None):
    if allowance()==0:raise ValueError('ria_budget_not_authorized_or_exhausted')
    # One active worker. Pending candidates persist across cycles/restarts.
    with Session(engine) as db:
        queue=[(r.id,copy.deepcopy(r.result)) for r in db.scalars(select(SourceProbe).where(SourceProbe.id.like('olx-monitor-ad-%'),SourceProbe.status=='pending').order_by(SourceProbe.checked_at).limit(4))]
        boundary=db.get(SourceProbe,STATE).result['started_at']
    for key,item in queue:
        if time.time()-item['first_seen']>86400:
            with Session(engine) as db:r=db.get(SourceProbe,key);r.status='expired';db.commit()
            continue
        try:
            raw=source_read(engine,settings,token,item['url'],DETAIL_CAP,public)
            car=enrich(raw,parse_detail_snapshot(raw,fetched_at=int(time.time()),truncated=False))['listing']
            if car['id']!=item['id'] or not feed.same_detail_url(car['url'],item['url']):raise ValueError('detail_identity_mismatch')
            dates=car.get('source_date_observations',{});created=dates.get('values',{}).get('createdTime',{}).get('epoch')
            if not dates.get('identity_matches') or dates.get('issues') or type(created) is not int or created<boundary:raise ValueError('old_or_unconfirmed_source_creation')
            if asking_price_reasons(car):raise ValueError('full_price_or_eligibility_unverified')
            quote=fx(engine,settings,token,public) if car['currency']!='USD' else None
            price=normalize(car,quote,time.time())
            if price['status']!='ready':raise ValueError('fx_pending')
            current=[s for s in searches(engine,settings) if feed.filter_reasons(car,s['filters'],quote,time.time())['match'] and (not s['filters'].get('region') or car.get('region'))]
            if not current:raise ValueError('outside_owner_filters')
            prepared=focus.prepare(car,mapping(engine,settings,token,car,provider),time.time())
            data=ria(engine,settings,token,'auto/ai-avarage-price/',prepared['body'],provider)
            assessment=None;chosen=None
            for search in current:
                a=focus.assess(data,prepared,car,search,time.time(),quote=quote)
                if a['eligible_deal']:assessment=a;chosen=search;break
            receipt=None
            if chosen:
                plan={'token':token,'search_id':chosen['id'],'fingerprint':chosen['fingerprint'],'until':min(time.time()+60,car['checked_at']+300)}
                kw={'arm':ARM}
                if sender is not None:kw['sender']=sender
                receipt=focus.send(engine,settings,plan,car,assessment,access,**kw)
            with Session(engine) as db:
                r=db.get(SourceProbe,key);r.status=(receipt or {}).get('status','below_threshold' if not chosen else 'not_attempted')
                r.result={**r.result,'reviewed_at':time.time(),'assessment':assessment or a,'receipt':receipt};review_status=r.status;db.commit()
                state=db.get(SourceProbe,STATE,with_for_update=True)
                state.result={**state.result,'accepted':state.result['accepted']+int(bool(receipt and receipt['status']=='accepted')),
                    'receipts':(state.result['receipts']+([receipt] if receipt else []))[-50:]};db.commit()
            LOG.info('OLX owner monitor review %s',json.dumps({'id':item['id'],'status':review_status,'receipt':receipt}))
        except ValueError as e:
            reason=str(e)
            if 'budget' in reason or reason.startswith(('source_hold','access_','lease_')):raise
            with Session(engine) as db:
                r=db.get(SourceProbe,key);r.status='pending' if reason=='fx_pending' else 'excluded';r.result={**r.result,'reason':reason};db.commit()


def tick(engine,settings,public=public_fetch,provider=probe.transport,sender=None):
    if not initialize(engine,settings):return 'access_stopped'
    token=claim(engine,settings)
    if not token:return 'not_due_or_stopped'
    error=None
    try:
        discover(engine,settings,token,public)
        process(engine,settings,token,public,provider,sender)
    except Exception as e:error=str(e) if isinstance(e,ValueError) else type(e).__name__
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        if row and row.result['lease']==token:
            if error and error.startswith('source_hold'):row.status='source_hold'
            row.result={**row.result,'lease':'','lease_until':0,'next_at':time.time()+feed.interval(),
                'cycles':row.result['cycles']+1,'last_cycle_at':time.time(),'last_error':error};db.commit()
    summary=status(engine,settings);LOG.info('OLX owner monitor cycle %s',json.dumps(summary,ensure_ascii=False));return summary


def status(engine,settings):
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE);d=row.result if row else {}
        stop=db.get(SourceProbe,feed.STATE)
        paused=bool(stop and stop.status=='paused_by_owner')
        return {'enabled':enabled(),'status':'paused_by_owner' if paused else row.status if row else 'not_started','active_searches':len(searches(engine,settings)),
            'interval_seconds':feed.interval(),'next_at':None if paused else d.get('next_at'),'cycles':d.get('cycles',0),'accepted':d.get('accepted',0),
            'source_requests':d.get('source_requests',0),'ria_requests':d.get('ria_requests',0),'fx_requests':d.get('fx_requests',0),
            'ria_daily_allowance':allowance(),'budget':d.get('budget',{}),'last_error':d.get('last_error'),
            'coverage':'newest_oblast_windows_up_to_three_pages','sources':d.get('sources',{}),'last_receipt':(d.get('receipts') or [None])[-1]}


async def run(engine,settings,stop):
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='olx-owner-monitor') as pool:
        while not stop.is_set() and enabled():
            try:await asyncio.get_running_loop().run_in_executor(pool,tick,engine,settings)
            except Exception as e:LOG.warning('OLX owner monitor isolated error (%s)',type(e).__name__)
            try:await asyncio.wait_for(stop.wait(),timeout=30)
            except asyncio.TimeoutError:pass
