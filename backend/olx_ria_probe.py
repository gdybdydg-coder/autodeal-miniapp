"""Once-only authorized owner probe. No sender and no RIA shared-state writes.

Credential use stays on server. Default off; separate durable reservation ledger.
The resulting API observations are never market-readiness or sale-price proof.
"""
import copy
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from . import billing, paid_source_access, olx_owner_feed as feed
from .models import SourceProbe, User
from .olx_market import ria_provider
from .olx_market.observations import enrich
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot

STATE='olx-ria-probe-20261005-1332-v1'
PLAN=Path(__file__).with_name('olx_ria_probe_plan.json')
LOG=logging.getLogger('uvicorn.error')
MAX_CALLS=30
MAX_AI=6
CAP=1024*1024
SHUTDOWN=threading.Event()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def enabled():return os.getenv('OLX_RIA_PROBE_ENABLED')=='true'


def allowed(engine,settings,plan):
    if (SHUTDOWN.is_set() or not enabled() or not settings.live or not billing.verified_admin(settings)
            or not paid_source_access.strict(engine) or time.time()>=plan['until']
            or not settings.auto_ria_api_key or not settings.auto_ria_user_id):return False
    with Session(engine) as db:
        owner=db.get(User,settings.admin_telegram_id)
        control=db.get(SourceProbe,feed.STATE)
        if control and control.status=='paused_by_owner':return False
        if not owner or not owner.ready or not paid_source_access.allowed(db,owner.id,time.time()):return False
        return any(s['id']==plan['search_id'] and s['fingerprint']==plan['fingerprint']
                   for s in feed.own_searches(db,settings))


def initialize(engine,settings,plan):
    if not allowed(engine,settings,plan):return False
    with Session(engine) as db:
        previous=db.get(SourceProbe,STATE,with_for_update=True)
        if previous:
            # One reviewed correction may continue the SAME spent ledger.
            # No crash/source hold replay, reset, or automatic phase advance.
            if (plan.get('phase')!=2 or previous.result.get('phase',1)!=1
                    or previous.status!='technical_hold'
                    or previous.result.get('error')!='dictionary_mapping_ambiguous_or_missing'
                    or previous.result.get('owner_id')!=settings.admin_telegram_id
                    or previous.result.get('fingerprint')!=plan['fingerprint']):return False
            previous.status='checking'
            previous.result={**previous.result,'phase':2,'previous_error':previous.result['error'],
                             'phase2_started_at':time.time(),'error':None}
            db.commit();return True
        db.add(SourceProbe(id=STATE,status='checking',requests=0,checked_at=time.time(),
            result={'budget':{'maximum_ria_calls':MAX_CALLS,'maximum_ai_calls':MAX_AI},
                'owner_id':settings.admin_telegram_id,'search_id':plan['search_id'],
                'fingerprint':plan['fingerprint'],'until':plan['until'],
                'calls':[],'olx_calls':[],'observations':[],
                'technical_ready':False,'telegram_calls':0,'phase':1}))
        try:db.commit();return True
        except IntegrityError:db.rollback();return False


def reserve(engine,settings,plan,kind,path,body=None):
    if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
    with Session(engine) as db:
        r=db.get(SourceProbe,STATE,with_for_update=True)
        if not r or r.status!='checking':raise ValueError('probe_not_active')
        d=copy.deepcopy(r.result)
        if len(d['calls'])>=MAX_CALLS or (kind=='ai' and sum(c['kind']=='ai' for c in d['calls'])>=MAX_AI):raise ValueError('probe_budget_exhausted')
        call={'kind':kind,'path':path,'body':body,'at':time.time(),'status':'reserved','bytes':0}
        d['calls'].append(call);r.result=d;r.requests=len(d['calls']);db.commit()
        return len(d['calls'])-1


