import copy,json
from pathlib import Path
from datetime import datetime,timedelta
from decimal import Decimal
import pytest
from .fx_policy import *
from .candidates import select_candidates,filter_reasons
from .observations import asking_price_reasons,enrich
from .valuation import estimate,assess_dataset

NOW=datetime.fromisoformat('2026-10-04T22:30:00+03:00')
EPOCH=int(NOW.timestamp())

def body(provider='privat_nbu'):
    if provider=='nbu':return json.dumps([{'cc':'USD','r030':840,'units':1,'exchangedate':'04.10.2026','calcdate':'02.10.2026','rate_per_unit':'44.8333','rate':'44.8333'}]).encode()
    if provider=='privat_nbu':return json.dumps({'date':'04.10.2026','bank':'PB','baseCurrency':980,'baseCurrencyLit':'UAH','exchangeRate':[{'baseCurrency':'UAH','currency':'USD','saleRateNB':'44.8333','purchaseRateNB':'44.8333','saleRate':'45.2','purchaseRate':'44.6'}]}).encode()
    return json.dumps([{'currencyCodeA':840,'currencyCodeB':980,'date':int(NOW.replace(hour=0,minute=1).timestamp()),'rateBuy':'44.8','rateSell':'45.1998'}]).encode()


def nbu_all_body(*,day='04.10.2026'):
    return json.dumps([
        {'r030':840,'txt':'Долар США','rate':'44.8333','cc':'USD','exchangedate':day},
        {'r030':978,'txt':'Євро','rate':'52.5000','cc':'EUR','exchangedate':day},
    ]).encode()

def car(id='target',price='6000',**extra):
    c=dict(source='olx',id=id,price=price,currency='USD',title='Skoda Octavia A5',brand='Skoda',model='Octavia',generation='A5',year=2008,body='wagon',fuel='diesel',transmission='manual',engine_cc=1900,drive_type='front',power_hp=105,mileage_km=300000,region='Хмельницька область',research_condition='seller_declared_running',category='whole_passenger_car',checked_at=EPOCH,publication_verified=True,published_at=EPOCH-10,eligibility_review={'status':'allowed'},price_review={'reasons':['full_price_unconfirmed']},observed_asking_display={'status':'corroborated_display','amount':price,'currency':'USD','description_reviewed_in_full':True,'reasons':[]},field_conflicts=[],price_conflicts=[],photos=[],url='https://www.olx.ua/d/uk/obyavlenie/test-IDexample.html')
    c.update(extra)
    if 'currency' in extra:c['observed_asking_display']['currency']=extra['currency']
    return c

def peers():return [car(str(i),str(p)) for i,p in enumerate((7800,7900,8000,8050,8100,8150,8200,8300))]

@pytest.mark.parametrize('provider',['nbu','privat_nbu','monobank_mid'])
def test_verified_provider_schema(provider):
    q=parse_quote(provider,body(provider),NOW)
    assert q.validate(NOW) is None
    assert q.rate==Decimal('44.9999' if provider=='monobank_mid' else '44.8333')

@pytest.mark.parametrize('broken',[b'',b'{}',b'null',b'invalid',b'[]',b'0'*262145])
def test_bad_payload(broken):
    with pytest.raises((ValueError,TypeError)):parse_quote('monobank_mid',broken,NOW)

@pytest.mark.parametrize('change',[{'currencyCodeA':978},{'currencyCodeB':840},{'rateBuy':'0'},{'rateSell':'NaN'},{'rateSell':'1000'},{'rateBuy':'50'},{'date':0},{'date':int(NOW.timestamp())+1}])
def test_wrong_mono_pair_rate_date(change):
    data=json.loads(body('monobank_mid'));data[0].update(change)
    with pytest.raises((ValueError,ArithmeticError)):parse_quote('monobank_mid',json.dumps(data),NOW)

