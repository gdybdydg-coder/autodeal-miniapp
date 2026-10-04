"""python -m experiments.olx_research.replay --input JSON --state /tmp/replay.db --output JSON
Input contains already acquired listings, fake users and an optional saved quote.
Never starts the application, reads credentials or performs HTTP/Telegram calls.
"""
import argparse,json
from pathlib import Path
from .flow import ReplayStore
from .fx_policy import Quote

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',required=True);ap.add_argument('--state',required=True);ap.add_argument('--output',required=True)
    args=ap.parse_args();raw=json.loads(Path(args.input).read_text())
    if raw.get('mode')!='isolated_research':ap.error('mode must be isolated_research')
    db=ReplayStore(args.state)
    for car in raw['listings']:db.ingest(car)
    quote=Quote.restore(raw['quote']) if raw.get('quote') else None
    result=db.prepare(raw['users'],quote,raw['now']);db.close()
    result['dataset_kind']=raw.get('dataset_kind','unverified');Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'prepared_local_previews':len(result['prepared']),'actual_telegram_calls':0,'held':len(result['held'])}))
if __name__=='__main__':main()
