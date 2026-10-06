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
    if (plan.get('phase')==6 and (os.getenv('OLX_FOCUSED_OWNER_SEND_ENABLED')!='true'
            or plan.get('owner_authorized') is not True or plan.get('client_authorized') is not False)):
        return False
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
            phase6=(plan.get('phase')==6 and os.getenv('OLX_FOCUSED_OWNER_SEND_ENABLED')=='true'
                    and plan.get('owner_authorized') is True and plan.get('client_authorized') is False
                    and previous.result.get('phase')==5
                    and previous.status=='completed_observations_not_admitted'
                    and previous.result.get('error') is None)
            phase3=(plan.get('phase')==3 and previous.result.get('phase')==2
                    and previous.status=='completed_observations_not_admitted'
                    and previous.result.get('error') is None)
            phase4=(plan.get('phase')==4 and previous.result.get('phase')==3
                    and previous.status=='completed_reference_audit_not_admitted'
                    and previous.result.get('error') is None)
            last=previous.result.get('olx_calls',[{}])[-1] if previous.result.get('olx_calls') else {}
            entries=plan.get('candidates',[])
            phase5=(plan.get('phase')==5 and previous.result.get('phase')==4
                    and previous.status=='technical_hold'
                    and previous.result.get('error')=='olx_detail_unavailable'
                    and last.get('status')=='http_302' and last.get('truncated') is False
                    and len(entries)==1 and entries[0].get('id')==last.get('id')
                    and entries[0].get('url')!=last.get('url')
                    and feed.same_detail_url(entries[0].get('url'),last.get('url')))
            phase2=(plan.get('phase')==2 and previous.result.get('phase',1)==1
                    and previous.status=='technical_hold'
                    and previous.result.get('error')=='dictionary_mapping_ambiguous_or_missing')
            if ((not phase6 and not phase5 and not phase4 and not phase3 and not phase2) or previous.result.get('phase',1) not in (1,2,3,4,5)
                    or previous.result.get('owner_id')!=settings.admin_telegram_id
                    or previous.result.get('fingerprint')!=plan['fingerprint']):return False
            previous.status='checking'
            previous.result={**previous.result,'phase':plan['phase'],'previous_error':previous.result['error'],
                             'phase_started_at':time.time(),'error':None,
                             **({'previous_until':previous.result.get('until'),'until':plan['until'],
                                  'focused_authorized_at':plan.get('focused_authorized_at')} if phase6 else {})}
            db.commit();return True
        if plan.get('phase',1)!=1:return False
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
    kind='ai' if body is not None else ('search' if path=='auto/search' else 'detail' if path=='auto/info' else 'dictionary')
    index=reserve(engine,settings,plan,kind,path,body)
    if params:
        with Session(engine) as db:
            row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
            d['calls'][index]['params']=params;row.result=d;db.commit()
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