@pytest.mark.parametrize('change',[{'date':'05.10.2026'},{'baseCurrency':840},{'baseCurrencyLit':'USD'},{'bank':'notPB'}])
def test_privat_header(change):
    data=json.loads(body());data.update(change)
    with pytest.raises(ValueError):parse_quote('privat_nbu',json.dumps(data),NOW)

def test_official_not_cash_and_single_quote_decimal():
    q=parse_quote('privat_nbu',body(),NOW)
    p=normalize({'price':'25500','currency':'UAH'},q,NOW)
    assert p['fx']['kind']=='nbu_official' and p['fx']['rate']=='44.8333'
    assert p['seller_original_currency'] is None
    assert abs(Decimal(p['usd_amount'])*q.rate-25500)<Decimal('1e-20')
    assert normalize(p,q,NOW)['usd_amount']==p['usd_amount']
    assert normalize({'price':'5000','currency':'USD'},None,NOW)['usd_amount']=='5000'
    assert normalize({'price':'5000','currency':'EUR'},q,NOW)['reason']=='eur_fx_missing'
    assert normalize({'price':'5000','currency':'EUR'},None,NOW)['reason']=='eur_fx_missing'


def test_same_date_official_eur_cross_rate_and_restore():
    q=parse_quote('nbu_all',nbu_all_body(),NOW)
    assert q.validate(NOW) is None and q.eur_rate==Decimal('52.5000')
    p=normalize({'price':'3500','currency':'EUR'},q,NOW)
    assert p['status']=='ready' and p['reason']=='converted_eur'
    with localcontext() as ctx:
        ctx.prec=DECIMAL_PRECISION
        assert Decimal(p['usd_amount'])==Decimal('3500')*Decimal('52.5000')/Decimal('44.8333')
    assert p['fx_conversion']['method']=='same_date_official_uah_cross'
    restored=Quote.restore(q.payload())
    assert restored.eur_rate==q.eur_rate and restored.basis==q.basis


@pytest.mark.parametrize('broken',[
    [{'r030':840,'rate':'44.8333','cc':'USD','exchangedate':'04.10.2026'}],
    [{'r030':840,'rate':'44.8333','cc':'USD','exchangedate':'04.10.2026'},
     {'r030':978,'rate':'52.5','cc':'EUR','exchangedate':'03.10.2026'}],
    [{'r030':840,'rate':'44.8333','cc':'USD','exchangedate':'04.10.2026'},
     {'r030':978,'rate':'NaN','cc':'EUR','exchangedate':'04.10.2026'}],
])
def test_eur_pair_requires_exact_unique_same_date_official_rows(broken):
    with pytest.raises((ValueError,ArithmeticError)):
        parse_quote('nbu_all',json.dumps(broken),NOW)


def test_saved_real_eur_examples_convert_without_bypassing_offer_review():
    raw=json.loads(Path('experiments/olx_research/examples/current-cohort.json').read_text())
    cars={c['id']:c for c in raw['listings']}
    q=parse_quote('nbu_all',nbu_all_body(),NOW)
    first=normalize(cars['932274776'],q,NOW)
    second=normalize(cars['931454683'],q,NOW)
    assert first['status']==second['status']=='ready'
    assert first['input_amount']=='3500' and second['input_amount']=='1000'
    assert 'eligibility_not_allowed' not in asking_price_reasons(cars['932274776'])
    assert 'eligibility_not_allowed' in asking_price_reasons(cars['931454683'])


def test_eur_pair_selection_is_one_bounded_shared_cached_get(tmp_path):
    calls=[]
    def transport(url):
        calls.append(url);return 200,nbu_all_body()
    book=RateBook(tmp_path/'eur-pair.db')
    q,trace=book.select_eur_pair(NOW,transport)
    assert q.eur_rate==Decimal('52.5000') and len(calls)==1
    assert trace[-1]['status']=='selected'
    again,_=book.select_eur_pair(NOW+timedelta(seconds=20),transport)
    assert again.basis==q.basis and len(calls)==1
    book.close()


