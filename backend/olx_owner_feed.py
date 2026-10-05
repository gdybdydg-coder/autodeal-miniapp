"""Valued OLX cards for one verified, paid owner; independent state and executor.

Default off. A saved profile is a reviewed real cohort, not a readiness override.
Current details, compatible analogs and frozen controls are recomputed before use.
No AUTO.RIA imports, provider calls, payment writes, webhook or Telegram poller.
"""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from html import escape
import hashlib
import json
import logging
import re
from pathlib import Path
import time
import uuid
import httpx
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from . import billing, paid_source_access, telegram_setup, olx_owner_batch as legacy
from .models import Filters, Search, SourceProbe, User
from .olx_market.candidates import filter_reasons
from .olx_market.fx_policy import normalize, Quote
from .olx_market.valuation import estimate, METHODS
from .olx_market.evaluation import evaluate_holdout, stability
from .olx_market.observations import enrich
from .olx_market.source_tracking import public_detail_url, same_detail_url, detail_change
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.integration import filters_from_backend

STATE='olx-owner-valued-20261005-v1'
PROFILE=Path(__file__).with_name('olx_owner_profile.json')
LOG=logging.getLogger('uvicorn.error')
KYIV=ZoneInfo('Europe/Kyiv')
MINIMUM=8
MAX_INITIAL=3
MAX_HOURLY_GETS=12
MAX_DAILY_GETS=60
MAX_DAILY_BYTES=160*1024*1024
DETAIL_CAP=2*1024*1024
PROFILE_MAX_AGE=24*3600
DETAIL_MAX_AGE=300
MAX_CYCLE_SECONDS=60
SOURCE_TIMEOUT=20
TRANSIENT_RETRY_SECONDS=60
FAILED_EPISODE_COOLDOWN=3600


def interval(now=None):
    hour=datetime.fromtimestamp(time.time() if now is None else now,KYIV).hour
    return 3600 if hour>=23 or hour<8 else 600


def enabled(settings,now=None):
    now=time.time() if now is None else now
    return bool(settings.olx_owner_feed_enabled and settings.live
        and billing.verified_admin(settings) and settings.olx_owner_feed_search_id>0
        and now<settings.olx_owner_feed_until
        and not settings.olx_owner_batch_enabled and not settings.olx_owner_canary_enabled)


def own_searches(db,settings):
    result=[]
    for row in db.scalars(select(Search).where(Search.user_id==settings.admin_telegram_id,
            Search.enabled.is_(True)).order_by(Search.id)):
        try:
            f=Filters.model_validate(row.filters)
            bridge=filters_from_backend(f.canonical(),currency='USD')
        except (ValueError,TypeError):
            continue
        result.append({'id':row.id,'fingerprint':f.fingerprint(),'filters':bridge['filters'],
            'threshold':f.minDiscount,'only_deals':f.onlyDeals})
    return result


def preflight(engine,settings):
    """Private server diagnostic of the configured owner only, with no mutations."""
    verified=billing.verified_admin(settings)
    out={'enabled':settings.olx_owner_feed_enabled,'owner_verified':verified,
         'recipient_count':1 if verified else 0,'strict_paid_policy':paid_source_access.strict(engine),
         'confirmed_paid':False,'ready':False,'own_searches':[],
         'selected_search_id':settings.olx_owner_feed_search_id,'interval_seconds':interval(),
         'legacy_batch_enabled':settings.olx_owner_batch_enabled,
         'legacy_canary_enabled':settings.olx_owner_canary_enabled,
         'until':settings.olx_owner_feed_until,'profile_present':PROFILE.is_file()}
    if not verified or not paid_source_access.strict(engine):return out
    with Session(engine) as db:
        if db.bind.dialect.name=='postgresql':
            from sqlalchemy import text
            db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        owner=db.get(User,settings.admin_telegram_id)
        out['ready']=bool(owner and owner.ready)
        out['confirmed_paid']=paid_source_access.allowed(db,settings.admin_telegram_id,time.time())
        out['own_searches']=own_searches(db,settings)
        state=db.get(SourceProbe,STATE)
        if state:
            out['state']={k:state.result.get(k) for k in ('attempts','accepted','receipts','last_error','next_at')}
            out['status']=state.status
    return out


def log_preflight(engine,settings):
    try:LOG.info('OLX valued owner preflight %s',json.dumps(preflight(engine,settings),ensure_ascii=False))
    except Exception as exc:LOG.warning('OLX valued owner preflight unavailable (%s)',type(exc).__name__)