def audit_references(engine,settings,plan,fetch):
    """Freeze search membership before detail prices; never admit readiness here."""
    from .ria_search import parse_ids, parse_car
    params=plan['reference_search']
    if params.get('marka_id[0]')!=70 or params.get('model_id[0]')!=652:
        raise ValueError('reference_identity_changed')
    data=request(engine,settings,plan,'auto/search',params=params,fetch=fetch)
    found=parse_ids(data)
    ids=found['ids'][:18]
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
        d['reference_membership']={'ids':ids,'control_ids':ids[:3],
            'frozen_at':time.time(),'total_search_matches':found['total'],
            'selection':'source order, no detail-price ranking','technical_ready':False}
        d['reference_details']=[];row.result=d;db.commit()
    LOG.info('OLX RIA reference membership %s',json.dumps(d['reference_membership']))
    for sid in ids:
        data=request(engine,settings,plan,'auto/info',params={'auto_id':sid},fetch=fetch)
        try:
            car=parse_car(data,sid)
            safe={k:car.get(k) for k in ('id','price_usd','year','url','brand_id','model_id',
                'body_id','fuel_id','gear_id','generation_id','modification_id','engine_cc',
                'mileage','vehicle_key','comparable_condition','condition_exclusions','category_id','observed_at')}
            auto=data.get('autoData',{})
            # Log observed non-sensitive schema names, never VIN/contact/description values.
            safe['auto_schema_keys']=sorted(str(k) for k in auto)
            safe['explicit_numeric_attributes']={k:auto[k] for k in ('driveId','driveTypeId','power','powerHp','horsePower')
                if type(auto.get(k)) in (int,float) and 0<auto[k]<10000}
            expected={'brand_id':70,'model_id':652,'generation_id':3133,'body_id':2,
                      'fuel_id':4,'gear_id':1,'engine_cc':1600,'category_id':1}
            safe['mismatches']=[k for k,v in expected.items() if safe.get(k)!=v]
            if not safe.get('mileage') or not 224000<=safe['mileage']<=284000:safe['mismatches'].append('mileage')
            if not safe.get('year') or not 2004<=safe['year']<=2006:safe['mismatches'].append('year')
            if not safe.get('comparable_condition'):safe['mismatches'].append('condition')
            safe['unknown_critical']=([] if safe['explicit_numeric_attributes'].get('driveId')==2 else ['drive'])+['power','target_fuel_subtype','cross_source_target_identity']
            safe['role']='control' if sid in ids[:3] else 'reference_candidate'
            safe['independent_identity_available']=bool(safe.get('vehicle_key'))
            safe['status']='not_admitted_pending_critical_compatibility'
        except Exception as exc:
            safe={'id':sid,'status':'detail_rejected','error':type(exc).__name__}
        with Session(engine) as db:
            row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
            d['reference_details'].append(safe);row.result=d;db.commit()
        LOG.info('OLX RIA reference detail %s',json.dumps(safe,ensure_ascii=False))


def audit_saved_comparisons(engine,settings,plan):
    """Read existing comparison-only cache; never create or refresh shared rows."""
    if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
    from sqlalchemy import select, text
    from .models import ValuationPeer
    now=time.time()
    with Session(engine) as db:
        if db.bind.dialect.name=='postgresql':db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        rows=list(db.scalars(select(ValuationPeer).where(ValuationPeer.available.is_(True),
            ValuationPeer.observed_at>=now-900,
            ValuationPeer.car['brand_id'].as_integer()==70,
            ValuationPeer.car['model_id'].as_integer()==652)
            .order_by(ValuationPeer.observed_at.desc(),ValuationPeer.source_id).limit(200)))
        summaries=[]
        for r in rows:
            c=r.car
            summaries.append({k:c.get(k) for k in ('id','url','brand_id','model_id','generation_id',
                'body_id','fuel_id','gear_id','engine_cc','mileage','year','price_usd','observed_at',
                'modification_id','modification_name','vehicle_key','comparable_condition','condition_exclusions')})
        db.rollback()
    audit={'kind':'read_only_existing_comparison_cache','fresh_octavia_rows':len(summaries),
        'truncated':len(summaries)==200,'maximum_age_seconds':900,'checked_at':now,
        'shared_state_writes':0,'paid_api_calls':0,'technical_ready':False}
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        row.result={**row.result,'comparison_cache_audit':audit,'saved_comparison_peers':summaries};db.commit()
    LOG.info('OLX RIA comparison cache audit %s',json.dumps(audit))


