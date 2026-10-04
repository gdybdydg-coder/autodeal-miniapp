"""One bounded offline observation replay; never starts a daemon or network call.
Input is a sanitized, authorized export in OUR contract, not an OLX API schema.
Independent control IDs must come from a separately obtained reference sample.
"""
import argparse
import json
from pathlib import Path
import time
from .pipeline import Pipeline


def compare_control(discovered, control):
    actual={(c['source'],c['id']) for c in discovered}
    expected={(c['source'],c['id']) for c in control}
    return {'control_size':len(expected),'found':len(actual & expected),
            'missing':len(expected-actual),'recall':len(actual & expected)/len(expected) if expected else None,
            'independent_provenance_required':True}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--export',required=True);ap.add_argument('--control',required=True)
    ap.add_argument('--database',required=True);ap.add_argument('--now',type=int,required=True)
    ap.add_argument('--pages',type=int,required=True);ap.add_argument('--rows',type=int,required=True)
    args=ap.parse_args()
    for path in (args.export,args.control):
        if Path(path).stat().st_size>2*1024*1024:ap.error('Each input must be <=2 MiB')
    pages=json.loads(Path(args.export).read_text());control=json.loads(Path(args.control).read_text())
    p=Pipeline(args.database);start=time.perf_counter()
    result=p.collect(lambda cursor:pages[cursor or 'start'],args.now,page_budget=args.pages,row_budget=args.rows)
    report={'collection':result,'coverage':compare_control(p.cars(),control),'seconds':time.perf_counter()-start,
            'publication_latency':None,'network_requests':0}
    print(json.dumps(report,indent=2));p.db.close()

if __name__=='__main__':main()