def current_search(engine,settings,*,fingerprint=None):
    if not enabled(settings) or not paid_source_access.strict(engine):return None
    with Session(engine) as db:
        state=db.get(SourceProbe,STATE)
        if state and (state.status!='active' or state.result.get('until',0)<=time.time()):return None
        if state and (state.result.get('owner_id')!=settings.admin_telegram_id
                or state.result.get('search_id')!=settings.olx_owner_feed_search_id):return None
        owner=db.get(User,settings.admin_telegram_id)
        if not owner or not owner.ready or not paid_source_access.allowed(db,owner.id,time.time()):return None
        for row in own_searches(db,settings):
            if row['id']==settings.olx_owner_feed_search_id and (fingerprint is None or row['fingerprint']==fingerprint):return row
    return None


def owner_readiness(e):
    blockers=[]
    if e.get('source_reviewed') is not True:blockers.append('source_review_pending')
    if type(e.get('minimum')) is not int or e['minimum']<MINIMUM:blockers.append('minimum_below_eight')
    if e.get('max_comparables',0)<MINIMUM:blockers.append('insufficient_compatible_analogs')
    if e.get('estimated',0)<1:blockers.append('no_current_asking_estimate')
    if e.get('holdout_estimated',0)<3:blockers.append('insufficient_independent_asking_controls')
    if e.get('independent_vehicles',0)<MINIMUM:blockers.append('vehicle_independence_pending')
    if e.get('stable') is not True:blockers.append('estimate_unstable')
    if e.get('selected_method') not in METHODS or not e.get('method_version'):blockers.append('method_not_validated')
    technical=not blockers
    return {'technical_ready':technical,'owner_ready':technical and e.get('owner_authorized') is True,
        'client_ready':False,'blockers':blockers,'scope':'experimental_asking_reference_not_sale_price'}


def load_profile():
    try:return json.loads(PROFILE.read_text())
    except (OSError,ValueError):return {}