def test_failed_eur_pair_does_not_relabel_usd_quote_or_retry_in_cooldown(tmp_path):
    calls=[]
    book=RateBook(tmp_path/'eur-fail.db')
    q,trace=book.select_eur_pair(NOW,lambda url:(calls.append(url) or (403,b'blocked')))
    assert q is None and trace[-1]['event']=='dependent_eur_work_pending'
    q,_=book.select_eur_pair(NOW+timedelta(seconds=20),lambda url:(calls.append(url) or (200,nbu_all_body())))
    assert q is None and len(calls)==1
    book.close()

def test_nbu_fallback_and_shared_restart_cache(tmp_path):
    calls=[]
    def transport(url):
        calls.append(url)
        return (403,b'blocked') if 'bank.gov.ua' in url else (200,body())
    p=tmp_path/'fx.sqlite';book=RateBook(p);q,trace=book.select(NOW,transport);book.close()
    assert len(calls)==2 and q.source.startswith('https://api.privatbank.ua')
    book=RateBook(p)
    for _ in range(50):assert book.select(NOW+timedelta(seconds=20),transport)[0].basis==q.basis
    assert len(calls)==2
    book.close()

def test_primary_and_all_fallback_paths(tmp_path):
    for successful in ('nbu','privat_nbu','monobank_mid',None):
        calls=[];book=RateBook(tmp_path/(str(successful)+'.db'))
        def transport(url):
            calls.append(url);provider='nbu' if 'bank.gov.ua' in url else 'privat_nbu' if 'privatbank' in url else 'monobank_mid'
            if provider==successful:return 200,body(provider)
            raise TimeoutError()
        q,trace=book.select(NOW,transport)
        assert len(calls)==({'nbu':1,'privat_nbu':2}.get(successful,3))
        assert (q is not None)==(successful is not None)
        book.select(NOW,transport);assert len(calls)<=3
        book.close()

def test_saved_weekend_fallback_then_monday_expiry(tmp_path):
    book=RateBook(tmp_path/'q.db');q,_=book.select(NOW,lambda u:(200,body('nbu')))
    later=NOW+timedelta(minutes=10)
    saved,trace=book.select(later,lambda u:(503,b''))
    assert saved.fetched_at==q.fetched_at and trace[-1]['event']=='fallback_saved_quote'
    failed,_=book.select(NOW+timedelta(days=1),lambda u:(503,b''));assert failed is None
    calls=[]
    book.select(NOW+timedelta(days=1,seconds=10),lambda u:calls.append(u))
    assert calls==[] # stale retained quote must not defeat negative cooldown
    book.close()

def test_rate_jump_and_bank_spread(tmp_path):
    book=RateBook(tmp_path/'q.db');q,_=book.select(NOW,lambda u:(200,body('nbu')))
    bad=json.loads(body('nbu'));bad[0]['rate']=bad[0]['rate_per_unit']='100'
    result,trace=book.select(NOW+timedelta(minutes=6),lambda u:(200,json.dumps(bad)))
    assert result.rate==q.rate and any(x.get('reason')=='fx_jump_over_15_percent' for x in trace)
    m=parse_quote('monobank_mid',body('monobank_mid'),NOW)
    p=normalize({'price':'25500','currency':'UAH'},m,NOW)
    assert Decimal(p['usd_range_from_fx']['low'])<Decimal(p['usd_amount'])<Decimal(p['usd_range_from_fx']['high'])
    book.close()

def test_pre_cap_duplicate_and_known_filters_regression():
    cards=[car(str(i),price='50000') for i in range(9)]+[car('good')]
    # The old first-N algorithm never reaches this eligible card.
    assert all(c['id']!='good' for c in cards[:9])
    result=select_candidates(cards,[{'price_max':10000}],None,EPOCH,limit=1)
    assert [c['id'] for c in result['selected']]==['good']
    assert result['reviews'][0]['reasons']==['filter_price_max']
    result=select_candidates([car('old'),car('old'),car('good')],[{}],None,EPOCH,already_seen=[('olx','old')],limit=1)
    assert [c['id'] for c in result['selected']]==['good']

