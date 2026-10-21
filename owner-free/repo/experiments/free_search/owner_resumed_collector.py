"""Explicit local resumed collector, shared history/budget, no sender or scheduler.

Requires a held executor token. A prior scope block is never automatically
abandoned; moving-head recovery is opt-in and bounded by ScanCursor's chain cap.
"""
import argparse
import json
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.free_search.owner_budget import Budget
from experiments.free_search.owner_night_runtime import LEDGER, WORKSPACE
from experiments.free_search.owner_night_scan import run
from experiments.free_search.owner_scan import HEAD


def collect(token, max_pages=2, recover_moving=False):
    # No replacement ledger and no enlargement of its cumulative research caps.
    result=run(token,max_pages,budget_factory=lambda:Budget(LEDGER,40,100000000),
               head_url=HEAD+'?page=1',recover_moving=recover_moving)
    result['kind']='bounded_resumed_scan'
    with (WORKSPACE/'resumed-collector-20261020.jsonl').open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(result,ensure_ascii=True)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('token')
    parser.add_argument('--max-pages',type=int,default=2)
    parser.add_argument('--recover-moving-head',action='store_true')
    args=parser.parse_args()
    print(json.dumps(collect(args.token,args.max_pages,args.recover_moving_head),indent=2))