def profile_ready(profile,now):
    """Recompute observed controls; metadata flags alone cannot unlock a send."""
    try:
        if (profile.get('dataset_kind')!='saved_real' or profile.get('owner_authorized') is not True
                or profile.get('source_review',{}).get('bounded_public_channel_reviewed') is not True):return False
        cars=profile['reference']+profile['holdout']
        freeze=profile['split_freeze']
        key_format=profile.get('split_key_format')
        if key_format not in (None,'source_id_v1'):return False
        key=lambda c:c['source']+':'+c['id'] if key_format=='source_id_v1' else c['id']
        membership={key(c):role for role in ('reference','holdout') for c in profile[role]}
        digest=hashlib.sha256(json.dumps(membership,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (len(membership)!=len(cars) or freeze['membership_sha256']!=digest
                or not re.fullmatch('[a-f0-9]{40}',freeze['commit'])
                or type(freeze['frozen_at']) is not int
                or any(freeze['frozen_at']>c['checked_at'] for c in cars)):return False
        # An intact provider receipt is still unusable if its frozen role was
        # changed after observation. Bind each receipt to this exact split.
        for role in ('reference','holdout'):
            for car in profile[role]:
                if car.get('source')!='auto_ria':continue
                provenance=car.get('reference_provenance') or {}
                if (provenance.get('role')!=role
                        or provenance.get('frozen_commit')!=freeze['commit']
                        or provenance.get('frozen_at')!=freeze['frozen_at']):return False
        from .olx_market.ria_reference import public_url as ria_public_url
        if not cars or any(not (public_detail_url(c.get('url')) if c.get('source')=='olx'
                else ria_public_url(c) if c.get('source')=='auto_ria' else False) for c in cars):return False
        if any(type(c.get('checked_at')) is not int or not 0<=now-c['checked_at']<=PROFILE_MAX_AGE for c in cars):return False
        keys=[c.get('vehicle_key') for c in cars]
        # Unique public VIN-derived claims plus explicit reviewed photo evidence.
        # This supports independent advertised vehicles, not physical inspections.
        if (any(not isinstance(k,str) or not re.fullmatch('vin-sha256:[a-f0-9]{64}',k) for k in keys)
                or len(set(keys))!=len(keys)
                or any(c.get('identity_review',{}).get('distinct_photos_reviewed') is not True for c in cars)):return False
        quote=Quote.restore(profile['quote']) if profile.get('quote') else None
        evaluation=evaluate_holdout(cars,membership,quote,int(now),minimum=MINIMUM)
        if evaluation['estimated_holdout_count']<3:return False
        method=profile['selected_method']
        scored={m:Decimal(v['mean_absolute_percent']) for m,v in evaluation['metrics'].items()
                if v['evaluated']>=3 and v['mean_absolute_percent'] is not None}
        if method!=min(scored,key=lambda m:(scored[m],METHODS.index(m))):return False
        if scored[method]>Decimal('15'):return False
        controls=[r for r in evaluation['rows'] if r['error_against_asking']]
        if any(not r['review']['methods'][method]['stable_under_omission'] for r in controls):return False
        return bool(profile.get('method_version'))
    except (KeyError,ValueError,TypeError,ArithmeticError):return False


def initialize(engine,settings,search):
    now=time.time()
    with Session(engine) as db:
        if db.get(SourceProbe,STATE):return
        db.add(SourceProbe(id=STATE,status='active',checked_at=now,requests=0,
            result={'owner_id':settings.admin_telegram_id,'search_id':search['id'],
                'fingerprint':search['fingerprint'],'started_at':now,
                'until':min(now+24*3600,settings.olx_owner_feed_until),'next_at':0,
                'lease':'','lease_until':0,'attempts':0,'accepted':0,'receipts':[],
                'seen':[],'reviews':[],'hour':0,'hour_gets':0,'day':'','day_gets':0,
                'day_bytes':0,'actual_bytes':0,'last_error':None}))
        try:db.commit()
        except IntegrityError:db.rollback()


def claim(engine):
    now=time.time();token=uuid.uuid4().hex
    with Session(engine) as db:
        changed=db.execute(update(SourceProbe).where(SourceProbe.id==STATE,
            SourceProbe.status=='active',SourceProbe.result['lease_until'].as_float()<=now,
            SourceProbe.result['next_at'].as_float()<=now).values(checked_at=now))
        if not changed.rowcount:db.rollback();return None
        row=db.get(SourceProbe,STATE);data=copy.deepcopy(row.result)
        if now>=data['until']:row.status='finished';db.commit();return None
        data.update(lease=token,lease_until=now+MAX_CYCLE_SECONDS,
                    cycle_until=now+MAX_CYCLE_SECONDS,next_at=now+interval(now))
        row.result=data;db.commit();return token,data


def reserve_get(engine,settings,token,state):
    if not current_search(engine,settings,fingerprint=state['fingerprint']):return False
    now=time.time();day=datetime.fromtimestamp(now,KYIV).date().isoformat();hour=int(now//3600)
    with Session(engine) as db:
        db.execute(update(SourceProbe).where(SourceProbe.id==STATE).values(checked_at=now))
        row=db.get(SourceProbe,STATE);d=copy.deepcopy(row.result) if row else {}
        if not row or row.status!='active' or d.get('lease')!=token or d['lease_until']<=now:return False
        if d['hour']!=hour:d.update(hour=hour,hour_gets=0)
        if d['day']!=day:d.update(day=day,day_gets=0,day_bytes=0)
        if d['hour_gets']>=MAX_HOURLY_GETS or d['day_gets']>=MAX_DAILY_GETS or d['day_bytes']+DETAIL_CAP>MAX_DAILY_BYTES:return False
        d.update(hour_gets=d['hour_gets']+1,day_gets=d['day_gets']+1,day_bytes=d['day_bytes']+DETAIL_CAP)
        row.requests+=1;row.result=d;db.commit();return True


def current_lease(engine,token):
    now=time.time()
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE)
        return bool(row and row.status=='active' and row.result.get('lease')==token
            and now<min(row.result.get('lease_until',0),row.result.get('cycle_until',0)))


def detail_state(engine,source_id):
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE)
        return copy.deepcopy(row.result.get('detail_states',{}).get(source_id,{})) if row else {}


def sanitized_detail(car):
    """Persist only normalized card/market facts and bounded coded evidence."""
    fields=('source','id','url','price','currency','brand','model','generation',
        'generation_variant','body','fuel','fuel_subtype','transmission','drive_type',
        'engine_cc','power_hp','modification','doors','year','mileage_km','category',
        'research_condition','checked_at','vehicle_identity_verified')
    out={k:copy.deepcopy(car[k]) for k in fields if k in car}
    key=car.get('vehicle_key')
    out['vehicle_key']=key if isinstance(key,str) and re.fullmatch('vin-sha256:[a-f0-9]{64}',key) else None
    def label(value):
        if not isinstance(value,str):return None
        value=re.sub(r'\b[A-HJ-NPR-Z0-9]{17}\b','[приховано]',value,flags=re.I)
        value=re.sub(r'(?<!\w)\+?\d[\d\s().-]{5,}\d(?!\w)','[приховано]',value)
        return value[:256]
    for k in ('title','locality','region'):
        if k in car:out[k]=label(car[k])
    def metadata(value,allowed):
        if not isinstance(value,dict):return {}
        result={}
        for k in allowed:
            v=value.get(k)
            if type(v) in (bool,int) or v is None:
                if k in value:result[k]=v
            elif isinstance(v,str) and (re.fullmatch(r'[a-z][a-z0-9_:.+/-]{0,149}',v)
                    or re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}',v)
                    or (k=='amount' and re.fullmatch(r'\d+(?:\.\d+)?',v))
                    or (k=='currency' and v in ('UAH','USD','EUR'))):result[k]=v
            elif isinstance(v,list):result[k]=[x for x in v if isinstance(x,str)
                    and re.fullmatch(r'[a-z][a-z0-9_:.+/-]{0,149}',x)][:100]
        return result
    for k,allowed in {
        'eligibility_review':('version','status','reasons','fingerprint','description_complete',
            'customs_status','customs_independently_verified'),
        'price_review':('version','status','reasons','text_is_not_proof'),
        'observed_asking_display':('status','reasons','amount','currency','basis',
            'description_reviewed_in_full','original_seller_currency_verified','full_price_verified','limits'),
        'research_asking_display':('status','reasons','previous_reasons','amount','currency','basis',
            'description_reviewed_in_full','original_seller_currency_verified','full_price_verified','limits'),
        'identity_review':('distinct_photos_reviewed','physical_identity_verified','format_verified','basis'),
        'condition_evidence':('basis','description_sha256','description_damage_flags','independently_verified'),
        'research_condition_evidence':('version','status','basis','binding','fingerprint','http_status',
            'checked_at','description_sha256','description_damage_flags','independently_verified'),
    }.items():
        if k in car:out[k]=metadata(car[k],allowed)
    for k in ('observed_asking_display','research_asking_display'):
        if k in out and isinstance(car[k].get('state_regular_price'),dict):
            out[k]['state_regular_price']=metadata(car[k]['state_regular_price'],('amount','currency'))
    for k in ('field_conflicts','research_field_conflicts','price_conflicts'):
        if k in car:
            out[k]=metadata({'codes':car[k]},('codes',)).get('codes',[])
            if car[k] and not out[k]:out[k]=['unclassified_observed_conflict']
    evidence=car.get('research_evidence')
    if isinstance(evidence,dict):
        out['research_evidence']={k:metadata(v,('basis','independently_verified','title_sha256',
            'description_sha256','description_damage_flags','description_basis'))
            for k,v in evidence.items() if k in ('generation','generation_variant','condition','body')}
    dates=car.get('source_date_observations')
    if isinstance(dates,dict):
        out['source_date_observations']=metadata(dates,('identity_matches','issues','origin','path'))
        values=dates.get('values') or {}
        out['source_date_observations']['values']={k:{'epoch':v['epoch']}
            for k,v in values.items() if k in ('createdTime','lastRefreshTime','pushupTime','validToTime')
            and isinstance(v,dict) and type(v.get('epoch')) is int}
    out['photos']=[]
    for url in car.get('photos',[])[:30]:
        try:
            u=urlsplit(url)
            if (u.scheme=='https' and u.hostname and (u.hostname=='olxcdn.com' or u.hostname.endswith('.olxcdn.com'))
                    and not (u.username or u.password or u.port or u.fragment)):out['photos'].append(url)
        except (ValueError,TypeError):continue
    return out


