"""Local review bundle from projected real observations or explicit fixtures.

python -m experiments.olx_research.review --input JSON --state SQLITE --output JSON
No network, backend import, environment configuration or actual Telegram sender.
"""
import argparse,json
from pathlib import Path
from .flow import ReplayStore
from .evaluation import evaluate_holdout,stability
from .valuation import assess_dataset
from .observations import asking_price_reasons
from .fx_policy import normalize,Quote
from .source_tracking import detail_change


def build(raw,state):
    if raw.get('mode')!='isolated_research':raise ValueError('Explicit isolated mode required')
    if any(not str(u.get('id','')).startswith('FAKE-') for u in raw['users']):raise ValueError('Review accepts labelled FAKE users only')
    cars=raw['listings'];now=raw['now'];quote=Quote.restore(raw['quote']) if raw.get('quote') else None
    dataset=assess_dataset(cars,quote,now,dataset_kind=raw['dataset_kind'])
    db=ReplayStore(state)
    try:
        for c in cars:db.ingest(c)
        previews=db.prepare(raw['users'],quote,now)
    finally:db.close()
    cases=[]
    for c,a in zip(cars,dataset['results']):
        cases.append({'id':c['id'],'url':c['url'],'title':c.get('title'),
            'attributes':{k:c.get(k) for k in ('brand','model','generation','year','engine_cc','fuel','transmission','body','mileage_km','research_condition','drive_type','power_hp','modification','doors')},
            'price':normalize(c,quote,now),'asking_evidence_reasons':asking_price_reasons(c),
            'offer':{k:c.get('eligibility_review',{}).get(k) for k in ('status','reasons','customs_status')},
            'observed_at':c['checked_at'],'newness':detail_change(None,c,first_seen=c.get('first_seen_at') or c['checked_at']),
            'assessment':a,'stability':stability(c,cars,quote,now) if a['status']=='experimental_asking_estimate' else None})
    return {'mode':'isolated_review_only','dataset_kind':raw['dataset_kind'],
            'cases':cases,'coverage':{k:v for k,v in dataset.items() if k!='results'},
            'holdout':evaluate_holdout(cars,raw['split'],quote,now),
            'local_previews':previews,'actual_telegram_calls':0,'production_changed':False}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for arg in ('input','state','output'):ap.add_argument('--'+arg,required=True)
    args=ap.parse_args();result=build(json.loads(Path(args.input).read_text()),args.state)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'real_or_synthetic':result['dataset_kind'],'cars':len(result['cases']),
                      'estimated':result['coverage']['estimated'],'new_local_previews':len(result['local_previews']['prepared']),
                      'real_telegram_calls':0}))


if __name__=='__main__':main()
