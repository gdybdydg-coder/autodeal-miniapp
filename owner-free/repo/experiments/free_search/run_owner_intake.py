"""Explicit local intake lifecycle. No scheduled/background installation."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.free_search.owner_continuous_intake import IntakeState, run_owned
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_session_runtime import session
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, WORKSPACE


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['run','status','stop','resume'])
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--max-requests',type=int,default=40)
    args=parser.parse_args()
    if args.action=='run':
        with session(LOCK,args.seconds) as deadline:
            coordinator=Coordinator(LOCK)
            try:token=coordinator.db.execute('SELECT token FROM executor').fetchone()[0]
            finally:coordinator.close()
            report=run_owned(token,deadline,max_requests=args.max_requests)
            print(json.dumps({k:v for k,v in report.items() if k not in ('details','receipts','tickets')},default=str))
        return
    q=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3')
    try:
        state=IntakeState(q)
        if args.action=='stop':state.stop('operator_stop',time.time())
        elif args.action=='resume':state.resume(time.time())
        row=q.db.execute('SELECT next_page,next_head,stopped,reason FROM intake_control').fetchone()
        print(json.dumps(dict(zip(('next_page','next_head','stopped','reason'),row))))
    finally:q.close()


if __name__=='__main__':main()