def save_detail_state(engine,token,source_id,changes):
    """Persist source refresh work separately from the attempt/delivery ledger."""
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        if not row or row.status!='active' or row.result.get('lease')!=token:return False
        states=copy.deepcopy(row.result.get('detail_states',{}))
        states[source_id]={**states.get(source_id,{}),**changes}
        row.result={**row.result,'detail_states':states};db.commit();return True


def source_receipt(engine,token,source_id,url,status,body=None,*,truncated=False,redirect=None):
    """Commit observed bytes; transport exceptions leave bytes unknown."""
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        if not row or row.result.get('lease')!=token:return False
        receipt={'source':'olx','id':source_id,'url':url,'at':time.time(),
                 'status':status,'bytes':len(body) if body is not None else None,
                 'truncated':truncated}
        if redirect:receipt['observed_locale_redirect']=redirect
        row.result={**row.result,
            'actual_bytes':row.result.get('actual_bytes',0)+(len(body) if body is not None else 0),
            'unknown_byte_receipts':row.result.get('unknown_byte_receipts',0)+int(body is None),
            'source_receipts':(row.result.get('source_receipts',[])+[receipt])[-120:]}
        db.commit();return True


def locale_redirect(url,location):
    """Accept only an explicit observed redirect to the same ad's UK route."""
    if not isinstance(location,str) or not 1<=len(location)<=2048:return None
    resolved=urljoin(url,location)
    if not same_detail_url(url,resolved) or url==resolved:return None
    before=urlsplit(url);after=urlsplit(resolved)
    return resolved if (before.path.startswith('/d/obyavlenie/')
        and after.path==before.path.replace('/d/','/d/uk/',1)) else None


