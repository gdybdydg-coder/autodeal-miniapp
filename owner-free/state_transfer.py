"""Offline handoff. Refuses an active executor, partial copy, or existing target.

Run export only AFTER graceful local stop AND disabling its Windows task.
Copy the resulting directory via authenticated Render SSH/SFTP, never Git.
Neither export nor restore enables the server. Activation is a separate final step.
"""
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import time
import uuid

REQUIRED=('free-public-audit-20261020.sqlite3','night21-candidates.sqlite3',
          'night-20261020.owner-observations.sqlite3','night-20261020-runtime.sqlite3',
          'night-20261020.sqlite3','public-mean-50-20261020.sqlite3')
OPTIONAL=('owner-service-deliveries.jsonl','continuous-intake-20261021.jsonl',
          'owner-browser-hold.json')
LOCK='night-20261020-runtime.sqlite3'

@contextmanager
def connect(*args,**kwargs):
    db=sqlite3.connect(*args,**kwargs)
    try:
        with db:yield db
    finally:db.close()

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def safe_name(name):
    p=PurePosixPath(name)
    if not name or p.is_absolute() or '\\' in name or ':' in name or '..' in p.parts:
        raise ValueError('unsafe_manifest_path')
    if name in REQUIRED+OPTIONAL:return p
    if len(p.parts)==2 and p.parts[0]=='continuous-browser-outbox' and (
        p.name=='browser-http.jsonl' or re.fullmatch(r'[1-9][0-9]{0,11}\.(job|result)\.json',p.name)):
        return p
    raise ValueError('unexpected_manifest_file')

def backup(source,target):
    with connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as src:
        with connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('database_integrity_failed')

def export_state(source,destination,*,disabled_proof):
    source,destination=Path(source),Path(destination)
    proof=json.loads(Path(disabled_proof).read_text(encoding='utf-8-sig'))
    if proof.get('task')!='AutoDeal-Owner-Free-Search' or proof.get('enabled') is not False:
        raise ValueError('local_autostart_disabled_proof_required')
    if not 0<=time.time()-proof.get('at',0)<=300:
        raise ValueError('fresh_local_disabled_proof_required')
    status=json.loads((source/'owner-service-status.json').read_text(encoding='utf-8'))
    if status.get('state')!='stopped' or not (source/'owner-service.stop').is_file():
        raise ValueError('graceful_local_stop_required')
    if (source/'continuous-browser-outbox/browser-service.lock').exists():
        raise ValueError('browser_still_owned')
    if not all((source/n).is_file() for n in REQUIRED):raise ValueError('historical_state_missing')
    token='handoff_'+uuid.uuid4().hex
    db=sqlite3.connect(source/LOCK,isolation_level=None,timeout=3)
    owned=False
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM executor').fetchone():raise ValueError('executor_busy')
        db.execute('INSERT INTO executor VALUES(1,?,?)',(token,time.time()))
        db.execute('COMMIT');owned=True
        destination.mkdir(parents=True,exist_ok=False)
        for name in REQUIRED:
            backup(source/name,destination/name)
        with connect(destination/LOCK) as copy:
            if copy.execute('DELETE FROM executor WHERE token=?',(token,)).rowcount!=1:
                raise ValueError('handoff_lock_mismatch')
        for name in OPTIONAL:
            if (source/name).is_file():shutil.copyfile(source/name,destination/name)
        outbox=destination/'continuous-browser-outbox';outbox.mkdir()
        for file in (source/'continuous-browser-outbox').iterdir():
            name='continuous-browser-outbox/'+file.name
            try:safe_name(name)
            except ValueError:continue
            if file.is_symlink():raise ValueError('symlink_not_allowed')
            if file.is_file():shutil.copyfile(file,outbox/file.name)
        if not (source/'owner-service.stop').is_file():raise ValueError('local_stop_changed')
        files={f.relative_to(destination).as_posix():digest(f) for f in destination.rglob('*') if f.is_file()}
        manifest={'version':1,'handoff_id':token,'created_at':time.time(),
            'local_autostart_disabled':True,'files':files,'paid_api_calls':0,'activated':False}
        (destination/'handoff-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        return manifest
    finally:
        if db.in_transaction:db.execute('ROLLBACK')
        if owned:db.execute('DELETE FROM executor WHERE token=?',(token,))
        db.close()

def verify_bundle(bundle):
    bundle=Path(bundle)
    manifest=json.loads((bundle/'handoff-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version')!=1 or manifest.get('activated') is not False:
        raise ValueError('invalid_handoff_manifest')
    if not set(REQUIRED)<=set(manifest['files']):raise ValueError('historical_state_missing')
    for name,expected in manifest['files'].items():
        safe_name(name)
        path=bundle/name
        if path.is_symlink() or not path.is_file() or digest(path)!=expected:
            raise ValueError('handoff_hash_mismatch')
    with connect((bundle/LOCK).resolve().as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('SELECT 1 FROM executor').fetchone():raise ValueError('copied_executor_busy')
    return manifest

def restore_state(bundle,destination):
    bundle,destination=Path(bundle),Path(destination)
    manifest=verify_bundle(bundle)
    paths=list(manifest['files'])+['handoff-manifest.json']
    if (destination/'cloud-activation.json').exists() or any((destination/n).exists() for n in paths):
        raise ValueError('destination_not_empty')
    destination.mkdir(parents=True,exist_ok=True)
    for name in paths:
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as out, (bundle/name).open('rb') as src:shutil.copyfileobj(src,out)
    verify_bundle(destination)
    return {'state':'restored_not_activated','handoff_id':manifest['handoff_id']}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['export','verify','restore'])
    parser.add_argument('source');parser.add_argument('destination',nargs='?');parser.add_argument('--disabled-proof')
    args=parser.parse_args()
    if args.action=='export':
        result=export_state(args.source,args.destination,disabled_proof=args.disabled_proof)
        print(json.dumps({'state':'exported_not_activated','files':len(result['files']),'handoff_id':result['handoff_id']}))
    elif args.action=='restore':print(json.dumps(restore_state(args.source,args.destination)))
    else:print(json.dumps({'state':'verified','files':len(verify_bundle(args.source)['files'])}))
