"""Single separate Render worker. No source/DB/TG activity before handoff activation."""
from pathlib import Path
import json
import os
import shutil
import signal
import sys
import threading

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'repo'))
REQUIRED_STATE=('free-public-audit-20261020.sqlite3','night21-candidates.sqlite3',
    'night-20261020.owner-observations.sqlite3','night-20261020-runtime.sqlite3')

def verify_handoff(root, owner):
    root=Path(root)
    activation=json.loads((root/'cloud-activation.json').read_text(encoding='utf-8'))
    manifest=json.loads((root/'handoff-manifest.json').read_text(encoding='utf-8'))
    if (activation.get('version')!=1 or activation.get('owner_id')!=owner or
        activation.get('handoff_id')!=manifest.get('handoff_id') or
        activation.get('local_autostart_disabled') is not True):
        raise ValueError('handoff_activation_invalid')
    if not all((root/name).is_file() for name in REQUIRED_STATE):
        raise ValueError('handoff_state_incomplete')
    if (root/'owner-browser-hold.json').exists() or (root/'cloud-service-hold.json').exists():
        raise ValueError('review_required')
    if (root/'owner-service.stop').exists():
        raise ValueError('explicit_stop_marker_present')
    return manifest

def environment(env):
    from sqlalchemy.engine import make_url
    owner=env.get('AUTODEAL_OWNER_ID','')
    if not owner.isdecimal() or int(owner)<=0 or owner!=env.get('SUBSCRIPTION_EXPECTED_ADMIN_ID'):
        raise ValueError('owner_identity_mismatch')
    if any(env.get(k) for k in ('AUTO_RIA_API_KEY','AUTORIA_API_KEY','RIA_API_KEY','OPENAI_API_KEY')):
        raise ValueError('paid_credentials_forbidden')
    url=make_url(env['AUTODEAL_OWNER_DATABASE_URL'])
    if url.drivername not in ('postgres','postgresql','postgresql+psycopg'):
        raise ValueError('postgres_required')
    # The secret must be supplied using this existing DB's INTERNAL Render URL.
    if url.host!='dpg-dal2giqd0e5s738ajb20-a':
        raise ValueError('expected_internal_database_required')
    env['AUTODEAL_OWNER_DATABASE_URL']=url.set(drivername='postgresql+psycopg').update_query_dict(
        {'sslmode':'require'}).render_as_string(hide_password=False)
    if not env.get('AUTODEAL_OWNER_TELEGRAM_TOKEN'):
        raise ValueError('telegram_secret_required')
    return int(owner)

def main():
    stop=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,lambda *_:stop.set())
    root=Path(os.environ.get('AUTODEAL_OWNER_STATE_DIR','/var/data'))
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('persistent_state_directory_required')
    # First deploy can build and wait without touching source, database or Telegram.
    print(json.dumps({'state':'awaiting_handoff','paid_api_calls':0}),flush=True)
    while not stop.is_set() and not (root/'cloud-activation.json').is_file():
        stop.wait(2)
    if stop.is_set():return
    try:
        owner=environment(os.environ)
        verify_handoff(root,owner)
        if shutil.disk_usage(root).free<128*1024*1024:
            raise ValueError('disk_space_low')
        from experiments.free_search.run_owner_service import command
        result=command('run',allow_product_browser=True,shutdown=stop)
        if result.get('state')!='stopped':raise ValueError('service_preflight_blocked')
        print(json.dumps(result),flush=True)
    except Exception as exc:
        # Exceptions may contain secrets. Expose only the class; preserve holds/claims.
        from experiments.free_search.owner_local_bridge import atomic_json
        atomic_json(root/'cloud-service-hold.json',{'state':'blocked','error_type':type(exc).__name__,
            'resume_requires_review':True,'paid_api_calls':0})
        print(json.dumps({'state':'blocked','error_type':type(exc).__name__}),flush=True)
        # Do not let platform restart policy hammer a challenged site or replay a claim.
        while not stop.wait(30):pass

if __name__=='__main__':main()