def fetch_current_detail(engine,settings,token,state,card,fetch,last,deadline):
    """At most two reserved GETs per episode, no implicit redirects/retries.

    This refreshes only the reviewed static initial candidate list. It is not
    continuous discovery. The existing nightly detail_change event policy is
    reused; no research SQLite, shared RIA state or paid transport is opened.
    """
    record=detail_state(engine,card['id']);now=time.time()
    if record.get('status')=='unavailable' or record.get('next_at',0)>now:return None
    previous=record.get('listing')
    if previous and 0<=now-previous.get('checked_at',0)<=DETAIL_MAX_AGE:
        return previous
    attempts=record.get('attempts',0)
    if record.get('status') in ('stored','cooldown'):attempts=0
    url=record.get('resolved_url',card['url'])
    while attempts<2:
        if last[0] is not None:time.sleep(max(0,4-(time.monotonic()-last[0])))
        # Leave room for one complete bounded request; do not extend the lease.
        if (time.monotonic()+SOURCE_TIMEOUT>deadline
                or time.time()+SOURCE_TIMEOUT>state['cycle_until']
                or not current_lease(engine,token)
                or not reserve_get(engine,settings,token,state)):return None
        if not current_search(engine,settings,fingerprint=state['fingerprint']):return None
        attempts+=1
        if not save_detail_state(engine,token,card['id'],{'status':'fetching','attempts':attempts,
                'next_at':time.time()+TRANSIENT_RETRY_SECONDS,'resolved_url':url}):return None
        if not current_lease(engine,token) or not current_search(engine,settings,fingerprint=state['fingerprint']):return None
        try:
            response=fetch(url)
        except (TimeoutError,OSError,httpx.TimeoutException,httpx.TransportError) as exc:
            last[0]=time.monotonic()
            source_receipt(engine,token,card['id'],url,type(exc).__name__)
            save_detail_state(engine,token,card['id'],{'status':'retry' if attempts<2 else 'cooldown',
                'next_at':time.time()+(TRANSIENT_RETRY_SECONDS if attempts<2 else FAILED_EPISODE_COOLDOWN),
                'last_error':'transient_'+type(exc).__name__})
            return None
        last[0]=time.monotonic()
        if not isinstance(response,(tuple,list)) or len(response) not in (3,4):raise ValueError('invalid_source_response')
        code,data,truncated=response[:3];location=response[3] if len(response)==4 else None
        redirect=locale_redirect(url,location) if code==302 else None
        source_receipt(engine,token,card['id'],url,code,data,truncated=truncated,redirect=redirect)
        if code in (401,403,429):raise ValueError('source_hold_http_'+str(code))
        if code==302:
            if not redirect or attempts>=2 or truncated:raise ValueError('unverified_or_repeated_source_redirect')
            save_detail_state(engine,token,card['id'],{'resolved_url':redirect})
            url=redirect
            continue
        if code in (408,500,502,503,504):
            save_detail_state(engine,token,card['id'],{'status':'retry' if attempts<2 else 'cooldown',
                'next_at':time.time()+(TRANSIENT_RETRY_SECONDS if attempts<2 else FAILED_EPISODE_COOLDOWN),
                'last_error':'transient_http_'+str(code)})
            return None
        if code in (404,410):
            save_detail_state(engine,token,card['id'],{'status':'unavailable','last_error':'http_'+str(code)})
            return None
        if code!=200 or truncated or len(data)>DETAIL_CAP:raise ValueError('incomplete_source_response')
        if not current_lease(engine,token):return None
        car=sanitized_detail(enrich(data,parse_detail_snapshot(data,fetched_at=int(time.time()),truncated=False))['listing'])
        if car['id']!=card['id'] or car.get('source')!='olx' or not same_detail_url(car['url'],card['url']):raise ValueError('source_identity_mismatch')
        first=record.get('first_seen_at',int(time.time()))
        change=detail_change(previous,car,first_seen=first)
        if not save_detail_state(engine,token,card['id'],{'status':'stored','attempts':0,
            'next_at':time.time()+interval(),'first_seen_at':first,'listing':car,
            'changes':change,'last_error':None}):return None
        return car
    return None


