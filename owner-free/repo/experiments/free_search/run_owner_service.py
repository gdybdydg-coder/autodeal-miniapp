"""Separate local owner service; activation and secret provisioning are explicit.

No app import, .env reading, database migration, paid API, production worker,
autostart installation or automatic lock/hold removal. Children receive an OS
environment allowlist. Stop uses a local marker and the existing intake control.
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import threading
from types import SimpleNamespace

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from experiments.free_search.owner_night_runtime import WORKSPACE,LOCK,Coordinator
from experiments.free_search.owner_local_bridge import atomic_json,child_environment,OwnerBridge,LeasePublisher

REQUIRED=('AUTODEAL_OWNER_DATABASE_URL','AUTODEAL_OWNER_TELEGRAM_TOKEN',
          'AUTODEAL_OWNER_ID','SUBSCRIPTION_EXPECTED_ADMIN_ID')
STOP=WORKSPACE/'owner-service.stop'
CONTROL=WORKSPACE/'owner-service-control.json'


def preflight(env,*,node=None,browser_dependency=False):
    reasons=['missing_'+name for name in REQUIRED if not env.get(name)]
    owner=env.get('AUTODEAL_OWNER_ID','')
    if owner and (not owner.isdecimal() or int(owner)<=0 or owner!=env.get('SUBSCRIPTION_EXPECTED_ADMIN_ID')):
        reasons.append('owner_identity_mismatch')
    dsn=env.get('AUTODEAL_OWNER_DATABASE_URL','')
    if dsn and not dsn.startswith('postgresql+psycopg://'):
        reasons.append('explicit_psycopg_dsn_required')
    if not node:reasons.append('node_missing')
    if not browser_dependency:reasons.append('standalone_playwright_missing')
    return {'ready':not reasons,'blockers':reasons,'paid_api_calls':0,'production_changes':0}


def acquire_service(coordinator,token,now):
    coordinator.db.execute('BEGIN IMMEDIATE')
    try:
        if coordinator.db.execute('SELECT 1 FROM executor').fetchone():raise ValueError('executor_busy')
        coordinator.db.execute('INSERT INTO executor VALUES(1,?,?)',(token,now))
        coordinator.db.execute('COMMIT')
    except BaseException:
        coordinator.db.execute('ROLLBACK');raise


def browser_binary_available(root, platform):
    patterns = ['chromium-*/chrome-win64/chrome.exe'] if platform == 'nt' else [
        'chromium-*/chrome-linux/chrome', 'chromium-*/chrome-linux64/chrome']
    return any(p.is_file() for pattern in patterns for p in Path(root).glob(pattern))


def command(action,*,allow_product_browser=False,shutdown=None):
    shutdown = shutdown if shutdown is not None else threading.Event()
    from experiments.free_search.owner_candidate_queue import CandidateQueue
    from experiments.free_search.owner_continuous_intake import IntakeState
    if action=='resume':
        guard=Coordinator(LOCK)
        try:
            if guard.status()['busy']:return {'state':'blocked','reason':'executor_busy'}
            if (WORKSPACE/'owner-browser-hold.json').exists():return {'state':'blocked','reason':'browser_hold_requires_review'}
            if STOP.exists():STOP.unlink()
            return {'state':'resume_allowed','note':'Explicit run still required; no history or source hold reset'}
        finally:guard.close()
    if action=='stop':
        STOP.write_text('explicit_operator_stop',encoding='utf-8')
        queue=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3')
        try:
            pages=queue.db.execute('SELECT pages FROM intake_control').fetchone()[0]
            IntakeState(queue,pages=pages).stop('operator_stop',time.time())
        finally:queue.close()
        return {'state':'stop_requested'}
    node=os.environ.get('AUTODEAL_OWNER_NODE') or shutil.which('node')
    if node and not Path(node).is_file():node=None
    # Read-only module resolution; never launches a browser.
    dependency=(REPO/'experiments/free_search/node_modules/playwright/package.json').is_file()
    check=preflight(os.environ,node=node,browser_dependency=dependency)
    binary_root = os.environ.get('AUTODEAL_OWNER_BROWSER_BINARIES', str(WORKSPACE/'owner-browser-binaries'))
    if not browser_binary_available(binary_root, os.name):
        check['ready']=False;check['blockers'].append('chromium_binary_missing')
    if action=='preflight':return check
    if not check['ready']:return {'state':'blocked',**check}
    if not allow_product_browser:return {'state':'blocked','reason':'explicit_product_browser_activation_required'}
    if (WORKSPACE/'owner-browser-hold.json').exists():return {'state':'blocked','reason':'browser_hold_requires_review'}
    if STOP.exists():return {'state':'blocked','reason':'explicit_stop_marker_present'}
    from sqlalchemy import create_engine,inspect
    from backend import paid_source_access
    from experiments.free_search.owner_card_transport import TelegramCard
    from experiments.free_search.owner_observations import Observations
    from experiments.free_search.owner_stage_pipeline import StagePipeline
    guard=Coordinator(LOCK);token='owner_service_'+uuid.uuid4().hex;owned=False
    engine=queue=observations=publisher=egress=None;children=[];logs=[];clean_stop=False;failure=None
    try:
        acquire_service(guard,token,time.time());owned=True
        if os.name!='nt':
            from experiments.free_search.owner_cloud_egress import Egress
            egress=Egress(WORKSPACE/'cloud-egress.sqlite3',clock=time.time)
        # The final claim transaction revalidates through a second read session.
        engine=create_engine(os.environ['AUTODEAL_OWNER_DATABASE_URL'],pool_size=2,max_overflow=0,pool_timeout=5,
            connect_args={'connect_timeout':5,'options':'-c statement_timeout=3000 -c lock_timeout=1000'})
        engine.update_execution_options(**{paid_source_access.OPTION:True,paid_source_access.EXCLUSIONS:()})
        if not inspect(engine).has_table('owner_free_comparison_deliveries'):
            raise ValueError('existing_comparison_table_required')
        settings=SimpleNamespace(live=True,admin_telegram_id=int(os.environ['AUTODEAL_OWNER_ID']))
        queue=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3')
        observations=Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
        pipeline=StagePipeline(queue,observations)
        bridge=OwnerBridge(engine,settings,pipeline,
            TelegramCard(os.environ['AUTODEAL_OWNER_TELEGRAM_TOKEN'],settings.admin_telegram_id),WORKSPACE)
        snapshot=bridge.refresh()
        if snapshot['status']!='readonly_ok':raise ValueError('current_owner_access_required')
        pages=queue.db.execute('SELECT pages FROM intake_control').fetchone()[0]
        state=IntakeState(queue,pages=pages)
        if pages<6:state.expand(6,time.time());pages=6
        state.resume(time.time())
        env=child_environment(os.environ)
        creation=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        def launch(argv,name):
            log=(WORKSPACE/(name+'.log')).open('ab');logs.append(log)
            proc=subprocess.Popen(argv,cwd=REPO,env=env,stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,creationflags=creation)
            children.append(proc);return proc
        def reap(proc):
            index=children.index(proc)
            children.pop(index);logs.pop(index).close()
        publisher=LeasePublisher(CONTROL,lambda:{'version':1,'token':token,'enabled':True,
                'heartbeat':time.time(),'access_at':snapshot['at']})
        publisher.start()
        browser=launch([node,str(REPO/'experiments/free_search/owner_browser_service.mjs'),str(WORKSPACE),token],'owner-browser-service')
        source=None;next_access=time.time()+30;next_status=0
        while not STOP.exists() and not shutdown.is_set():
            if not guard.owns(token):raise ValueError('executor_lock_lost')
            if egress is not None:egress.check()
            if publisher.error:raise RuntimeError('lease_write_failed')
            if browser.poll() is not None:raise ValueError('browser_process_stopped')
            if (WORKSPACE/'owner-browser-hold.json').exists():raise ValueError('browser_source_hold')
            if state.action(time.time(),False)[0]=='stop':raise ValueError('intake_stopped')
            if time.time()>=next_access:
                snapshot=bridge.refresh();next_access=time.time()+30
                if snapshot['status']!='readonly_ok':raise ValueError('current_owner_access_revoked')
            if source is None or source.poll() is not None:
                if source is not None and source.returncode!=0:raise ValueError('source_process_stopped')
                if source is not None:reap(source)
                source=launch([sys.executable,'-I',str(REPO/'experiments/free_search/owner_intake_worker.py'),token,str(pages)],'owner-intake-service')
            bridge.deliver_ready()
            if time.time()>=next_status:
                if shutil.disk_usage(WORKSPACE).free < 128*1024*1024:
                    raise ValueError('disk_space_low')
                atomic_json(WORKSPACE/'owner-service-status.json',{'state':'running','at':time.time(),
                    'token':token,'pid':os.getpid(),'browser_pid':browser.pid,'intake_pid':source.pid,
                    'access_at':snapshot['at'],'pages':pages,'paid_api_calls':0})
                next_status=time.time()+10
            time.sleep(.5)
        clean_stop=True
        return {'state':'stopped'}
    except BaseException as exc:
        failure=type(exc).__name__;raise
    finally:
        if owned:
            if publisher is not None:publisher.stop()
            atomic_json(CONTROL,{'version':1,'token':token,'enabled':False,'heartbeat':time.time(),'access_at':0})
            if queue is not None:
                pages=queue.db.execute('SELECT pages FROM intake_control').fetchone()[0]
                IntakeState(queue,pages=pages).stop('service_stopped',time.time())
            # Browser observes lease loss. Source checks stop before every GET.
            until=time.monotonic()+8
            while any(p.poll() is None for p in children) and time.monotonic()<until:time.sleep(.25)
            for proc in children:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=2)
            guard.release(token)
            atomic_json(WORKSPACE/'owner-service-status.json',{'state':'stopped' if clean_stop else 'blocked',
                'at':time.time(),'token':token,'pid':os.getpid(),'error_type':failure,'paid_api_calls':0})
        for log in logs:log.close()
        if queue is not None:queue.close()
        if observations is not None:observations.close()
        if engine is not None:engine.dispose()
        if egress is not None:egress.close()
        guard.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['preflight','run','stop','resume'])
    parser.add_argument('--allow-product-browser',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(command(args.action,allow_product_browser=args.allow_product_browser)))
    except Exception as exc:
        # SQL/transport exceptions may embed credentials. Never print their text.
        print(json.dumps({'state':'blocked','error_type':type(exc).__name__}))
        raise SystemExit(2)
