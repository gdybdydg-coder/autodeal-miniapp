"""Two passes of the first six public pages; no sender or scheduler."""
import sys,json,time,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dataclasses import asdict
from experiments.free_search.owner_live_scan import WORKSPACE,LOCK
from experiments.free_search.owner_night_runtime import Coordinator,RENEWED_DEADLINE
from experiments.free_search.owner_budget import Budget,Denied
from experiments.free_search.owner_public_fetch import PublicFetch,ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_head_window import HeadWindow,HEAD
from experiments.free_search.public_pagination import inspect_page
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_stage_pipeline import StagePipeline

def wait_for_fetch(next_at,deadline):
    """Reserve transport timeout headroom; recheck after OS sleep/suspension."""
    now=time.time()
    wait=max(0,next_at-now)+.05
    if now+wait+21>=deadline:raise Denied('batch_deadline')
    time.sleep(wait)
    if time.time()+21>=deadline:raise Denied('batch_deadline')

def run(token, *, pages=6):
    if type(pages) is not int or not 1<=pages<=6:raise ValueError('invalid_head_page_count')
    if time.time()>=RENEWED_DEADLINE:raise Denied('night_ended')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):raise Denied('production_configuration_present')
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(token):raise Denied('executor_lock_required')
    finally:lock.close()
    budget=Budget(WORKSPACE/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
    obs=Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
    window=HeadWindow(obs);fetch=PublicFetch(budget)
    queue=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3');pipeline=StagePipeline(queue,obs)
    report=dict(receipts=[],pages=[],paid_calls=0,telegram_calls=0,whole_market_recall=None,window_pages=pages)
    try:
        seen_ids=set()
        for url in (ROBOTS,*(HEAD+f'?page={p}' for _ in range(2) for p in range(1,pages+1))):
            budget.observe(url)
            wait_for_fetch(budget.db.execute('SELECT next_at FROM budget').fetchone()[0],RENEWED_DEADLINE)
            body,receipt=fetch.get(url,20000 if url==ROBOTS else 2500000)
            report['receipts'].append(receipt)
            if body is None:raise Denied('source_unavailable_no_retry')
            if url==ROBOTS:continue
            meta=inspect_page(body,url);cards=parse_public_cards(body)
            result=window.accept(meta,cards,receipt['completed_at'])
            result['queued_ids']=pipeline.ingest(cards,receipt['completed_at'])
            result['queue_skipped']=pipeline.skipped
            seen_ids.update(c.listing_id for c in cards)
            report['pages'].append(dict(metadata=asdict(meta),result=result,cards=[dict(listing_id=c.listing_id,url=c.url,preview_usd=str(c.preview_usd) if c.preview_usd is not None else None) for c in cards]))
        report['signals']=[obs.signal(ident,time.time()) for ident in sorted(seen_ids)]
    except ValueError as exc:report['reason']=str(exc)
    finally:
        report['budget']=budget.summary();queue.close();obs.close();budget.close()
        with (WORKSPACE/'head-window-20261020.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(report)+'\n')
    return report

if __name__=='__main__':print(json.dumps(run(sys.argv[1],pages=1 if '--head-only' in sys.argv[2:] else 6)))