def caption(car,price,assessment,method):
    ref=Decimal(assessment['methods'][method]['reference_usd']);asking=Decimal(price['usd_amount'])
    discount=(ref-asking)/ref*100
    money=lambda n:format(Decimal(n).quantize(Decimal('1')),',').replace(',',' ')
    attrs=[]
    fuel={'diesel':'дизель','petrol':'бензин','hybrid':'гібрид','electric':'електро','lpg_petrol':'газ/бензин'}
    gearbox={'manual':'механіка','automatic':'автомат'}
    if car.get('fuel'):attrs.append(fuel.get(car['fuel'],car['fuel']))
    if car.get('transmission'):attrs.append(gearbox.get(car['transmission'],car['transmission']))
    if car.get('engine_cc') is not None:attrs.append(str(car['engine_cc'])+' см³')
    if car.get('mileage_km') is not None:attrs.append(money(car['mileage_km'])+' км')
    condition=car.get('research_condition','')
    if condition.startswith('running_reported_damage:') or condition.startswith('running_body_repair:'):
        labels={'body_dents':'вм’ятини кузова','windshield_crack':'тріщина лобового скла'}
        attrs.append('за описом: '+', '.join(labels[k] for k in condition.partition(':')[2].split('+') if k in labels))
    elif condition=='running_body_repair':attrs.append('за описом потрібен ремонт кузова')
    elif condition=='not_running':attrs.append('за описом не на ходу')
    return '\n'.join(['🟠 <b>OLX • тест лише для власника</b>',
        '🚘 <b>'+escape(' '.join(str(car[k]) for k in ('brand','model','year') if car.get(k)))+'</b>',
        '💵 Ціна: '+('≈ ' if price.get('fx') else '')+money(asking)+' USD',
        '📊 Ринкова вартість ≈ '+money(ref)+' USD',
        '📉 Нижче ринкового орієнтира: '+str(discount.quantize(Decimal('.1')))+'%',
        '📍 '+escape(' · '.join(str(car[k]) for k in ('locality','region') if car.get(k))),
        '⚙️ '+escape(' · '.join(attrs)),'',
        '🔎 Оцінка за '+str(assessment['sample'])+' сумісними аналогами.'+
            (' Джерело аналогів: AUTO.RIA.' if any(c.get('source')=='auto_ria' for c in assessment['used_comparables']) else ''),
        'Ринковий діапазон: '+money(assessment['range_usd']['low'])+'–'+money(assessment['range_usd']['high'])+' USD.',
        'Орієнтир за цінами пропозицій; ціна продажу не підтверджена.',
        'Перше виявлення не підтверджує нову публікацію.','/olx_stop — зупинити лише OLX.'])


def assessment_for(car,profile,search,now):
    quote=Quote.restore(profile['quote']) if profile.get('quote') else None
    price=normalize(car,quote,now)
    if price['status']!='ready' or not filter_reasons(car,search['filters'],quote,now)['match']:return None
    if (not re.fullmatch('vin-sha256:[a-f0-9]{64}',car.get('vehicle_key') or '')
            or any((c.get('source'),c.get('id'))==(car.get('source'),car.get('id'))
                or c.get('vehicle_key')==car['vehicle_key']
                for c in profile['reference']+profile['holdout'])):return None
    checked=stability(car,profile['reference'],quote,int(now),minimum=MINIMUM)
    a=checked['assessment'];method=profile['selected_method']
    if a['status']!='experimental_asking_estimate' or not checked['methods'][method]['stable_under_omission']:return None
    discount=Decimal(a['methods'][method]['discount_percent'])
    if discount<Decimal(str(search['threshold'])):return None
    a['selected_method']=method;a['method_version']=profile['method_version']
    a['reference_usd']=a['methods'][method]['reference_usd']
    return price,a