def test_optional_fields_not_filter_denial():
    c=car(fuel=None,transmission=None,photos=[])
    r=filter_reasons(c,{'fuel':'diesel','transmission':'manual'},None,EPOCH)
    assert r['match'] and len(r['unknown'])==2
    assert 'missing_fuel' in estimate(c,peers(),None,EPOCH)['reasons']

@pytest.mark.parametrize('change,reason',[(dict(generation='A4'),'mismatch_generation'),(dict(body='sedan'),'mismatch_body'),(dict(mileage_km=400000),'mismatch_mileage'),(dict(research_condition='not_running'),'mismatch_condition')])
def test_incompatible_analogs(change,reason):
    bad=car('bad',**change);r=estimate(car(),peers()+[bad],None,EPOCH)
    assert reason in r['exclusions'] and all(c['id']!='bad' for c in r['used_comparables'])

def test_three_methods_math_threshold_and_no_ria_margin():
    r=estimate(car(),peers(),None,EPOCH)
    assert r['status']=='experimental_asking_estimate' and r['sample']==8
    assert r['methods']['median']['reference_usd']=='8075.0'
    assert r['methods']['trimmed_mean']['reference_usd']=='8066.6666666666666666666666666666666666666666666667'
    assert Decimal(r['methods']['median']['discount_percent'])>25
    assert r['extra_ria_margin_percent']=='0' and r['recommended'] is None

def test_missing_small_sample_and_no_invented_accuracy():
    r=estimate(car(),peers()[:2],None,EPOCH);assert r['reference_usd'] is None
    report=assess_dataset([car()]+peers(),None,EPOCH)
    assert report['confusion'] is None and report['independently_labeled_cases']==0

def test_forbidden_price_risks_and_self_duplicate():
    t=car(vehicle_key='known-target');duplicate=car('cross',vehicle_key='known-target')
    bad=car('parts',eligibility_review={'status':'excluded'})
    deposit=car('deposit',price_review={'reasons':['price_context_deposit']})
    r=estimate(t,peers()+[t,duplicate,bad,deposit],None,EPOCH)
    assert r['sample']==8 and r['exclusions']['self_or_known_crosspost']==2
    assert 'price_context_deposit' in r['exclusions']

def test_same_time_duplicate_conflicts():
    rows=peers();rows.append(car('0','999'))
    r=estimate(car(),rows,None,EPOCH)
    assert r['status']=='profitability_unconfirmed' and r['sample']==7

def mixed_sale_html(c,values=None,visible='Можливий обмін, Звичайний продаж'):
    values=['possible_exchange','regular_sale'] if values is None else values
    state={'ad':{'ad':{'id':c['id'],'url':c['url'],'params':[{'key':'sale_terms','value':visible,'normalizedValue':values}]}}}
    return ('<html><p>Умови продажу: '+visible+'</p><script id="olx-init-config">window.__PRERENDERED_STATE__='+json.dumps(state)+';</script></html>').encode()

def test_regular_sale_with_optional_exchange_regression():
    c=car();c['observed_asking_display'].update(status='needs_review',reasons=['ordinary_sale_not_corroborated','visible_sale_terms_missing_or_conflicting'])
    parsed={'listing':c,'summary':{'download_truncated':False}}
    assert asking_price_reasons(c)
    out=enrich(mixed_sale_html(c),parsed)
    assert asking_price_reasons(out['listing'])==[]
    assert parsed['listing']['observed_asking_display']['status']=='needs_review'

@pytest.mark.parametrize('values,visible',[
    (['possible_exchange'],'Можливий обмін'),
    (['regular_sale','credit'],'Звичайний продаж, Кредит'),
    (['regular_sale','possible_exchange'],'Звичайний продаж')])
def test_ambiguous_sale_is_not_promoted(values,visible):
    c=car();c['observed_asking_display'].update(status='needs_review',reasons=['ordinary_sale_not_corroborated','visible_sale_terms_missing_or_conflicting'])
    r=enrich(mixed_sale_html(c,values,visible),{'listing':c,'summary':{'download_truncated':False}})
    assert asking_price_reasons(r['listing'])

