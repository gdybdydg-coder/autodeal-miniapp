"""Whole-app integration with real Telegram signatures and offline providers."""
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import Settings, create_app
from backend.billing_models import Entitlement
from backend.models import Search, User
from backend.radar_catalog import import_catalog
from backend.radar_models import Radar
from backend.tests.test_backend import TOKEN, SECRET, headers, command


@pytest.fixture
def application(tmp_path):
    url='sqlite:///'+str(tmp_path/'radar-app.sqlite3')
    engine=create_engine(url,connect_args={'check_same_thread':False})
    settings=Settings(url,TOKEN,SECRET,radar_enabled=True)
    with TestClient(create_app(settings,engine,paid_source_only=True)) as client:
        with Session(engine) as db,db.begin():
            import_catalog(db,{'brands':[{'id':9,'name':'BMW','models_available':True,
                'models':[{'id':3219,'name':'3 Series'}]}],
                'regions':[{'id':10,'name':'Київська'}]})
        yield engine,client,settings
    engine.dispose()


def body():
    return {'name':'Мій радар','brands':[{'brand_id':9,'model_ids':[3219]}],
            'price_category':3,'region_ids':[10],'year_from':2000,'year_to':2026,'min_profit':10}


def test_valid_non_whitelisted_user_gets_three_empty_slots_without_subscription(application):
    _,client,_=application
    result=client.get('/api/radars',headers=headers(456789))
    assert result.status_code==200,result.text
    assert result.json()=={'radars':[],'limit':3,'access':{'active':False,'expires_at':None}}
    created=client.post('/api/radars',headers=headers(456789),json=body())
    assert created.status_code==201,created.text
    assert created.json()['status']=='paused'
    assert client.get('/api/billing/status',headers=headers(456789)).json()['access_available'] is False


def test_unsigned_tampered_and_cross_user_radar_access_are_rejected(application):
    _,client,_=application
    assert client.get('/api/radars').status_code==401
    assert client.get('/api/radars',headers={'X-Telegram-Init-Data':'user_id=777292211'}).status_code==401
    saved=client.post('/api/radars',headers=headers(111),json=body()).json()
    assert client.put('/api/radars/'+saved['id'],headers=headers(222),
        json={**body(),'revision':saved['revision']}).status_code==404
    assert client.patch('/api/radars/'+saved['id']+'/status',headers=headers(222),
        json={'active':True,'revision':saved['revision']}).status_code==404
    assert client.delete('/api/radars/'+saved['id']+'?revision=1',headers=headers(222)).status_code==404
    assert client.get('/api/radars',headers=headers(222)).json()['radars']==[]


def test_legacy_browser_tab_cannot_start_old_search_or_spend_manual_query(application):
    _,client,_=application
    assert client.post('/api/subscriptions',headers=headers(),json={'name':'Старий','filters':{},'enabled':True}).status_code==410
    assert client.get('/api/subscriptions',headers=headers()).status_code==410
    assert client.get('/api/catalog?brand=BMW',headers=headers()).status_code==410
    assert client.post('/api/cars/search',headers=headers(),json={}).status_code==410
    assert client.get('/api/radars/catalog',headers=headers()).status_code==200


def test_webhook_stop_pauses_radars_start_does_not_resume(application):
    engine,client,_=application
    assert command(client,'/start',uid=111).status_code==200
    with Session(engine) as db,db.begin():
        db.add(Entitlement(user_id=111,expires_at=time.time()+3600,updated_at=time.time()))
    created=client.post('/api/radars',headers=headers(),json=body()).json()
    started=client.patch('/api/radars/'+created['id']+'/status',headers=headers(),
        json={'active':True,'revision':created['revision']})
    assert started.status_code==200,started.text
    assert started.json()['status']=='active'
    assert command(client,'/stop',update=2,uid=111).status_code==200
    assert client.get('/api/radars',headers=headers()).json()['radars'][0]['status']=='paused'
    assert command(client,'/start',update=3,uid=111).status_code==200
    assert client.get('/api/radars',headers=headers()).json()['radars'][0]['status']=='paused'
    with Session(engine) as db:
        assert not any(db.scalars(select(Search.enabled).where(Search.user_id==111)))
        assert db.get(User,111).ready is True


def test_renewal_after_expiry_requires_manual_resume(application):
    engine,client,_=application
    command(client,'/start')
    with Session(engine) as db,db.begin():
        db.add(Entitlement(user_id=111,expires_at=time.time()+3600,updated_at=time.time()))
    created=client.post('/api/radars',headers=headers(),json=body()).json()
    client.patch('/api/radars/'+created['id']+'/status',headers=headers(),json={'active':True,'revision':1})
    with Session(engine) as db,db.begin():
        db.get(Radar,created['id']).active_until=time.time()-30
        db.get(Entitlement,111).expires_at=time.time()+7200
    saved=client.get('/api/radars',headers=headers()).json()['radars'][0]
    assert saved['status']=='subscription_expired'
    edited=client.put('/api/radars/'+saved['id'],headers=headers(),json={**body(),'name':'Змінений','revision':saved['revision']})
    assert edited.status_code==200,edited.text
    assert edited.json()['status']=='subscription_expired'
    resumed=client.patch('/api/radars/'+saved['id']+'/status',headers=headers(),
        json={'active':True,'revision':edited.json()['revision']})
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['status']=='active'


def test_radar_api_errors_are_ukrainian_and_do_not_echo_identity(application):
    _,client,_=application
    result=client.post('/api/radars',headers=headers(),json={**body(),'year_from':2026,'year_to':2020})
    assert result.status_code==422
    assert result.json()['detail']=='Некоректні дані. Перевірте заповнені поля.'
    assert TOKEN not in result.text


