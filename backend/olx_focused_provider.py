"""One owner card using a fresh documented AUTO.RIA parameter estimate.
Separate from both AUTO.RIA valuation and the OLX local peer estimator.
Condition is descriptive, never a rejection. No polling or subscription writes.
"""
import copy
import hashlib
import json
import os
import time
from decimal import Decimal
from html import escape
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from . import billing, paid_source_access, telegram_setup, olx_owner_feed as feed
from .models import SourceProbe, User
from .olx_market import ria_provider as provider
from .olx_market.observations import asking_price_reasons

VERSION='olx-ria-provider-lower-bound-minus5-v2'
ARM='OLX_FOCUSED_OWNER_SEND_ENABLED'


def prepare(car,mapping,now):
    if (car.get('source')!='olx' or not str(car.get('id','')).isdigit()
            or not feed.public_detail_url(car.get('url'))
            or type(car.get('checked_at')) is not int
            or not 0<=now-car['checked_at']<=300 or asking_price_reasons(car)):
        raise ValueError('fresh_whole_car_price_unverified')
    params={'categoryId':'1'}
    used=[];omitted=[]
    # Family A5 alone does not resolve pre-FL vs FL. Do not fabricate it.
    for field,key in provider.IDS.items():
        if field=='generation' and not car.get('generation_variant'):
            omitted.append('generation');continue
        if car.get(field) is None:
            if field in ('brand','model'):raise ValueError('core_identity_missing')
            omitted.append(field);continue
        params[key]=provider.official_mapping(car,mapping,field,now);used.append(field)
    year=car.get('year')
    if type(year) is not int or not 1900<=year<=2100:raise ValueError('year_missing')
    params['year']={'gte':str(year),'lte':str(year)}
    for field,key in (('mileage_km','mileage'),('engine_cc','engineVolume')):
        if car.get(field) is None:omitted.append(field);continue
        value=format((provider.positive(car[field])/1000).normalize(),'f')
        params[key]={'gte':value,'lte':value}
    if car.get('power_hp') is not None:params['power']=float(provider.positive(car['power_hp']))
    else:omitted.append('power_hp')
    body={'langId':4,'period':168,'params':params}
    return {'version':VERSION,'source':'olx','source_id':car['id'],
        'target_binding':provider.binding(car),'request_sha256':provider.digest(body),
        'body':body,'omitted_criteria':omitted,'observed_at':now}


def assess(data,request,car,search,now):
    if (request.get('version')!=VERSION or request.get('target_binding')!=provider.binding(car)
            or request.get('source_id')!=car.get('id')
            or request.get('request_sha256')!=provider.digest(request.get('body'))
            or not 0<=now-car['checked_at']<=300 or asking_price_reasons(car)):
        raise ValueError('response_target_binding_invalid')
    if car.get('currency')!='USD':raise ValueError('focused_currency_requires_verified_fx')
    if not feed.filter_reasons(car,search['filters'],None,now)['match']:raise ValueError('owner_filter_contradiction')
    blocks=[b for b in data.get('statisticData',[]) if isinstance(b,dict) and b.get('type')=='avgPrice'] if isinstance(data,dict) else []
    if len(blocks)!=1:raise ValueError('provider_average_ambiguous')
    b=blocks[0];average=provider.positive(b.get('price',{}).get('USD'))
    radius=provider.positive(b.get('avgValueRange'))
    if radius>=1:raise ValueError('provider_range_invalid')
    quantity=b.get('quantityAdv')
    if type(quantity) is not int or quantity<8:raise ValueError('provider_sample_insufficient')
    lower=average*(1-radius)
    reference=lower*Decimal('0.95')
    discount=provider.discount_percent(reference,car['price'])
    ids=sorted({str(p['id']) for p in data.get('similarCars',[]) if isinstance(p,dict) and type(p.get('id')) is int and p['id']>0})
    return {'version':VERSION,'reference_usd':str(reference),'provider_average_usd':str(average),
        'provider_lower_bound_usd':str(lower),'pricing_method':'provider_lower_bound_minus_5_percent',
        'range_usd':{'low':str(average*(1-radius)),'high':str(average*(1+radius))},
        'discount_percent':str(discount),'asking_usd':str(car['price']),
        'provider_quantity':quantity,'returned_ad_ids':ids,
        'independently_reviewed_compatible_analogs':None,
        'omitted_criteria':request['omitted_criteria'],'request':request,
        'observed_at':now,'asking_price_not_sale_price':True,'extra_margin_percent':5,
        'threshold':search['threshold'],'eligible_deal':discount>=Decimal(str(search['threshold']))}