def test_current_crosspost_exclusion_cannot_launder_older_record():
    older=car('older',vehicle_key='same',checked_at=EPOCH-5)
    current=car('latest',vehicle_key='same',eligibility_review={'status':'excluded'})
    r=estimate(car(),peers()+[older,current],None,EPOCH)
    assert r['sample']==8
    assert r['exclusions']['known_vehicle_current_evidence_invalid']==2

def users():
    return [dict(id=x,confirmed_at=EPOCH-100,expires_at=EPOCH+1000,ready=True,search_enabled=True,stopped=False,filters={},min_discount=15) for x in ('paid-1','paid-2')]+[dict(id='unpaid',ready=True,search_enabled=True,filters={})]

def test_shared_detail_cache_partial_cursor_restart_paid_only(tmp_path):
    from .flow import ReplayStore
    path=tmp_path/'flow.db';store=ReplayStore(path);calls=[]
    c=car('source');c['publication_verified']=False
    def fetch(url):calls.append(url);return 200,json.dumps(c).encode()
    r=store.collect('bounded',[c,c],users(),None,EPOCH,fetch,page_complete=False,checkpoint=12,parser=json.loads)
    assert len(calls)==1 and r['run']['page_checkpoint'] is None
    report=store.prepare(users(),None,EPOCH)
    assert len(report['prepared'])==2 and report['denied_users']==['unpaid']
    assert report['actual_telegram_calls']==0 and not report['prepared'][0]['new_publication_verified']
    store.close();store=ReplayStore(path)
    assert store.prepare(users(),None,EPOCH)['prepared']==[]
    store.collect('bounded',[c],users(),None,EPOCH,fetch,parser=json.loads)
    assert len(calls)==1;store.close()

@pytest.mark.parametrize('status',[404,410,429,403,401])
def test_individual_failure_vs_source_stop_and_no_cursor_loss(tmp_path,status):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'flow.db');calls=[];a,b=car('a'),car('b')
    def fetch(url):
        calls.append(url)
        return (status,b'gone') if len(calls)==1 else (200,json.dumps(b).encode())
    r=store.collect('run',[a,b],users(),None,EPOCH,fetch,page_complete=True,checkpoint=9,parser=json.loads)
    assert r['run']['page_checkpoint'] is None
    assert len(calls)==(2 if status in (404,410) else 1)
    assert len(store.cars())==(1 if status in (404,410) else 0)
    r=store.collect('run',[a,b],users(),None,EPOCH,fetch,page_complete=True,checkpoint=9,parser=json.loads)
    assert r['run']['page_checkpoint'] is None # restart cannot erase failed detail
    store.close()

def test_no_unpaid_source_calls_and_access_recheck(tmp_path):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'flow.db');bad=users()[-1:]
    r=store.collect('run',[car()],bad,None,EPOCH,lambda u:pytest.fail('unpaid source call'))
    assert r['run']['calls']==0
    store.ingest(car());calls=[]
    def policy(uid):calls.append(uid);return calls.count(uid)==1
    report=store.prepare(users(),None,EPOCH,current_access=policy)
    assert not report['prepared'] and all(r['reasons']==['access_changed'] for r in report['held'])
    store.close()

def test_full_fake_flow_usd_uah_fx_failures_and_market(tmp_path):
    from .flow import ReplayStore
    quote=parse_quote('privat_nbu',body(),NOW)
    uah=car('uah','268999.8',currency='UAH') # exactly 6000 USD at this rate
    for q,expected in ((quote,6000),(None,None)):
        store=ReplayStore(tmp_path/('full'+str(expected)+'.db'))
        for c in peers()+[uah]:store.ingest(c)
        report=store.prepare(users(),q,EPOCH)
        target=[c for c in report['prepared'] if c['source_id']=='uah']
        if expected:
            assert len(target)==2 and Decimal(target[0]['price']['usd_amount'])==expected
            assert target[0]['threshold_passed_in_experiment']
        else:
            assert not target and any('fx_missing' in h['reasons'] for h in report['held'])
            assert any(c['source_id']=='0' for c in report['prepared']) # independent USD work continues
        assert report['actual_telegram_calls']==0
        store.close()

