"""Reproducible saved evidence evaluation; offline only, no app/.env."""
import json,time,socket,argparse,hashlib
from pathlib import Path
# No network can happen during imports or replay.
def deny(*a,**k):raise AssertionError('offline_saved_real_replay')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.socket.sendto=deny;socket.getaddrinfo=deny
from backend.olx_market.valuation import assess_dataset,estimate
from backend.olx_market.evaluation import evaluate_holdout
from backend.olx_market.fx_policy import Quote
from backend.olx_market.candidates import filter_reasons
parser=argparse.ArgumentParser(description='Offline replay; pass the immutable research/f95f1c1 current-cohort fixture')
parser.add_argument('--night-cohort',type=Path,required=True)
args=parser.parse_args()
assert hashlib.sha256(args.night_cohort.read_bytes()).hexdigest()=='0d9e72dfb10c555afbe1d7306222a6d1f2dca299c35f3581504c2eeacfb49076', 'Unexpected night fixture; do not silently replace controls/data'
root=Path('experiments/olx_research')
rows=json.loads(args.night_cohort.read_text())['listings']
rows+=json.loads((root/'owner-20261005/fresh-details-sanitized.json').read_text())
rows+=json.loads((root/'owner-20261005/enable-new-details-sanitized.json').read_text())
fresh=json.loads((Path(__file__).parent/'fresh-details-sanitized.json').read_text())
by={}
for c in rows+fresh:
 key=(c['source'],c['id'])
 if key not in by or c.get('checked_at',0)>=by[key].get('checked_at',0):by[key]=c
cars=list(by.values());now=int(time.time())
e=json.loads((root/'owner-20261005/current-evidence.json').read_text());quote=Quote.restore(e['quote'])
plan=json.loads((root/'owner-20261005/source-plan.json').read_text())
result=assess_dataset(cars,quote,now,minimum=8)
freeze=json.loads((root/'owner-20261005/split-freeze.json').read_text())
controls=evaluate_holdout([c for c in cars if c['id'] in freeze['membership']],freeze['membership'],quote,now,minimum=8)
ref=[c for c in cars if freeze['membership'].get(c['id'])!='holdout']
observations=[]
for c in fresh:
 a=estimate(c,ref,quote,now,minimum=8)
 filt=filter_reasons(c,plan['owner_search']['filters'],quote,now)
 observations.append({'source':c['source'],'id':c['id'],'url':c['url'],'checked_at':c['checked_at'],'price':c['price'],'currency':c['currency'],'owner_filter':filt,'sample':a['sample'],'reference_usd':a['reference_usd'],'status':a['status'],'reasons':a['reasons'],'exclusions':a['exclusions'],'research_condition':c['research_condition'],'generation_variant':c.get('generation_variant'),'target_identity_verified':bool(c.get('vehicle_key')),'technical_ready':False})
authorization_path=root/'owner-launch-20261005/authorization.json'
authorization=json.loads(authorization_path.read_text()) if authorization_path.exists() else {}
owner_authorized=authorization.get('owner_authorized') is True and authorization.get('client_authorized') is False
report={'kind':'mixed_saved_real_replay_with_three_current_details_not_live_market_census','checked_at':now,'unique_loaded_ads':len(cars),'fresh_detail_ads':len(fresh),'asking_estimates':result['estimated'],'max_exploratory_sample':max(r['sample'] for r in result['results']), 'exploratory_pool_not_validated_reference':True,'minimum':8,'frozen_holdout':controls['frozen_holdout_count'],'eligible_holdout':controls['eligible_holdout_count'],'estimated_holdout':controls['estimated_holdout_count'],'metrics':controls['metrics'],'selected_method':None,'owner_previews':observations,'all_asking_prices_not_sales':True,'fresh_source_complete':False,'technical_ready':False,'new_owner_permission_pending':not owner_authorized,'owner_authorized':owner_authorized,'authorization_receipt':authorization.get('id'),'client_authorized':False,'paid_ria_calls':0,'telegram_calls':0,'quote':quote.payload()}
dest=root/'prelaunch-20261005';dest.mkdir(exist_ok=True)
(dest/'saved-real-evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
(dest/'fresh-details-sanitized.json').write_text(json.dumps(fresh,ensure_ascii=False,indent=2))
# Source receipts are immutable observations; offline replay never replaces them.
print(json.dumps({k:v for k,v in report.items() if k not in ('quote','metrics','owner_previews')},ensure_ascii=False))
for c in observations:print(json.dumps({k:c[k] for k in ('id','sample','reference_usd','status','owner_filter')},ensure_ascii=False))