def transport(settings,path,body=None,params=None):
    # All paths originate from the documented allowlist in run_once.
    query={'api_key':settings.auto_ria_api_key,**(params or {})}
    if body is not None:query['user_id']=settings.auto_ria_user_id
    url='https://developers.ria.com/'+path+'?'+urlencode(query)
    req=Request(url,data=json.dumps(body).encode() if body is not None else None,
                headers={'Accept':'application/json','Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect()).open(req,timeout=20) as response:
            raw=response.read(CAP+1)
            if len(raw)>CAP:raise ValueError('provider_byte_cap_exceeded')
            return response.status,json.loads(raw),len(raw)
    except HTTPError as e:return e.code,None,0
    except Exception:raise ValueError('provider_response_unknown_no_retry') from None


def request(engine,settings,plan,path,body=None,params=None,fetch=transport):
    kind='ai' if body is not None else 'dictionary'
    index=reserve(engine,settings,plan,kind,path,body)
    if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
    code,data,size=fetch(settings,path,body,params)
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
        d['calls'][index].update(status='http_'+str(code),bytes=size);row.result=d;db.commit()
    if code in (401,403,429):raise ValueError('provider_hold_http_'+str(code))
    if code!=200:raise ValueError('provider_http_'+str(code))
    time.sleep(4)
    return data


def catalog_rows(data):
    if not isinstance(data,list):raise ValueError('dictionary_shape_unverified')
    rows=[]
    for r in data:
        if not isinstance(r,dict):continue
        if isinstance(r.get('generations'),list):
            for g in r['generations']:
                if (isinstance(g,dict) and type(g.get('generationId')) is int
                        and g['generationId']>0 and isinstance(g.get('name'),str)):
                    rows.append((g['generationId'],g['name']))
            continue
        id=r.get('value',r.get('id'));name=r.get('name')
        if type(id) is int and id>0 and isinstance(name,str):rows.append((id,name))
    return rows


def exact_id(rows,names):
    ids={id for id,name in rows if name.casefold().strip() in {n.casefold() for n in names}}
    if len(ids)!=1:raise ValueError('dictionary_mapping_ambiguous_or_missing')
    return str(ids.pop())


def run_once(engine,settings,plan=None,fetch=transport,olx_fetch=feed.source_fetch):
    if not enabled():return
    if plan is None:
        try:plan=json.loads(PLAN.read_text())
        except (OSError,ValueError):return
    if not initialize(engine,settings,plan):return
    status='completed_observations_not_admitted';error=None
    try:
        paths={'brands':'auto/categories/1/marks','models':'auto/categories/1/marks/70/models',
               'body':'auto/categories/1/bodystyles','fuel':'auto/type',
               'gear':'auto/categories/1/gearboxes','drive':'auto/categories/1/driverTypes',
               'generation':'generations/by/models/652/generations'}
        catalogs={}
        for key,path in paths.items():
            evidence=plan.get('dictionary_evidence',{}).get(key)
            if plan.get('phase')==2 and evidence:
                if (evidence['path']!=path or not 0<=time.time()-evidence['checked_at']<=86400
                        or not evidence.get('receipt_log_id')):raise ValueError('dictionary_receipt_invalid')
                catalogs[key]=[(id,name) for id,name in evidence['items']]
            else:catalogs[key]=catalog_rows(request(engine,settings,plan,path,fetch=fetch))
            # Only public dictionary IDs/labels, no credentials or private content.
            LOG.info('OLX RIA probe dictionary %s',json.dumps({'kind':key,'items':catalogs[key]},ensure_ascii=False))
        brand=exact_id(catalogs['brands'],['Skoda','Škoda'])
        model=exact_id(catalogs['models'],['Octavia'])
        if brand!='70' or model!='652':raise ValueError('pinned_dictionary_identity_changed')
        for entry in plan['candidates']:
            if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
                if len(d['olx_calls'])>=plan['maximum_olx_gets']:raise ValueError('olx_probe_budget_exhausted')
                d['olx_calls'].append({'id':entry['id'],'url':entry['url'],'status':'reserved','cap':feed.DETAIL_CAP})
                row.result=d;db.commit()
            if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
            code,raw,truncated=olx_fetch(entry['url'])
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
                d['olx_calls'][-1].update(status='http_'+str(code),bytes=len(raw),truncated=truncated)
                row.result=d;db.commit()
            if code in (401,403,429):raise ValueError('olx_source_hold_http_'+str(code))
            if code!=200 or truncated:raise ValueError('olx_detail_unavailable')
            car=enrich(raw,parse_detail_snapshot(raw,fetched_at=int(time.time()),truncated=False))['listing']
            if car['id']!=entry['id'] or not feed.same_detail_url(car['url'],entry['url']):raise ValueError('olx_identity_mismatch')
            body_id=exact_id(catalogs['body'],{'wagon':['Універсал','Универсал'],'liftback':['Ліфтбек','Лифтбек']}.get(car.get('body'),[]))
            fuel_names={'petrol':['Бензин'],'diesel':['Дизель']}.get(car.get('fuel'))
            fuel_id=exact_id(catalogs['fuel'],fuel_names) if fuel_names else None
            if fuel_id is None and plan.get('phase')!=2:raise ValueError('dictionary_mapping_ambiguous_or_missing')
            gear_id=exact_id(catalogs['gear'],{'manual':['Ручна / Механіка','Ручна/Механіка','Ручная / Механика']}.get(car.get('transmission'),[]))
            # Diagnostic criteria deliberately do not guess ambiguous generation IDs.
            # Never admissible as a card valuation until all missing criteria reviewed.
            params={'categoryId':'1','brandId':brand,'modelId':model,'bodyId':body_id,
                    'gearBoxId':gear_id,
                    'year':{'gte':str(car['year']),'lte':str(car['year'])},
                    'mileage':{'gte':str(car['mileage_km']/1000),'lte':str(car['mileage_km']/1000)},
                    'engineVolume':{'gte':str(car['engine_cc']/1000),'lte':str(car['engine_cc']/1000)}}
            if car.get('power_hp') is not None:params['power']=car['power_hp']
            if fuel_id is not None:params['fuelId']=fuel_id
            if plan.get('phase')==2:
                generation=exact_id(catalogs['generation'],['II покоління/'+car.get('generation','')])
                drive=exact_id(catalogs['drive'],{'front':['Передній'],'rear':['Задній'],'all':['Повний']}.get(car.get('drive_type'),[]))
                params.update(generationId=generation,driveId=drive)
            body={'langId':4,'period':168,'params':params}
            data=request(engine,settings,plan,'auto/ai-avarage-price/',body,fetch=fetch)
            blocks=[b for b in data.get('statisticData',[]) if isinstance(b,dict) and b.get('type')=='avgPrice']
            avg=blocks[0].get('price',{}).get('USD') if len(blocks)==1 else None
            peers=data.get('similarCars',[])
            safe_peers=[]
            for p in peers if isinstance(peers,list) else []:
                if not isinstance(p,dict) or type(p.get('id')) is not int:continue
                safe={k:p.get(k) for k in ('id','year','raceInt')}
                for key,fields in (('brand',('id','eng')),('model',('id','eng')),
                                   ('fuel',('name',)),('gearbox',('name',))):
                    value=p.get(key)
                    safe[key]={k:value.get(k) for k in fields} if isinstance(value,dict) else None
                safe_peers.append(safe)
            observation={'id':car['id'],'checked_at':car['checked_at'],'asking':car.get('price'),
                'currency':car.get('currency'),'condition':car.get('research_condition'),
                'request':body,'average_usd':avg,'provider_range_fraction':blocks[0].get('avgValueRange') if len(blocks)==1 else None,
                'provider_quantity':blocks[0].get('quantityAdv') if len(blocks)==1 else None,
                'peers':safe_peers,'technical_ready':False,'status':'diagnostic_only_compatibility_pending',
                'missing_criteria':(['fuel_subtype_mapping'] if fuel_id is None else [])+
                    ([] if plan.get('phase')==2 else ['generation_mapping','drive_mapping'])+
                    ['condition_compatibility','independent_compatible_peer_details','frozen_control_validation']}
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
                d['olx_calls'][-1].update(status='http_200',bytes=len(raw))
                d['observations'].append(observation);row.result=d;db.commit()
            LOG.info('OLX RIA probe observation %s',json.dumps(observation,ensure_ascii=False))
    except Exception as exc:
        status='source_hold' if isinstance(exc,ValueError) and ('hold_http_' in str(exc) or 'source_hold_challenge' in str(exc)) else 'technical_hold'
        error=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        row.status=status;row.result={**row.result,'error':error,'finished_at':time.time()};db.commit()
        result={'status':status,'error':error,'ria_calls':row.requests,
                'ai_calls':sum(c['kind']=='ai' for c in row.result['calls']),
                'olx_gets':len(row.result['olx_calls']),'observations':len(row.result['observations']),
                'technical_ready':False,'telegram_calls':0}
    LOG.info('OLX RIA probe result %s',json.dumps(result));return result


async def run(engine,settings,stop):
    SHUTDOWN.clear()
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='olx-ria-probe') as executor:
        task=asyncio.get_running_loop().run_in_executor(executor,run_once,engine,settings)
        stopping=asyncio.create_task(stop.wait())
        await asyncio.wait((task,stopping),return_when=asyncio.FIRST_COMPLETED)
        if stopping.done():SHUTDOWN.set()
        else:stopping.cancel()
        try:await task
        except Exception as exc:
            LOG.warning('OLX RIA probe isolated failure (%s)',type(exc).__name__)
