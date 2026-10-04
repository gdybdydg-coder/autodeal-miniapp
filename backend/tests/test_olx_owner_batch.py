"""Current policy + real parser + temporary DB; no external transport."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json

import pytest
from sqlalchemy import select, event as sql_event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import olx_owner_batch as batch
from backend.app import Settings
from backend.models import SourceProbe, Search, User
from backend.manual_payment_models import PaymentRequest
from backend.tests.test_olx_isolated_integration import bench, client
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.fx import FXQuote, instant, nbu_url
from experiments.olx_offline.test_observed_price_provenance import page, AD


def car_page(identity='123', *, currency='USD', description='Розмитнений. Продаю автомобіль цілим.', **kwargs):
    url = 'https://www.olx.ua/d/uk/obyavlenie/example-ID' + identity + '.html'
    ad = deepcopy(AD)
    symbol = '$' if currency == 'USD' else 'грн.'
    ad.update(id=int(identity), url=url)
    ad['price'].update(displayValue='2 550 '+symbol, regularPrice={'value':2550,'currencyCode':currency})
    return page(ad=ad, visible='2 550 '+symbol, description=description,
                offer_amount=2550, offer_currency=currency,
                vehicle_extra={'sku':identity,'url':url}, **kwargs)


@pytest.fixture
def live(bench, monkeypatch):
    client(bench, 900)
    client(bench, 100)
    monkeypatch.setenv('SUBSCRIPTION_EXPECTED_ADMIN_ID', '900')
    monkeypatch.setattr(batch.time, 'time', lambda: bench.clock[0])
    bench.settings = Settings(str(bench.engine.url), 'test-token', 'test-secret-'*4,
        delivery_enabled=True, source_ready=True, admin_telegram_id=900,
        olx_owner_batch_enabled=True, olx_owner_batch_until=bench.clock[0]+7200)
    bench.calls, bench.telegram = [], []
    bench.pages = {str(n):car_page(str(n)) for n in range(123,127)}
    def fetch(url):
        bench.calls.append(url)
        if '/transport/' in url:
            cards = ''.join('<div data-testid="l-card" id="'+key+'"><a data-testid="card-title-link" href="https://www.olx.ua/d/uk/obyavlenie/example-ID'+key+'.html">Mazda</a><p data-testid="ad-price">2 550 $</p></div>' for key in bench.pages)
            return 200, ('<html>'+cards+'</html>').encode(), False
        key = url.split('-ID')[1].split('.')[0]
        return 200, bench.pages[key], False
    def sender(token, method, payload, **kwargs):
        bench.telegram.append((method, payload))
        chat = {'id':900,'type':'private'}
        return {'ok':True, 'result':chat if method=='getChat' else {'message_id':550+len(bench.telegram),'chat':chat}}
    bench.fetch, bench.sender = fetch, sender
    return bench


def run(b):
    return batch.tick(b.engine, b.settings, b.fetch, b.sender)


def ads(b):
    return [(m,p) for m,p in b.telegram if m in ('sendMessage','sendPhoto')]


def test_three_real_parser_samples_only_owner_and_restart_no_replay(live):
    run(live)
    assert len(ads(live)) == 3
    assert {p['chat_id'] for _,p in live.telegram} == {900}
    assert len(live.calls) == 4
    for _,p in ads(live):
        text = p.get('text',p.get('caption'))
        assert 'ТЕСТ OLX — лише для власника' in text
        assert 'Ринкова оцінка: ще не підтверджена.' in text
        assert '📊 Ринкова ціна' not in text and 'Вигода:' not in text
        assert '2 550 $' in text and p['reply_markup']['inline_keyboard'][0][0]['url'].startswith('https://www.olx.ua/')
    with Session(live.engine) as db:
        state = db.get(SourceProbe,batch.STATE)
        assert state.status == 'finished' and state.result['accepted'] == 3
        assert len(state.result['receipts']) == 3
        assert all(x['message_id'] for x in state.result['receipts'])
        assert state.result['search_ids'] == [900]
    run(live)
    assert len(ads(live)) == 3 and len(live.calls) == 4


@pytest.mark.parametrize('why',['disabled','expired','unverified_owner','unpaid','review','expired_purchase','stop','search_off','other_recipient'])
def test_no_get_or_send_without_verified_current_owner(live,monkeypatch,why):
    if why=='disabled':live.settings=replace(live.settings,olx_owner_batch_enabled=False)
    elif why=='expired':live.settings=replace(live.settings,olx_owner_batch_until=live.clock[0])
    elif why=='unverified_owner':monkeypatch.setenv('SUBSCRIPTION_EXPECTED_ADMIN_ID','901')
    elif why=='other_recipient':live.settings=replace(live.settings,admin_telegram_id=100)
    else:
        with Session(live.engine) as db:
            p=db.get(PaymentRequest,'test-900')
            if why=='unpaid':db.delete(p)
            elif why=='review':p.state='review'
            elif why=='expired_purchase':p.expires_at=live.clock[0]
            elif why=='stop':db.get(User,900).ready=False
            elif why=='search_off':db.get(Search,900).enabled=False
            db.commit()
    run(live)
    assert not live.calls and not live.telegram


@pytest.mark.parametrize('reason',['Нерозмитнений автомобіль.','Продаю під розбір.','Донор, продається частинами.',
                                  'Автомобіль у розборі.','Автомобиль в разборе.', 'Ціна за двигун.', 'Перший внесок, решта в кредит.'])
def test_forbidden_categories_and_deposits_never_sent(live,reason):
    live.pages={'123':car_page(description=reason)}
    run(live)
    assert len(live.calls)==2 and not live.telegram


@pytest.mark.parametrize('description',['Розмитнений, не на розбір, продається цілим.',
                                      'Растаможен, не на разбор, продается целиком.',
                                      'Не на ходу, потрібен ремонт, продається цілим.'])
def test_negation_and_repair_are_not_parts_and_missing_fuel_gear_photo_allowed(live,description):
    live.pages={'123':car_page(description=description)}
    run(live)
    assert len(ads(live))==1 and ads(live)[0][0]=='sendMessage'


def test_owner_usd_budget_known_contradiction_blocks(live):
    with Session(live.engine) as db:
        row=db.get(Search,900);row.filters={**row.filters,'price':{'from':None,'to':2000}};db.commit()
    assert batch.searches(live.engine,live.settings)
    run(live)
    assert len(live.calls)>1 and not live.telegram


def test_uah_without_current_nbu_rate_never_invents_conversion(live):
    live.pages={'123':car_page(currency='UAH')}
    run(live)
    assert not live.telegram
    car=parse_detail_snapshot(live.pages['123'],fetched_at=int(live.clock[0]),truncated=False)['listing']
    today=instant(live.clock[0]).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Kyiv')).date()
    q=FXQuote(Decimal('40'),today,instant(live.clock[0]),nbu_url(today))
    proof=batch.price_proof(car,live.clock[0],q)
    assert proof['usd']['usd_amount']=='63.75'
    assert proof['usd']['fx']['effective_date']==today.isoformat()
    assert proof['seller_original_currency_verified'] is False
    assert '≈' in batch.caption(car,proof)


@pytest.mark.parametrize('step',['source','getChat'])
def test_stop_rechecked_before_each_external_call(live,step):
    def stop():
        with Session(live.engine) as db:db.get(User,900).ready=False;db.commit()
    if step=='source':
        old=live.fetch
        def fetch(url):
            result=old(url);stop();return result
        live.fetch=fetch
    else:
        old=live.sender
        def sender(token,method,payload,**kw):
            result=old(token,method,payload,**kw);stop();return result
        live.sender=sender
    run(live)
    assert not ads(live)
    assert len(live.calls)==1 if step=='source' else any(m=='getChat' for m,_ in live.telegram)


@pytest.mark.parametrize('chat',[{'id':100,'type':'private'},{'id':900,'type':'group'},{'id':True,'type':'private'}])
def test_wrong_chat_proof_never_sends(live,chat):
    def sender(token,method,payload,**kw):
        live.telegram.append((method,payload))
        return {'ok':True,'result':chat}
    live.sender=sender
    run(live)
    assert not ads(live)


def test_old_batch_and_global_ledger_keep_cross_package_dedup(live):
    with Session(live.engine) as db:
        db.add(SourceProbe(id=batch.previous.TEST_BATCH+'-old',status='accepted',checked_at=live.clock[0],requests=1,result={'source_id':'123','message_id':12}))
        db.commit()
    run(live)
    assert len(ads(live))==3
    assert all('ID123.html' not in p['reply_markup']['inline_keyboard'][0][0]['url'] for _,p in ads(live))
    with Session(live.engine) as db:
        assert db.get(SourceProbe,batch.previous.TEST_BATCH+'-old').result['message_id']==12


def test_unknown_photo_result_never_falls_back_or_retries(live):
    live.pages={'123':car_page()}
    old=batch.parse_detail_snapshot
    def parse(*a,**kw):
        data=old(*a,**kw);data['listing']['photos']=['https://a.olxcdn.com/image'];return data
    def sender(token,method,payload,**kw):
        live.telegram.append((method,payload))
        if method=='getChat':return {'ok':True,'result':{'id':900,'type':'private'}}
        raise TimeoutError('synthetic ambiguous transport')
    from unittest.mock import patch
    with patch.object(batch,'parse_detail_snapshot',parse):
        live.sender=sender;run(live);run(live)
    assert [m for m,_ in ads(live)]==['sendPhoto']
    with Session(live.engine) as db:
        assert db.get(SourceProbe,batch.STATE).result['receipts'][0]['status']=='uncertain'


def test_403_terminal_and_source_failure_independent_of_ria(live):
    live.fetch=lambda url:(403,b'',False)
    assert run(live)=='source_http_403'
    assert not live.telegram
    with Session(live.engine) as db:
        assert db.get(Search,100).enabled and db.get(Search,900).enabled
        assert db.get(User,900).ready
        assert db.get(PaymentRequest,'test-900').state=='approved'
    assert run(live)=='paused_finished_or_busy'


def test_410_skips_only_that_listing(live):
    old=live.fetch
    def fetch(url):return (410,b'',False) if 'ID123.html' in url else old(url)
    live.fetch=fetch;run(live)
    assert len(ads(live))==3


def test_command_stop_before_initialization_and_other_client_denied(live):
    def event(uid):return {'message':{'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':'/olx_stop'}}
    assert batch.handle(live.engine,live.settings,event(100))=={'ok':True}
    batch.handle(live.engine,live.settings,event(900));run(live)
    assert not live.calls and not live.telegram


def test_database_error_fails_closed(live,monkeypatch):
    monkeypatch.setattr(batch.paid_source_access,'allowed',lambda *a: (_ for _ in ()).throw(OperationalError('fixture',{},Exception())))
    with pytest.raises(OperationalError):run(live)
    assert not live.calls and not live.telegram


def test_only_probe_ledger_writes_no_payment_search_or_budget_mutation(live):
    writes=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        if statement.upper().startswith(('UPDATE','INSERT','DELETE')):writes.append(statement.lower())
    sql_event.listen(live.engine,'before_cursor_execute',capture)
    try:run(live)
    finally:sql_event.remove(live.engine,'before_cursor_execute',capture)
    assert writes and all('source_probes' in s for s in writes)


def test_lease_and_persistent_budget_no_parallel_claim_or_reset(live):
    assert batch.initialize(live.engine,live.settings)
    token,state=batch.claim(live.engine)
    assert batch.claim(live.engine) is None
    for _ in range(batch.MAX_GETS):assert batch.reserve_get(live.engine,live.settings,token,state)
    assert not batch.reserve_get(live.engine,live.settings,token,state)
    assert batch.initialize(live.engine,live.settings)
    with Session(live.engine) as db:
        assert db.get(SourceProbe,batch.STATE).requests==batch.MAX_GETS
        assert db.get(SourceProbe,batch.STATE).result['reserved_bytes']==batch.MAX_BYTES


def test_deadline_is_pinned_and_not_extended_by_restart(live):
    batch.initialize(live.engine,live.settings)
    live.clock[0]+=batch.MAX_SECONDS
    run(live)
    assert not live.calls and not live.telegram
    with Session(live.engine) as db:assert db.get(SourceProbe,batch.STATE).status=='finished'


def test_overlapping_processes_share_one_durable_claim(live):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    old=live.fetch
    def fetch(url):
        if '/transport/' in url:
            entered.set();assert release.wait(5)
        return old(url)
    live.fetch=fetch
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(run,live)
        assert entered.wait(5)
        try:assert pool.submit(run,live).result(timeout=5)=='paused_finished_or_busy'
        finally:release.set()
        first.result(timeout=5)
    assert len(ads(live))==3 and len(live.calls)==4


@pytest.mark.parametrize('error,status',[(400,'rejected'),(429,'rejected'),(500,'uncertain')])
def test_telegram_error_classification_no_retry_or_fallback(live,error,status):
    live.pages={'123':car_page()}
    old=live.sender
    def sender(token,method,payload,**kw):
        response=old(token,method,payload,**kw)
        return response if method=='getChat' else {'ok':False,'error_code':error}
    live.sender=sender;run(live);run(live)
    assert len(ads(live))==1
    with Session(live.engine) as db:
        assert db.get(SourceProbe,batch.STATE).result['receipts'][0]['status']==status


def test_usd_display_is_not_converted_using_alternate_uah_price(live):
    ad=deepcopy(AD)
    ad['price'].update(displayValue='2 550 $',regularPrice={'value':115000,'currencyCode':'UAH'})
    raw=page(ad=ad,visible='2 550 $',offer_amount=115000,offer_currency='UAH')
    car=parse_detail_snapshot(raw,fetched_at=int(live.clock[0]),truncated=False)['listing']
    proof=batch.price_proof(car,live.clock[0])
    assert proof['usd']['usd_amount']=='2550' and proof['usd']['fx'] is None
    assert proof['source_regular_price']=={'amount':'115000','currency':'UAH'}
    assert proof['seller_original_currency'] is None
