"""Local acceptance-test server. Synthetic identity/payment; provider work disabled."""
import argparse
import mimetypes
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.app import Settings,create_app
from backend.models import Base,User
from backend.billing_models import Entitlement
from backend.radar_catalog import import_catalog

parser=argparse.ArgumentParser()
parser.add_argument('--database',required=True)
parser.add_argument('--port',type=int,default=8791)
args=parser.parse_args()
target=Path(args.database).resolve()
if target.exists():
    raise RuntimeError('Use a new disposable fixture database')
target.parent.mkdir(parents=True,exist_ok=True)
engine=create_engine('sqlite:///'+str(target),connect_args={'check_same_thread':False})
Base.metadata.create_all(engine)
with Session(engine) as db,db.begin():
    db.add_all([User(id=111,ready=True),User(id=222,ready=True)])
    db.flush()
    db.add(Entitlement(user_id=111,expires_at=time.time()+86400,updated_at=time.time()))
    import_catalog(db,{'brands':[
        {'id':9,'name':'BMW','models_available':True,'models':[{'id':3219,'name':'3 Series'},{'id':2319,'name':'5 Series'}]},
        {'id':6,'name':'Audi','models_available':True,'models':[{'id':49,'name':'A6'},{'id':47,'name':'A4'}]}],
        'regions':[{'id':10,'name':'Київська'},{'id':1,'name':'Вінницька'},{'id':17,'name':'Луганська'}]})
settings=Settings(database_url='sqlite:///'+str(target),bot_token='12345:RADAR_BROWSER_FIXTURE_ONLY',
    webhook_secret='radar-browser-fixture-secret-only-0123456789',radar_enabled=True,
    origin=f'http://127.0.0.1:{args.port}')
app=create_app(settings,engine,paid_source_only=True)
@app.get('/')
def index():
    return FileResponse(ROOT/'index.html')
@app.get('/{asset}')
def static(asset:str):
    if '/' in asset or '\\' in asset or asset.startswith('.') or Path(asset).suffix not in {'.html','.js','.css','.json'}:
        raise HTTPException(404)
    path=ROOT/asset
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,media_type=mimetypes.guess_type(path)[0])
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
