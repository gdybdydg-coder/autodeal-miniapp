"""Consume a visible CUA capture into the local queue; never send or authorize."""
import argparse
import json
from pathlib import Path
import sys
import time

REPO=Path(__file__).resolve().parents[2]
if __name__=='__main__': sys.path.insert(0,str(REPO))
from experiments.free_search.owner_night_runtime import Coordinator,LOCK,RENEWED_DEADLINE
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_stage_pipeline import StagePipeline


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('token');parser.add_argument('capture_file')
    parser.add_argument('--research-percent',required=True)
    args=parser.parse_args()
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(args.token): raise ValueError('executor_lock_required')
        if time.time()>=RENEWED_DEADLINE: raise ValueError('night_ended')
        root=REPO.parent
        capture=json.loads(Path(args.capture_file).read_text(encoding='utf-8'))
        ident=capture['listing_id']
        queue=CandidateQueue(root/'night21-candidates.sqlite3')
        obs=Observations(root/'night-20261020.owner-observations.sqlite3')
        try:
            pipeline=StagePipeline(queue,obs)
            current=queue.get(ident)
            if current is None: raise ValueError('candidate_not_discovered')
            if current['stage']=='detail': pipeline.capture(ident,capture,time.time())
            else:
                evidence=queue.evidence(ident,'range')
                if current['stage']!='range' or not evidence or evidence['capture']!=capture:
                    raise ValueError('capture_restart_conflict')
            rule=pipeline.qualify(ident,args.research_percent,time.time())
            print(json.dumps({'listing_id':ident,'rule':rule,'send_authorized':False,
                              'next':'current owner/access/filter/dedup revalidation in sender'}))
        finally: obs.close();queue.close()
    finally: lock.close()


if __name__=='__main__':main()