def send_card(engine,settings,token,state,car,profile,sender):
    """At-most-once source+ad+recipient ledger including the old OLX receipts."""
    now=time.time();search=current_search(engine,settings,fingerprint=state['fingerprint'])
    proof=assessment_for(car,profile,search,now) if search and profile_ready(profile,now) else None
    if not proof or not 0<=now-car['checked_at']<=DETAIL_MAX_AGE:return False
    key='olx-owner-car-'+hashlib.sha256((str(settings.admin_telegram_id)+':'+car['id']).encode()).hexdigest()[:32]
    with Session(engine) as db:
        db.execute(update(SourceProbe).where(SourceProbe.id==STATE).values(checked_at=now))
        row=db.get(SourceProbe,STATE)
        if (not row or row.status!='active' or row.result.get('lease')!=token
                or row.result['lease_until']<=now or row.result['attempts']>=MAX_INITIAL
                or legacy.prior_attempt(db,car['id'],settings.admin_telegram_id)):return False
        row.result={**row.result,'attempts':row.result['attempts']+1}
        db.add(SourceProbe(id=key,status='sending',checked_at=now,requests=1,
            result={'source':'olx','source_id':car['id'],'feed':STATE,'attempted_at':now,
                'assessment':proof[1],'price':proof[0]}))
        try:db.commit()
        except IntegrityError:db.rollback();return False
    response={'not_attempted':True};method=None
    try:
        if current_lease(engine,token) and current_search(engine,settings,fingerprint=state['fingerprint']):
            r=sender(settings.bot_token,'getChat',{'chat_id':settings.admin_telegram_id},timeout=5)
            chat=r.get('result',{}) if isinstance(r,dict) else {}
            if (r.get('ok') is True and type(chat.get('id')) is int and chat['id']==settings.admin_telegram_id
                    and chat.get('type')=='private' and current_search(engine,settings,fingerprint=state['fingerprint'])
                    and current_lease(engine,token) and profile_ready(profile,time.time())
                    and time.time()-car['checked_at']<=DETAIL_MAX_AGE):
                text=caption(car,*proof,profile['selected_method'])
                payload={'chat_id':settings.admin_telegram_id,'parse_mode':'HTML',
                    'reply_markup':{'inline_keyboard':[[{'text':'Переглянути на OLX','url':car['url']}]]}}
                method='sendPhoto' if car.get('photos') and len(text)<=1024 else 'sendMessage'
                payload.update({'photo':car['photos'][0],'caption':text} if method=='sendPhoto' else {'text':text,'link_preview_options':{'is_disabled':True}})
                response=sender(settings.bot_token,method,payload,timeout=5)
    except Exception:response={'uncertain':True} if method else {'not_attempted':True}
    response=response if isinstance(response,dict) else {};r=response.get('result',{})
    r=r if isinstance(r,dict) else {};chat=r.get('chat',{});chat=chat if isinstance(chat,dict) else {}
    accepted=bool(response.get('ok') is True and type(r.get('message_id')) is int and r['message_id']>0
        and type(chat.get('id')) is int and chat['id']==settings.admin_telegram_id and chat.get('type')=='private')
    status='accepted' if accepted else 'not_attempted' if response.get('not_attempted') else 'rejected' if response.get('ok') is False and response.get('error_code') in (400,401,403,404,429) else 'uncertain'
    receipt={'source_id':car['id'],'status':status,'method':method,'at':time.time(),
        'message_id':r.get('message_id') if accepted else None,'owner_private_chat_verified':accepted}
    with Session(engine) as db:
        row=db.get(SourceProbe,key);row.status=status;row.result={**row.result,**receipt}
        control=db.get(SourceProbe,STATE,with_for_update=True)
        control.result={**control.result,'accepted':control.result['accepted']+int(accepted),
            'receipts':control.result['receipts']+[receipt]}
        db.commit()
    LOG.info('OLX valued owner receipt %s',json.dumps(receipt));return accepted