def run_once(engine,settings,plan=None,fetch=transport,olx_fetch=feed.source_fetch):
    if not enabled():return
    if plan is None:
        try:plan=json.loads(PLAN.read_text())
        except (OSError,ValueError):return
    if not initialize(engine,settings,plan):
        if allowed(engine,settings,plan):report_completed(engine)
        return
    status='completed_observations_not_admitted';error=None
    try:
        if plan.get('phase')==6:
            return focused_once(engine,settings,plan,fetch,olx_fetch)
        if plan.get('phase')==3:
            audit_references(engine,settings,plan,fetch)
            return finish(engine,'completed_reference_audit_not_admitted',None)
        if plan.get('phase')==4:audit_saved_comparisons(engine,settings,plan)
        paths={'brands':'auto/categories/1/marks','models':'auto/categories/1/marks/70/models',
               'body':'auto/categories/1/bodystyles','fuel':'auto/type',
               'gear':'auto/categories/1/gearboxes','drive':'auto/categories/1/driverTypes',
               'generation':'generations/by/models/652/generations'}
        catalogs={}
        for key,path in paths.items():
            evidence=plan.get('dictionary_evidence',{}).get(key)
            if plan.get('phase') in (2,4,5) and evidence:
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
            if plan.get('phase') in (4,5):
                if (car.get('category')!='whole_passenger_car'
                        or car.get('eligibility_review',{}).get('status')!='allowed'
                        or not car.get('observed_asking_display',{}).get('description_reviewed_in_full')):
                    raise ValueError('fresh_candidate_full_eligibility_pending')
                safe_target={k:car.get(k) for k in ('id','url','brand','model','year','mileage_km','engine_cc',
                    'fuel','transmission','body','generation','drive_type','power_hp','modification',
                    'research_condition','price','currency','checked_at','vehicle_key','region')}
                safe_target['technical_ready']=False
                with Session(engine) as db:
                    row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
                    d.setdefault('fresh_candidates',[]).append(safe_target);row.result=d;db.commit()
                LOG.info('OLX RIA fresh candidate %s',json.dumps(safe_target,ensure_ascii=False))
            body_id=exact_id(catalogs['body'],{'wagon':['Універсал','Универсал'],'liftback':['Ліфтбек','Лифтбек']}.get(car.get('body'),[]))
            fuel_names={'petrol':['Бензин'],'diesel':['Дизель']}.get(car.get('fuel'))
            fuel_id=exact_id(catalogs['fuel'],fuel_names) if fuel_names else None
            if fuel_id is None and plan.get('phase') not in (2,4,5):raise ValueError('dictionary_mapping_ambiguous_or_missing')
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
            if plan.get('phase')==4:
                params['driveId']=exact_id(catalogs['drive'],{'front':['Передній'],'rear':['Задній'],'all':['Повний']}.get(car.get('drive_type'),[]))
                # A5 names the family. Do not infer pre-FL vs FL by model year.
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
                    ([] if plan.get('phase')==2 else ['generation_mapping'] if plan.get('phase') in (4,5) else ['generation_mapping','drive_mapping'])+
                    ([] if car.get('power_hp') is not None else ['power_mapping'])+
                    ['condition_compatibility','independent_compatible_peer_details','frozen_control_validation']}
            with Session(engine) as db:
                row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
                d['olx_calls'][-1].update(status='http_200',bytes=len(raw))
                d['observations'].append(observation);row.result=d;db.commit()
            LOG.info('OLX RIA probe observation %s',json.dumps(observation,ensure_ascii=False))
    except Exception as exc:
        status='source_hold' if isinstance(exc,ValueError) and ('hold_http_' in str(exc) or 'source_hold_challenge' in str(exc)) else 'technical_hold'
        error=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    return finish(engine,status,error)


def report_completed(engine):
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE)
        if not row or not (row.status.startswith('completed_') or row.status in ('technical_hold','source_hold')):return
        d=row.result
        calls=d.get('calls',[]);olx=d.get('olx_calls',[])
        summary={'status':row.status,'error':d.get('error'),'ria_calls':row.requests,
            'ai_calls':sum(c.get('kind')=='ai' for c in calls),
            'ria_bytes_known':sum(c.get('bytes',0) for c in calls),
            'olx_gets':len(olx),'olx_bytes_known':sum(c.get('bytes',0) for c in olx),
            'olx_bytes_unknown_receipts':sum('bytes' not in c or c.get('status')=='reserved' for c in olx),
            'reference_details':len(d.get('reference_details',[])),
            'last_olx_receipt':{k:olx[-1].get(k) for k in ('id','status','bytes','truncated','cap')} if olx else None,
            'technical_ready':d.get('technical_ready',False),'telegram_calls':d.get('telegram_calls',0),'focused_receipt':d.get('focused_receipt'),'restart_additional_requests':0}
    LOG.info('OLX RIA completed receipt %s',json.dumps(summary));return summary