def test_radar_schema_marker_prevents_legacy_revival_when_env_disabled(application):
    engine,_,settings=application
    # A restart with an old switch cannot restore the replaced UI/pipeline.
    with TestClient(create_app(replace(settings,radar_enabled=False),engine,paid_source_only=True)) as restarted:
        assert restarted.get('/api/subscriptions',headers=headers()).status_code==410
        assert restarted.get('/api/radars',headers=headers()).status_code==200


def test_admin_statistics_count_one_radar_with_multiple_model_branches(application):
    from backend import bot_commands
    from backend.manual_payment_models import ManualBase
    engine,client,settings=application
    ManualBase.metadata.create_all(engine)
    command(client,'/start')
    with Session(engine) as db,db.begin():
        db.add(Entitlement(user_id=111,expires_at=time.time()+3600,updated_at=time.time()))
    saved=client.post('/api/radars',headers=headers(),json={**body(),
        'brands':[{'brand_id':9,'model_ids':[3219,2319]}]}).json()
    assert client.patch('/api/radars/'+saved['id']+'/status',headers=headers(),
        json={'active':True,'revision':saved['revision']}).status_code==200
    with Session(engine) as db:
        assert len(list(db.scalars(select(Search).where(Search.user_id==111,Search.enabled.is_(True)))))==2
        text=bot_commands.stats_text(db,999999,999999,settings=settings)
        assert 'Активних радарів: 1' in text
        assert 'Користувачів з активним радаром: 1' in text
        db.get(Radar,saved['id']).active_until=time.time()-1
        db.commit()
        assert 'Активних радарів: 0' in bot_commands.stats_text(db,999999,999999,settings=settings)


def test_radar_command_menu_has_own_idempotent_configuration_marker(application,monkeypatch):
    from backend import bot_commands
    from backend.models import SourceProbe
    engine,_,settings=application
    monkeypatch.setattr(bot_commands.telegram_setup,'webhook_status',lambda _: {'status':'configured'})
    configured=replace(settings,configure_webhook=True)
    with Session(engine) as db,db.begin():
        db.add(SourceProbe(id='telegram-commands-v1',status='configured',checked_at=time.time(),requests=0,result={}))
    calls=[]
    def request(token,method,payload):
        calls.append((method,payload))
        return {'ok':True,'result':True}
    bot_commands.configure(engine,configured,request)
    bot_commands.configure(engine,configured,request)
    assert len(calls)==1 and calls[0][0]=='setMyCommands'
    assert {'command':'help','description':'Як налаштувати радари'} in calls[0][1]['commands']


@pytest.mark.parametrize('enabled_flag',[False,True])
def test_radar_cutover_cannot_start_legacy_experimental_source_tasks(application,monkeypatch,enabled_flag):
    from backend import (app as app_module, html_shadow, olx_owner_monitor, olx_ria_probe,
                         paid_source_access, ria_source_comparison)
    engine,_,settings=application
    captured=[]
    async def observe_primary(engine,settings,stop):
        captured.append(settings)
    async def forbidden_task(*args,**kwargs):
        pytest.fail('Legacy source task started in Radar mode')
    def forbidden_probe(*args,**kwargs):
        pytest.fail('Diagnostic source probe started in Radar mode')
    monkeypatch.setattr(app_module.monitor,'run',observe_primary)
    monkeypatch.setattr(olx_owner_monitor,'enabled',lambda:True)
    monkeypatch.setattr(olx_ria_probe,'enabled',lambda:True)
    monkeypatch.setattr(olx_owner_monitor,'run',forbidden_task)
    monkeypatch.setattr(olx_ria_probe,'run',forbidden_task)
    monkeypatch.setattr(html_shadow,'run',forbidden_task)
    monkeypatch.setattr(ria_source_comparison,'check_once',forbidden_probe)
    monkeypatch.setattr(app_module,'probe_once',forbidden_probe)
    monkeypatch.setattr(app_module,'verify_search_once',forbidden_probe)
    noisy=replace(settings,radar_enabled=enabled_flag,monitor_enabled=True,
        ria_html_shadow_run_id='stale',ria_source_comparison_listing_id='12345678',
        ria_photo_repair_ids='12345678',ria_recovery_listing_id='12345678',
        ria_failed_delivery_recovery_id='12345678',ria_diagnostic_listing_id='12345678',
        full_scan_enabled=True,ria_active_window_enabled=True,ria_recent_publications_enabled=True)
    with TestClient(create_app(noisy,engine)) as restarted:
        assert restarted.get('/api/radars',headers=headers()).status_code==200
    assert len(captured)==1
    effective=captured[0]
    assert effective.radar_enabled and effective.ria_confirmed_deals_only and paid_source_access.strict(engine)
    assert not any((effective.ria_html_shadow_run_id,effective.ria_source_comparison_listing_id,
        effective.ria_photo_repair_ids,effective.ria_recovery_listing_id,effective.ria_failed_delivery_recovery_id,
        effective.ria_diagnostic_listing_id,effective.full_scan_enabled,effective.ria_recent_publications_enabled,
        effective.ria_active_window_enabled))


def test_live_radar_requires_confirmed_provider_pricing_before_database_cutover(tmp_path):
    from sqlalchemy import inspect
    url='sqlite:///'+str(tmp_path/'misconfigured.sqlite3')
    engine=create_engine(url)
    settings=Settings(url,TOKEN,SECRET,radar_enabled=True,monitor_enabled=True,
        source_ready=True,delivery_enabled=True,ria_ai_price_enabled=False)
    with pytest.raises(ValueError,match='requires RIA_AI_PRICE_ENABLED'):
        create_app(settings,engine)
    assert not inspect(engine).has_table('radars')
    engine.dispose()