def tick(engine,settings,fetch=None,sender=telegram_setup.call,profile=None):
    search=current_search(engine,settings)
    if not search:return 'access_or_search_blocked'
    profile=load_profile() if profile is None else profile
    if not profile_ready(profile,time.time()):return 'technical_data_pending'
    initialize(engine,settings,search);owned=claim(engine)
    if not owned:return 'paused_finished_or_busy'
    token,state=owned;last=[None];deadline=time.monotonic()+MAX_CYCLE_SECONDS
    try:
        fetch=(lambda url:source_fetch(url,include_location=True)) if fetch is None else fetch
        for card in profile.get('candidate_urls',[]):
            with Session(engine) as db:
                control=db.get(SourceProbe,STATE)
                if not control or control.result.get('attempts',0)>=MAX_INITIAL:break
                if legacy.prior_attempt(db,card['id'],settings.admin_telegram_id):continue
            if not public_detail_url(card['url']):raise ValueError('unobserved_public_detail_url')
            if not current_lease(engine,token) or not current_search(engine,settings,fingerprint=state['fingerprint']):break
            car=fetch_current_detail(engine,settings,token,state,card,fetch,last,deadline)
            if car is None:continue
            proof=assessment_for(car,profile,search,time.time())
            if car['id'] not in state['seen']:state['seen'].append(car['id'])
            state['reviews'].append({'id':car['id'],'checked_at':car['checked_at'],
                'status':'estimated' if proof else 'Недостатньо даних для оцінки'})
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True)
                row.result={**row.result,'seen':state['seen'],'reviews':state['reviews'],
                    'actual_bytes':row.result['actual_bytes']};db.commit()
            if proof:send_card(engine,settings,token,state,car,profile,sender)
    except Exception as exc:
        with Session(engine) as db:
            row=db.get(SourceProbe,STATE,with_for_update=True)
            if row:
                if row.status=='active':
                    row.status='source_hold' if str(exc).startswith('source_hold') else 'technical_hold'
                row.result={**row.result,'last_error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__};db.commit()
        LOG.warning('OLX valued owner held (%s)',type(exc).__name__)
    finally:
        with Session(engine) as db:
            row=db.get(SourceProbe,STATE,with_for_update=True)
            if row and row.result.get('lease')==token:
                row.result={**row.result,'lease':'','lease_until':0};db.commit()
    return 'cycle_complete'


def source_fetch(url,*,include_location=False):
    if not public_detail_url(url):raise ValueError('unobserved_public_detail_url')
    deadline=time.monotonic()+SOURCE_TIMEOUT
    with httpx.Client(timeout=httpx.Timeout(5,connect=5),follow_redirects=False,
            headers={'User-Agent':'AutoDeal-OwnerTest/1.0 (bounded public OLX pages)'}) as client:
        with client.stream('GET',url) as response:
            body=bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic()>deadline:raise TimeoutError('source_deadline')
                body.extend(chunk[:DETAIL_CAP+1-len(body)])
                if len(body)>DETAIL_CAP:
                    result=(response.status_code,bytes(body[:DETAIL_CAP]),True)
                    return result+(response.headers.get('Location'),) if include_location else result
            if time.monotonic()>deadline:raise TimeoutError('source_deadline')
            if response.status_code==200 and any(m in bytes(body).lower() for m in
                    (b'challenge-platform',b'robot verification',b'captcha-page')):
                raise ValueError('source_hold_challenge')
            result=(response.status_code,bytes(body),False)
            return result+(response.headers.get('Location'),) if include_location else result


async def run(engine,settings,stop):
    # Its own single worker prevents slow OLX from taking RIA/default executors.
    executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='olx-owner-only')
    try:
        while not stop.is_set() and enabled(settings):
            try:await asyncio.get_running_loop().run_in_executor(executor,tick,engine,settings)
            except Exception as exc:LOG.warning('OLX valued owner isolated failure (%s)',type(exc).__name__)
            try:await asyncio.wait_for(stop.wait(),timeout=interval())
            except asyncio.TimeoutError:pass
    finally:executor.shutdown(wait=False,cancel_futures=True)


def handle(engine,settings,event):
    if not billing.verified_admin(settings):return None
    message=event.get('message') or {};chat=message.get('chat') or {};user=message.get('from') or {}
    if chat.get('type')!='private' or type(chat.get('id')) is not int or chat['id']!=settings.admin_telegram_id or user.get('id')!=settings.admin_telegram_id:return None
    raw=message.get('text','').split()[0] if message.get('text','').strip() else ''
    command,_,mention=raw.partition('@')
    if mention and mention.casefold()!=telegram_setup.BOT_USERNAME.casefold():return None
    if command not in ('/olx_status','/olx_stop'):return None
    # An explicitly enabled legacy task retains its own stop command. With
    # both old switches off, status belongs to this new, default-off canary.
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE)
        if (not settings.olx_owner_feed_enabled and not row
                and (settings.olx_owner_batch_enabled or settings.olx_owner_canary_enabled)):return None
        if command=='/olx_stop':
            if not row:
                row=SourceProbe(id=STATE,status='paused_by_owner',checked_at=time.time(),requests=0,
                    result={'until':settings.olx_owner_feed_until,'attempts':0,'accepted':0,'receipts':[]});db.add(row)
            else:row.status='paused_by_owner'
            db.commit()
    info=preflight(engine,settings)
    technical=profile_ready(load_profile(),time.time())
    text=('🟠 OLX зупинено. AUTO.RIA продовжує працювати.' if command=='/olx_stop' else
        '🟠 OLX • лише власник\nСтан: '+str(info.get('status','вимкнено'))+
        '\nПідтверджено Telegram: '+str(info.get('state',{}).get('accepted',0))+
        '\nПеревірена оцінка готова: '+('так' if technical else 'ні')+
        '\nПочатковий пакет: максимум 3 спроби. Постійний збір ще не запущений.'+
        '\nІнтервал: '+str(interval())+' с\nЛіміти: 12 GET/год, 60 GET/день, 160 MiB/день.'+
        '\n/olx_stop — зупинити лише OLX.')
    return billing.message(settings.admin_telegram_id,text)