def test_collection_cap_does_not_advance_checkpoint(tmp_path):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'cap.db');a,b=car('a'),car('b')
    r=store.collect('run',[a,b],users(),None,EPOCH,lambda u:(200,json.dumps(a).encode()),parser=json.loads,page_complete=True,checkpoint=9,max_calls=1)
    assert r['run']['calls']==1 and r['run']['page_checkpoint'] is None
    store.close()

def test_equal_time_crosspost_conflicting_specs_are_held():
    rows=peers()+[car('cross1',vehicle_key='known'),car('cross2',vehicle_key='known',transmission='automatic')]
    r=estimate(car(),rows,None,EPOCH)
    assert r['sample']==8 and r['exclusions']['known_vehicle_current_evidence_invalid']==2

@pytest.mark.parametrize('field,value',[('confirmed_at',None),('expires_at',EPOCH),('ready',False),('stopped',True),('search_enabled',False)])
def test_each_access_guard_blocks_previews(tmp_path,field,value):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'access.db');store.ingest(car());u=users()[0];u[field]=value
    assert store.prepare([u],None,EPOCH)['prepared']==[];store.close()

def test_real_network_is_fenced():
    import socket
    with pytest.raises(AssertionError):socket.create_connection(('example.com',443))

def test_ria_plan_never_sends_olx_id_or_invents_dictionary():
    from .ria_plan import prepare,FIELDS,DOC
    c=car();r=prepare(c,{},EPOCH)
    assert r['body'] is None and r['actual_provider_calls']==0 and not r['execution_authorized']
    fixture={f:{('whole_passenger_car' if f=='category' else c[f]):{'id':str(i+1),'source':DOC,'checked_at':EPOCH,'reviewed':True}} for i,f in enumerate(FIELDS)}
    r=prepare(c,fixture,EPOCH)
    assert 'omniId' not in r['body']['params']
    assert r['body']['params']['mileage']=={'gte':'270','lte':'330'}
    assert r['body']['params']['engineVolume']['gte']=='1.9'
    assert r['price_per_call'] is None and not r['execution_authorized']
    fixture['brand']['Skoda']['source']='https://unverified.invalid/'
    assert prepare(c,fixture,EPOCH)['body'] is None

@pytest.mark.parametrize('url',['https://example.com/a.html','https://user:pass@www.olx.ua/d/uk/obyavlenie/a.html','https://www.olx.ua/api/partner/adverts','http://www.olx.ua/d/uk/obyavlenie/a.html'])
def test_only_observed_public_detail_hosts_reach_transport(tmp_path,url):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'url.db')
    r=store.collect('run',[car(url=url)],users(),None,EPOCH,lambda u:pytest.fail('invalid URL I/O'))
    assert r['run']['calls']==0 and r['receipts'][0]['status']=='invalid_detail_url'
    store.close()

@pytest.mark.parametrize('bad_url',['https://www.olx.ua:invalid/d/uk/obyavlenie/a.html','https://[invalid/d/uk/obyavlenie/a.html'])
def test_malformed_url_does_not_discard_later_valid_detail(tmp_path,bad_url):
    from .flow import ReplayStore
    store=ReplayStore(tmp_path/'malformed-url.db');good=car('good');calls=[]
    def fetch(url):
        calls.append(url)
        return 200,json.dumps(good).encode()
    result=store.collect('run',[car('bad',url=bad_url),good],users(),None,EPOCH,fetch,parser=json.loads)
    assert len(calls)==1 and store.cars()[0]['id']=='good'
    assert result['receipts'][0]['status']=='invalid_detail_url'
    store.close()