def finish(engine,status,error):
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        row.status=status;row.result={**row.result,'error':error,'finished_at':time.time()};db.commit()
        result={'status':status,'error':error,'ria_calls':row.requests,
                'ai_calls':sum(c['kind']=='ai' for c in row.result['calls']),
                'olx_gets':len(row.result['olx_calls']),'observations':len(row.result['observations']),
                'technical_ready':row.result.get('technical_ready',False),'telegram_calls':row.result.get('telegram_calls',0)}
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


def focused_once(engine,settings,plan,fetch,olx_fetch):
    """Single current target; same historical spent RIA ledger, no replay."""
    from . import olx_focused_provider as focus
    entry=plan['candidates'][0]
    if (len(plan['candidates'])!=1 or plan['maximum_olx_gets']!=6
            or plan.get('focused_method') not in (None,focus.VERSION) or os.getenv(focus.ARM)!='true'):raise ValueError('focused_arm_missing')
    if not allowed(engine,settings,plan):raise ValueError('owner_access_or_stop_blocked')
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True);d=copy.deepcopy(row.result)
        if len(d['olx_calls'])>=plan['maximum_olx_gets']:raise ValueError('olx_probe_budget_exhausted')
        d['olx_calls'].append({'id':entry['id'],'url':entry['url'],'status':'reserved','cap':feed.DETAIL_CAP,'phase':6})
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
    paths={'brand':'brands','model':'models','body':'body','fuel':'fuel','transmission':'gear','drive_type':'drive'}
    labels={'brand':['Skoda','Škoda'],'model':['Octavia'],'body':{'wagon':['Універсал'],'liftback':['Ліфтбек']}.get(car.get('body'),[]),
        'fuel':{'diesel':['Дизель'],'petrol':['Бензин']}.get(car.get('fuel'),[]),
        'transmission':{'manual':['Ручна / Механіка'],'automatic':['Автомат']}.get(car.get('transmission'),[]),
        'drive_type':{'front':['Передній'],'rear':['Задній'],'all':['Повний']}.get(car.get('drive_type'),[])}
    mapping={}
    for field,key in paths.items():
        if car.get(field) is None:continue
        e=plan['dictionary_evidence'][key]
        if not e.get('receipt_log_id'):raise ValueError('dictionary_receipt_missing')
        mapping[field]={'value':car[field],'id':int(exact_id(e['items'],labels[field])),
            'dictionary_url':'https://developers.ria.com/'+e['path'],'checked_at':e['checked_at']}
    prepared=focus.prepare(car,mapping,time.time())
    with Session(engine) as db:
        search=next((s for s in feed.own_searches(db,settings) if s['id']==plan['search_id'] and s['fingerprint']==plan['fingerprint']),None)
    if not search or not feed.filter_reasons(car,search['filters'],None,time.time())['match']:raise ValueError('owner_filter_contradiction')
    data=request(engine,settings,plan,'auto/ai-avarage-price/',prepared['body'],fetch=fetch)
    assessment=focus.assess(data,prepared,car,search,time.time())
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        row.result={**row.result,'focused_assessment':assessment,'technical_ready':True};db.commit()
    LOG.info('OLX focused provider assessment %s',json.dumps({'id':car['id'],**assessment},ensure_ascii=False))
    receipt=focus.send(engine,settings,plan,car,assessment,allowed) if assessment['eligible_deal'] else None
    with Session(engine) as db:
        row=db.get(SourceProbe,STATE,with_for_update=True)
        row.result={**row.result,'focused_receipt':receipt,'telegram_calls':int(receipt is not None and receipt.get('method') is not None)};db.commit()
    LOG.info('OLX focused owner receipt %s',json.dumps(receipt or {'source_id':car['id'],'status':'not_attempted','reason':'below_threshold_or_previous_attempt'}))
    return finish(engine,'completed_focused_owner',None)
