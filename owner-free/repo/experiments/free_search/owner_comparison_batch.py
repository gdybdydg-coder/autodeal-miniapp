"""Explicit bounded owner comparison intake; public data only, no delivery authority.

Existing history, request ledger and shared executor lock are never reset.
Owner snapshot filters rank research candidates; sender rereads actual access.
"""
from pathlib import Path
import sys,os,time,json
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dataclasses import asdict
from decimal import Decimal
from experiments.free_search.owner_session_runtime import session
from experiments.free_search.owner_night_runtime import LOCK,WORKSPACE
from experiments.free_search.owner_budget import Budget,Denied
from experiments.free_search.owner_public_fetch import PublicFetch,ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_head_window import HeadWindow,HEAD
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_stage_pipeline import StagePipeline
from experiments.free_search.owner_head_probe import wait_for_fetch
from experiments.free_search.public_pagination import inspect_page
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.owner_pilot import PublicPage,_page,_filters
from experiments.free_search.filter_gate import PublicEvidence,gate
from experiments.free_search.visible_adapter import parse_visible_facts
from experiments.free_search.owner_browser_handoff import next_ticket
from experiments.free_search.network_guard import CREDENTIAL_NAMES

def matches(record,category,filters):
    evidence=PublicEvidence(listing_id=record['listing_id'],publication_proven=False,active=True,
        category=category,price_usd=Decimal(str(record['price'])),region=record['region'],
        year=record['year'],brand=record['brand'],model=record['model'],mileage_km=record['mileage_km'],
        body=record['body'],fuel=record['fuel'],transmission=record['transmission'])
    return not [b for b in gate(evidence,_filters(filters)).blockers if b!='publication_not_proven']


def collect_details(pipeline,cards_by_id,searches,get,report,deadline,detail_limit,*,clock=time.time,priority=None,on_ticket=None):
    """A qualifying first car must not starve the remainder of the observed batch."""
    inspected=0
    report.setdefault('tickets',[])
    pending=pipeline.queue.pending(100,stage='discovered')
    if priority is not None:pending.sort(key=priority)
    for row in pending:
        preview=cards_by_id.get(row['id'])
        if preview is None or preview.preview_usd is None:continue
        if not any((_filters(s['filters']).price_usd.minimum or 0)<=preview.preview_usd<=
            (_filters(s['filters']).price_usd.maximum or Decimal('Infinity')) for s in searches):continue
        if inspected>=detail_limit or clock()>=deadline:
            report['detail_backlog']=True
            break
        inspected+=1
        html,receipt=get(row['url'],2500000)
        if html is None and receipt.get('status')==404:
            pipeline.queue.advance(row['id'],'discovered','rejected',clock(),{'reason':'listing_disappeared','receipt':receipt})
            report['details'].append({'id':row['id'],'reason':'listing_disappeared'})
            continue
        page=PublicPage(row['id'],row['url'],html,receipt['completed_at'])
        try:
            record=pipeline.detail(page,clock());record['listing_id']=row['id']
            category=parse_visible_facts(html,row['id']).category
            selected=[s['id'] for s in searches if matches(record,category,s['filters'])]
            report['details'].append({'id':row['id'],'url':row['url'],'record':record,'searches':selected})
            if selected:
                ticket=next_ticket(pipeline,clock(),deadline,listing_id=row['id'])
                if not ticket:raise ValueError('handoff_candidate_expired')
                work={'envelope':ticket,'search_ids':selected}
                report['tickets'].append(work)
                if on_ticket is not None:on_ticket(work)
                if 'ticket' not in report:report.update(ticket=ticket,search_id=selected[0])
                continue
            pipeline.queue.advance(row['id'],'detail','rejected',clock(),{'reason':'saved_filters_mismatch'})
        except ValueError as exc:
            current=pipeline.queue.get(row['id'])
            if current['stage'] in ('discovered','detail'):
                pipeline.queue.advance(row['id'],current['stage'],'rejected',clock(),{'reason':str(exc)})
            report['details'].append({'id':row['id'],'reason':str(exc)})

def run(*,pages=3,detail_limit=10):
    if type(pages) is not int or not 1<=pages<=6:raise ValueError('invalid_pages')
    if type(detail_limit) is not int or not 1<=detail_limit<=20:raise ValueError('invalid_detail_limit')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):raise Denied('production_configuration_present')
    saved=json.loads((WORKSPACE/'comparison-current-access-20261021.json').read_text(encoding='utf-8'))
    searches=[s for s in saved['searches'] if s['enabled']]
    if not searches:raise ValueError('no_saved_searches')
    report={'started_at':time.time(),'pages':[],'details':[],'receipts':[],'paid_calls':0,'telegram_calls':0,
            'coverage':'bounded public head, not whole market','publication_at':None,'send_authorized':False}
    with session(LOCK,300) as deadline:
        budget=Budget(WORKSPACE/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
        obs=Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
        queue=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3');pipeline=StagePipeline(queue,obs)
        window=HeadWindow(obs);fetch=PublicFetch(budget)
        def get(url,cap):
            budget.observe(url)
            wait_for_fetch(budget.db.execute('SELECT next_at FROM budget').fetchone()[0],deadline)
            body,receipt=fetch.get(url,cap);report['receipts'].append(receipt)
            if body is None:raise Denied('source_unavailable_no_retry')
            return body,receipt
        try:
            report['budget_before']=budget.summary();cards_by_id={}
            get(ROBOTS,20000)
            for _ in range(2):
                for p in range(1,pages+1):
                    url=HEAD+f'?page={p}';html,receipt=get(url,2500000)
                    cards=parse_public_cards(html);meta=inspect_page(html,url)
                    accepted=window.accept(meta,cards,receipt['completed_at'])
                    queued=pipeline.ingest(cards,receipt['completed_at'])
                    cards_by_id.update((c.listing_id,c) for c in cards)
                    report['pages'].append({'url':url,'count':len(cards),'queued':queued,'window':accepted,
                        'observed_at':receipt['completed_at'],'ids':[c.listing_id for c in cards]})
            pipeline.expire(time.time())
            collect_details(pipeline,cards_by_id,searches,get,report,deadline,detail_limit)
        except ValueError as exc:report['reason']=str(exc)
        finally:
            report['budget_after']=budget.summary();report['completed_at']=time.time()
            queue.close();obs.close();budget.close()
            with (WORKSPACE/'comparison-batches-20261021.jsonl').open('a',encoding='utf-8') as out:
                out.write(json.dumps(report,ensure_ascii=False,default=str)+'\n')
            (WORKSPACE/'comparison-batch-current.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return report

if __name__=='__main__':
    result=run()
    print(json.dumps({k:v for k,v in result.items() if k not in ('pages','receipts')},ensure_ascii=True,default=str))
