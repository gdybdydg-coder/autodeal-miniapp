"""Explicit disposable LOCAL PostgreSQL integration, no Telegram/network sources.

Run with -I from repository root. Reads only a generated local test password.
Never accepts DATABASE_URL or production configuration. Keeps evidence in JSON.
"""
from pathlib import Path
import sys, os, threading, json, time, uuid
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import psycopg
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from backend import paid_source_access
from backend.models import Base, User, Search, Filters, MonitorWatch, Delivery, Listing
from backend.billing_models import Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from experiments.free_search.tests.test_owner_pilot import page, NOW, OWNER, CLIENT
from experiments.free_search.owner_access import current_searches
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import PublicCard
from experiments.free_search import owner_delivery
from types import SimpleNamespace

local = root.parent/'.local-postgres'
config = dict(host='127.0.0.1', port=55439, user='pilot',
              password=(local/'password.txt').read_text().strip())
database = 'autodeal_owner_concurrency'
with psycopg.connect(**config, dbname='postgres', autocommit=True) as db:
    if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(database,)).fetchone():
        db.execute('CREATE DATABASE autodeal_owner_concurrency')
engine = create_engine(URL.create('postgresql+psycopg', database=database,
    username=config['user'],host=config['host'],port=config['port'],password=config['password']),
                       connect_args={'options':'-c statement_timeout=10000 -c lock_timeout=5000'})
engine.update_execution_options(**{paid_source_access.OPTION:True,paid_source_access.EXCLUSIONS:()})
assert engine.url.host == '127.0.0.1' and engine.url.database == database
os.environ['SUBSCRIPTION_EXPECTED_ADMIN_ID'] = str(OWNER)
settings = SimpleNamespace(live=True,admin_telegram_id=OWNER)
Base.metadata.create_all(engine); ManualBase.metadata.create_all(engine)
subject = page(); peers = [page(str(20000000+i),10000+i*100) for i in range(6)]
results = []
run_tag = uuid.uuid4().hex[:12]

def seed(case):
    # Exact local database checked above; truncate only disposable fixture tables.
    with engine.begin() as db:
        db.execute(text('TRUNCATE deliveries, delivery_timings, listings, monitor_watches, searches, users, billing_entitlements, manual_payment_requests RESTART IDENTITY CASCADE'))
    f = Filters(brand='Example')
    with Session(engine) as db, db.begin():
        for uid in (OWNER, CLIENT):
            db.add(User(id=uid,ready=True))
            db.add(PaymentRequest(id=str(uid),user_id=uid,state='approved',amount_minor=25000,
                currency='UAH',days=30,created_at=NOW-100,updated_at=NOW-100,expires_at=NOW+1000))
            db.add(Entitlement(user_id=uid,expires_at=NOW+1000,updated_at=NOW-100))
            db.add(Search(id=uid,user_id=uid,name='Synthetic',enabled=True,filters=f.canonical(),fingerprint=f.fingerprint()))
            db.add(MonitorWatch(search_id=uid,epoch='test-'+str(uid)))
    path = local/(case+'-'+run_tag+'.owner-observations.sqlite3')
    obs = Observations(path)
    def card(ident): return PublicCard(ident,page(ident).url,None,None,None,False,(),())
    # Each case uses its own durable fixture identity and retained state.
    obs.ingest([card('98765432')],NOW-30)
    obs.ingest([card(subject.listing_id)],NOW-10); obs.close()
    return path,current_searches(engine,settings,NOW)[0][0]

def invoke(path,snapshot,transport,clock=lambda:NOW):
    obs=Observations(path)
    try:
        return owner_delivery.send_one(engine,settings,snapshot,subject,peers,
            [p.listing_id for p in peers],obs,transport,enabled=True,clock=clock)
    finally: obs.close()

path,snapshot=seed('concurrent')
barrier=threading.Barrier(2); calls=[]; original=owner_delivery.prepare
def prepared(*args):
    result=original(*args); barrier.wait(timeout=5); return result
def transport(uid,message):
    assert uid==OWNER; calls.append(uid)
    return {'ok':True,'result':{'message_id':101}}
with patch.object(owner_delivery,'prepare',prepared), ThreadPoolExecutor(2) as workers:
    futures=[workers.submit(invoke,path,snapshot,transport) for _ in range(2)]
    outcomes=[f.result(timeout=15) for f in futures]
assert len(calls)==1, outcomes
with Session(engine) as db:
    assert len(db.scalars(select(Delivery)).all())==1
results.append(dict(case='two_concurrent_same_id',transport_calls=len(calls),outcomes=outcomes))

for case in ('stop_while_waiting','expiry_while_waiting','epoch_while_waiting'):
    path,snapshot=seed(case); entered=threading.Event(); clock_value=[NOW]; calls=[]
    access=owner_delivery._access
    def blocked(db,settings,snapshot,clock):
        entered.set(); return access(db,settings,snapshot,clock)
    locker=Session(engine); owner=locker.scalar(select(User).where(User.id==OWNER).with_for_update())
    with patch.object(owner_delivery,'_access',blocked), ThreadPoolExecutor(1) as workers:
        future=workers.submit(invoke,path,snapshot,transport,lambda:clock_value[0])
        assert entered.wait(timeout=5)
        deadline=time.monotonic()+3
        waited=False
        while time.monotonic()<deadline:
            with engine.connect() as monitor:
                waited=bool(monitor.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")))
            if waited: break
            time.sleep(.02)
        assert waited, 'row_lock_wait_not_observed'
        if case.startswith('stop'): owner.ready=False
        elif case.startswith('epoch'): locker.get(MonitorWatch,OWNER).epoch='edited'
        else: clock_value[0]=NOW+1001
        locker.commit(); locker.close()
        outcome=future.result(timeout=15)
    assert not calls and outcome['state']=='blocked',outcome
    results.append(dict(case=case,transport_calls=0,postgres_lock_wait_observed=waited,outcome=outcome))

path,snapshot=seed('crash_after_acceptance'); calls=[]
def crash(uid,message):
    calls.append(uid); raise SystemExit('synthetic_crash')
try: invoke(path,snapshot,crash)
except SystemExit: pass
replay=invoke(path,snapshot,transport)
assert len(calls)==1 and replay.get('reason')=='existing_owner_delivery'
results.append(dict(case='crash_after_possible_acceptance_no_replay',transport_calls=1,replay=replay))

with engine.connect() as db: version=db.scalar(text('SELECT version()'))
engine.dispose()
report=dict(status='passed',database_scope='localhost_disposable_synthetic_only',
    postgres_version=version,cases=results,real_telegram_calls=0,paid_api_calls=0)
(root.parent/'owner-postgres-concurrency-20261019.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report,indent=2))
