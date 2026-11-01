"""Persistent owner-only worker, disabled until verified handoff activation."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path('/var/data/sorted-v1')

def validate(root, env):
    owner = int(env['AUTODEAL_OWNER_ID'])
    if owner <= 0 or str(owner) != env.get('SUBSCRIPTION_EXPECTED_ADMIN_ID'):
        raise ValueError('owner_identity_mismatch')
    if any(env.get(k) for k in ('AUTO_RIA_API_KEY','AUTORIA_API_KEY','RIA_API_KEY','OPENAI_API_KEY')):
        raise ValueError('paid_credentials_forbidden')
    activation = json.loads((root/'activation.json').read_text())
    if activation.get('owner_id') != owner or activation.get('local_stopped') is not True:
        raise ValueError('handoff_not_verified')
    with sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro', uri=True) as db:
        if db.execute('SELECT owner FROM identity').fetchall() != [(owner,)]:
            raise ValueError('wrong_handoff_owner')
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('handoff_corrupt')
    from sqlalchemy.engine import make_url
    url = make_url(env['AUTODEAL_OWNER_DATABASE_URL'])
    if url.host != 'dpg-dal2giqd0e5s738ajb20-a':
        raise ValueError('expected_internal_database_required')
    env['AUTODEAL_OWNER_DATABASE_URL'] = url.set(drivername='postgresql+psycopg').update_query_dict({'sslmode':'require'}).render_as_string(hide_password=False)

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    def shutdown(*_):
        stopped.set()
        (ROOT/'stop').write_text('platform_shutdown')
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with (ROOT/'server.flock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps({'state':'awaiting_sorted_handoff','paid_api_calls':0}), flush=True)
        while not (ROOT/'activation.json').exists():
            if stopped.wait(2): return
        validate(ROOT, os.environ)
        # A persistent OS lock, released by the kernel on crash, protects this
        # single-instance directory. Never clean historical claims or holds.
        for name in ('worker.lock','cache/client.lock'):
            (ROOT/name).unlink(missing_ok=True)
        marker = ROOT/'stop'
        if marker.exists() and marker.read_text().strip() == 'platform_shutdown':
            marker.unlink()
        if marker.exists():
            print(json.dumps({'state':'operator_stopped'}), flush=True)
            while not stopped.wait(10): pass
            return
        command = [sys.executable,'-I','/app/repo/experiments/free_search/owner_sorted_service.py','run','--state-dir',str(ROOT)]
        child = subprocess.Popen(command)
        while child.poll() is None:
            if stopped.wait(2):
                try: child.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try: child.wait(timeout=15)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                return
        raise RuntimeError('worker_exited')

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'state':'failed','error_type':type(exc).__name__}), flush=True)
        # Avoid a tight source retry loop on platform-managed restarts.
        time.sleep(60)
        sys.exit(1)
