"""One bounded night queue dispatch, using the existing free source ledger."""
import sys,json,time,os
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
if __name__=='__main__':sys.path.insert(0,str(REPO))
from experiments.free_search.owner_night_runtime import Coordinator,LOCK,RENEWED_DEADLINE
from experiments.free_search.owner_budget import Budget,Denied
from experiments.free_search.owner_public_fetch import PublicFetch,ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_stage_pipeline import StagePipeline
from experiments.free_search.owner_queue_dispatch import dispatch
from experiments.free_search.owner_pilot import PublicPage
from experiments.free_search.network_guard import CREDENTIAL_NAMES


def run(token,limit=5):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):raise Denied('production_configuration_present')
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(token):raise Denied('executor_lock_required')
    finally:lock.close()
    deadline=min(time.time()+120,RENEWED_DEADLINE)
    if time.time()>=deadline:raise Denied('night_ended')
    root=REPO.parent
    queue=CandidateQueue(root/'night21-candidates.sqlite3')
    obs=Observations(root/'night-20261020.owner-observations.sqlite3')
    budget=Budget(root/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
    fetch=PublicFetch(budget);receipts=[]
    report={'paid_calls':0,'telegram_calls':0}
    def get(url,cap):
        budget.observe(url)
        wait=max(0,budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())+.05
        # Allow the bounded transport timeout to finish before the hard deadline.
        if time.time()+wait+21>=deadline:raise Denied('batch_deadline')
        time.sleep(wait)
        html,receipt=fetch.get(url,cap);receipts.append(receipt)
        if html is None:raise Denied('source_unavailable_no_retry')
        return html,receipt
    def detail(ident,url):
        if fetch.robots is None:get(ROBOTS,20000)
        html,receipt=get(url,2500000)
        return PublicPage(ident,url,html,receipt['completed_at'])
    try:
        report.update(dispatch(StagePipeline(queue,obs),detail,limit=limit,deadline=deadline))
        report['awaiting_browser']=queue.pending(20,stage='detail')
    finally:
        report.update(receipts=receipts,budget=budget.summary(),completed_at=time.time())
        with (root/'night21-queue-runs.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(report,default=str,ensure_ascii=False)+'\n')
        budget.close();obs.close();queue.close()
    return report


if __name__=='__main__':print(json.dumps(run(sys.argv[1]),default=str))
