"""Isolated PostgreSQL acceptance checks; never connects to production/providers.

Usage: python tools/radar_postgres_check.py --cluster-root ../.local-postgres
Creates a new uniquely named database on 127.0.0.1:55439, retaining it for review.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from backend.app import Settings, create_app
from backend.models import Base, Search, User, MonitorMatch, MonitorSeen, MonitorMembership, MonitorWatch
from backend.billing_models import Entitlement, BillingControl
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.radar_models import (RADAR_TABLES, BrandCatalog, ModelCatalog, RegionCatalog,
    Radar, RadarBrand, RadarModel, RadarRegion, RadarSearch, RadarMigration)
from backend.radar_catalog import import_catalog
from backend.radar_migration import migrate
from backend import radar_service as service
from backend import radar_monitor

args=argparse.ArgumentParser()
args.add_argument('--cluster-root',required=True)
options=args.parse_args()
cluster=Path(options.cluster_root).resolve()
password=(cluster/'password.txt').read_text().strip()
url=URL.create('postgresql+psycopg',username='pilot',password=password,host='127.0.0.1',port=55439,database='postgres')
name='autodeal_radars_'+uuid.uuid4().hex[:12]
admin=create_engine(url,isolation_level='AUTOCOMMIT',poolclass=NullPool)
with admin.connect() as db:
    db.execute(text('CREATE DATABASE '+name))
admin.dispose()
engine=create_engine(url.set(database=name),pool_size=10,max_overflow=2,
    connect_args={'options':'-c statement_timeout=20000 -c lock_timeout=15000 -c jit=off'})
report={'database':name,'host':'127.0.0.1','provider_requests':0,'telegram_sends':0,'checks':[]}
def passed(label):
    report['checks'].append(label)
    print(label+' PASS',flush=True)
def payload(**changes):
    return service.RadarCreate(**{'name':'Тестовий радар','brands':[{'brand_id':9,'model_ids':[3219]}],
        'price_category':3,'region_ids':[10],'year_from':2000,'year_to':2026,'min_profit':10,**changes})
try:
    Base.metadata.create_all(engine,tables=[table for table in Base.metadata.sorted_tables if table not in RADAR_TABLES])
    ManualBase.metadata.create_all(engine)
    now=time.time()
    with Session(engine) as db,db.begin():
        db.add_all([User(id=501,ready=True),User(id=502,ready=True)])
        db.flush()
        db.add(Entitlement(user_id=501,expires_at=now+3600,updated_at=now))
        db.add(BillingControl(id='commercial-v1',version=4,enforce=True,sales=False,offer={'fixture':'preserve'}))
        db.add(Search(user_id=501,name='Старий пошук',fingerprint='legacy-fixture',filters={},enabled=True))
        db.add(PaymentRequest(id='radar-payment-test',user_id=501,state='approved',amount_minor=25000,
            currency='UAH',days=30,created_at=now-200,updated_at=now-100))
    with ThreadPoolExecutor(max_workers=2) as migration_pool:
        migration_results=list(migration_pool.map(lambda _: migrate(engine),range(2)))
    assert sorted(row['applied'] for row in migration_results)==[False,True]
    assert migrate(engine)['applied'] is False
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Radar))==0
        assert db.scalar(select(Search.enabled).where(Search.fingerprint=='legacy-fixture')) is False
        assert db.get(User,501).ready is True
        assert db.get(Entitlement,501).expires_at==now+3600
        assert db.get(BillingControl,'commercial-v1').offer=={'fixture':'preserve'}
        receipt=db.get(PaymentRequest,'radar-payment-test')
        assert (receipt.state,receipt.user_id,receipt.amount_minor,receipt.currency)==('approved',501,25000,'UAH')
    passed('migration_repeatable_empty_slots_and_identity_billing_preserved')
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BrandCatalog))==397
        assert db.scalar(select(func.count()).select_from(ModelCatalog))==5070
        assert db.scalar(select(func.count()).select_from(RegionCatalog))==25
    passed('concurrent_transactional_migration_imports_complete_official_catalog')
    with Session(engine) as db,db.begin():
        import_catalog(db,{'brands':[{'id':9,'name':'BMW','models_available':True,'models':[{'id':3219,'name':'3 Series'}]}],
                           'regions':[{'id':10,'name':'Київська'}]})
    barrier=threading.Barrier(8)
    def create_one(i):
        with Session(engine) as db:
            barrier.wait(timeout=15)
            try:
                with db.begin():
                    result=service.create(db,90001,payload(name=f'Радар {i}',client_request_id='fixture-request-'+str(i)))
                return {'created':result}
            except service.RadarError as exc:
                return {'error':exc.code}
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes=list(pool.map(create_one,range(8)))
    created=[row['created'] for row in outcomes if 'created' in row]
    assert len(created)==3,outcomes
    assert sum(row.get('error')=='radar_slots_full' for row in outcomes)==5,outcomes
    assert {row['slot'] for row in created}=={1,2,3}
    passed('eight_concurrent_first_visits_create_exactly_three_slots')
    with Session(engine) as db:
        try:
            with db.begin():
                db.add(Radar(id=uuid.uuid4().hex,user_id=90001,slot=4,name='Заборонений',price_category=3,
                    year_from=2000,year_to=2026,min_profit=10,status='paused',revision=1,created_at=now,updated_at=now))
                db.flush()
            raise AssertionError('Database accepted slot4')
        except IntegrityError:
            pass
    passed('database_check_rejects_fourth_slot_directly')
    victim=created[0]
    with Session(engine) as db,db.begin():
        service.remove(db,90001,victim['id'],victim['revision'])
        replacement=service.create(db,90001,payload(name='Новий після видалення'))
        assert replacement['slot']==victim['slot']
        for child in (RadarBrand,RadarModel,RadarRegion,RadarSearch):
            assert db.scalar(select(func.count()).select_from(child).where(child.radar_id==victim['id']))==0
    passed('delete_releases_slot_and_cascades_configuration')
    with Session(engine) as db,db.begin():
        own=service.create(db,501,payload())
        own=service.set_status(db,501,own['id'],service.RadarStatus(active=True,revision=own['revision']))
        assert own['status']=='active'
        search_ids=list(db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id==own['id'])))
        assert search_ids and all(db.get(Search,sid).enabled for sid in search_ids)
    passed('paid_activation_uses_existing_monitor_watch_pipeline')
    with Session(engine) as db,db.begin():
        try:
            service.owned(db,502,own['id'])
            raise AssertionError('Cross-owner radar exposed')
        except service.RadarError as exc:
            assert exc.status==404
        radar=db.get(Radar,own['id'])
        radar.active_until=now-1
        db.get(Entitlement,501).expires_at=now+7200
    radar_monitor.sync_expired(engine,now=now)
    with Session(engine) as db,db.begin():
        radar=db.get(Radar,own['id'])
        assert radar.status=='subscription_expired'
        assert all(not db.get(Search,sid).enabled for sid in search_ids)
        assert db.scalar(select(func.count()).select_from(RadarModel).where(RadarModel.radar_id==radar.id))==1
    passed('expiry_gap_never_auto_resumes_after_renewal')
    with Session(engine) as db,db.begin():
        radar=db.get(Radar,own['id'])
        service.set_status(db,501,radar.id,service.RadarStatus(active=True,revision=radar.revision))
        ids=list(db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id==radar.id)))
        service.remove(db,501,radar.id,radar.revision)
        for model,column in [(Search,Search.id),(MonitorSeen,MonitorSeen.search_id),(MonitorMatch,MonitorMatch.search_id),
                             (MonitorMembership,MonitorMembership.search_id),(MonitorWatch,MonitorWatch.search_id)]:
            assert db.scalar(select(func.count()).select_from(model).where(column.in_(ids)))==0
    passed('active_delete_removes_all_execution_tracking_without_orphans')
    with Session(engine) as db:
        try:
            with db.begin():
                db.add(RadarRegion(radar_id='missing-radar',region_id=10))
                db.flush()
            raise AssertionError('Foreign key accepted orphan')
        except IntegrityError:
            pass
    passed('postgres_foreign_keys_reject_orphans')
    report['passed']=True
finally:
    engine.dispose()
    (ROOT/'radar-postgres-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':report.get('passed',False),'checks':len(report['checks']),'database':name}),flush=True)