def caption(car,a):
    money=lambda n:format(Decimal(n).quantize(Decimal('1')),',').replace(',',' ')
    attrs=[]
    for key,labels in (('fuel',{'diesel':'дизель','petrol':'бензин'}),('transmission',{'manual':'механіка','automatic':'автомат'})):
        if car.get(key):attrs.append(labels.get(car[key],car[key]))
    if car.get('engine_cc'):attrs.append(str(car['engine_cc'])+' см³')
    if car.get('mileage_km'):attrs.append(money(car['mileage_km'])+' км')
    return '\n'.join(['🟠 <b>OLX • тест лише для власника</b>',
        '🚘 '+escape(' '.join(str(car.get(k,'')) for k in ('brand','model','year'))),
        '💵 Ціна: '+money(a['asking_usd'])+' USD',
        '📊 Ринкова вартість ≈ '+money(a['reference_usd'])+' USD',
        '📉 Нижче ринкового орієнтира: '+str(Decimal(a['discount_percent']).quantize(Decimal('.1')))+'%',
        '📍 '+escape(car.get('locality') or car.get('region') or ''),
        '⚙️ '+escape(' · '.join(attrs)),
        '🔎 Джерело оцінки: AUTO.RIA AI за параметрами авто. Кількість пропозицій у відповіді: '+str(a['provider_quantity'])+'.',
        'Діапазон: '+money(a['range_usd']['low'])+'–'+money(a['range_usd']['high'])+' USD.',
        'Ціни пропозицій, не підтверджені продажі. Стан не коригується.'+
        (' Точний варіант покоління не підтверджений.' if 'generation' in a['omitted_criteria'] else ''),
        '/olx_stop — зупинити лише OLX.'])


def send(engine,settings,plan,car,a,access,sender=telegram_setup.call):
    """At most once, owner-only, fresh access again under the payment lock."""
    if not a['eligible_deal'] or not access(engine,settings,plan):return None
    key='olx-owner-car-'+hashlib.sha256((str(settings.admin_telegram_id)+':'+car['id']).encode()).hexdigest()[:32]
    with Session(engine) as db:
        if feed.legacy.prior_attempt(db,car['id'],settings.admin_telegram_id):return None
        db.add(SourceProbe(id=key,status='sending',requests=1,checked_at=time.time(),
            result={'source':'olx','source_id':car['id'],'assessment':a,'owner_only':True}))
        try:db.commit()
        except IntegrityError:db.rollback();return None
    response={'not_attempted':True};method=None
    try:
        if access(engine,settings,plan):
            check=sender(settings.bot_token,'getChat',{'chat_id':settings.admin_telegram_id},timeout=5)
            chat=check.get('result',{})
            if (check.get('ok') is True and type(chat.get('id')) is int and chat['id']==settings.admin_telegram_id and chat.get('type')=='private'):
                with Session(engine) as db:
                    billing.control(db,lock=True)
                    owner=db.get(User,settings.admin_telegram_id,with_for_update=True)
                    stopped=db.get(SourceProbe,feed.STATE)
                    searches=feed.own_searches(db,settings)
                    if (os.getenv(ARM)=='true' and owner and owner.ready
                            and paid_source_access.allowed(db,owner.id,time.time())
                            and not (stopped and stopped.status=='paused_by_owner')
                            and time.time()<plan['until'] and 0<=time.time()-car['checked_at']<=300
                            and any(s['id']==plan['search_id'] and s['fingerprint']==plan['fingerprint'] for s in searches)):
                        text=caption(car,a)
                        payload={'chat_id':settings.admin_telegram_id,'parse_mode':'HTML',
                            'reply_markup':{'inline_keyboard':[[{'text':'Переглянути на OLX','url':car['url']}]]}}
                        method='sendPhoto' if car.get('photos') and len(text)<=1024 else 'sendMessage'
                        payload.update({'photo':car['photos'][0],'caption':text} if method=='sendPhoto' else {'text':text,'link_preview_options':{'is_disabled':True}})
                        response=sender(settings.bot_token,method,payload,timeout=5)
                    db.rollback()
    except Exception:response={'uncertain':True} if method else {'not_attempted':True}
    r=response.get('result',{}) if isinstance(response,dict) else {};r=r if isinstance(r,dict) else {};chat=r.get('chat',{})
    accepted=response.get('ok') is True and type(r.get('message_id')) is int and r['message_id']>0 and chat.get('id')==settings.admin_telegram_id and chat.get('type')=='private'
    status='accepted' if accepted else 'not_attempted' if response.get('not_attempted') else 'rejected' if response.get('ok') is False else 'uncertain'
    receipt={'source_id':car['id'],'status':status,'method':method,'at':time.time(),'message_id':r.get('message_id') if accepted else None}
    with Session(engine) as db:
        row=db.get(SourceProbe,key);row.status=status;row.result={**row.result,'receipt':receipt};db.commit()
    return receipt
