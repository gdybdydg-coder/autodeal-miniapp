"""Explicit local public scan batches after unlimited-free authorization.

Historical capped ledgers stay untouched. SQLite's integer maximum is an
accounting storage bound, not a new user quota. Source denials persist; no retry,
paid fallback, scheduler or Telegram. Caller must own the shared executor lock.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time
if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.free_search.owner_budget import Budget, Denied
from experiments.free_search.owner_night_runtime import WORKSPACE, LOCK, Coordinator
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_scan import ScanCursor, HEAD
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.public_pagination import inspect_page
from experiments.free_search.network_guard import CREDENTIAL_NAMES


def run(token, *, max_pages=12, transition=False, recovery=False, missing_page_receipt=None):
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError('invalid_batch_size')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(token):raise Denied('executor_lock_required')
    finally:lock.close()
    budget=Budget(WORKSPACE/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,
                  allow_feed_pagination=True)
    observations=Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
    scan=ScanCursor(observations); fetch=PublicFetch(budget)
    report={'started_at':time.time(),'receipts':[],'pages':[],'paid_calls':0,
            'telegram_calls':0,'whole_market_recall':None}
    def get(url,cap=2500000):
        budget.observe(url)
        delay=budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time()
        if delay>60:raise Denied('pause_requires_later_invocation')
        # Leave a small scheduling margin: sleep may return just before next_at.
        time.sleep(max(0,delay)+0.05)
        html,receipt=fetch.get(url,cap);report['receipts'].append(receipt)
        if html is None:
            scan.block_source()
            raise Denied('source_unavailable_no_retry')
        return html,receipt
    try:
        state=scan.status()
        if state and state['phase']=='blocked':
            if missing_page_receipt is not None:
                state=scan.recover_missing_page(time.time(),missing_page_receipt)
            elif recovery and state['reason']=='head_changed_recovery_required':
                state=scan.recover_moving_head(time.time())
            elif not(transition and state['reason']=='scope_metadata_changed'):
                raise Denied('persisted_scan_block')
        get(ROBOTS,20000)
        first=None
        if state and state['phase']=='blocked' and transition:
            html,receipt=get(HEAD+'?page=1')
            metadata=inspect_page(html,HEAD+'?page=1')
            state=scan.transition_initial_scope(receipt['completed_at'],old_scope=state['scope'],
                head=metadata,evidence_sha256=hashlib.sha256(html.encode()).hexdigest())
            report['scope_transition']=state['id']
            first=(html,receipt)
        elif state is None or state['phase'] in ('complete','abandoned'):
            scan.start(time.time(),head_url=HEAD+'?page=1')
        for index in range(max_pages):
            url=scan.status()['expected']
            html,receipt=first if first is not None else get(url)
            first=None
            metadata=inspect_page(html,url); cards=parse_public_cards(html)
            state=scan.accept(metadata,cards,receipt['completed_at'])
            report['pages'].append({'metadata':asdict(metadata),'phase':state['phase'],
                'reason':state['reason'],'cards':[{'listing_id':c.listing_id,'url':c.url,
                 'preview_usd':None if c.preview_usd is None else str(c.preview_usd)} for c in cards]})
            print(json.dumps({'page':metadata.page_index,'cards':len(cards),
                              'phase':state['phase'],'reason':state['reason']}),flush=True)
            if state['phase'] in ('complete','blocked'):break
        report['status']=scan.status()['phase']
    except ValueError as exc:
        report.update(status='stopped',reason=str(exc))
    finally:
        report.update(scan=scan.status(),budget=budget.summary(),completed_at=time.time())
        observations.close();budget.close()
        with (WORKSPACE/'live-scan-20261020.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(report,ensure_ascii=True)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('token');p.add_argument('--transition',action='store_true')
    p.add_argument('--recovery',action='store_true');p.add_argument('--max-pages',type=int,default=12)
    a=p.parse_args();r=run(a.token,max_pages=a.max_pages,transition=a.transition,recovery=a.recovery)
    print(json.dumps({k:r[k] for k in ('status','scan','budget')},ensure_ascii=True))
