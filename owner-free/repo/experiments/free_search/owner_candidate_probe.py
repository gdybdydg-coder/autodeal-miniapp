"""Inspect fresh observed candidates; saved filters are research, not send authority."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import json,time,os,re
from decimal import Decimal
from dataclasses import asdict
from experiments.free_search.owner_live_scan import WORKSPACE,LOCK
from experiments.free_search.owner_night_runtime import Coordinator
from experiments.free_search.owner_budget import Budget,Denied
from experiments.free_search.owner_public_fetch import PublicFetch,ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_pilot import PublicPage,_page,_filters
from experiments.free_search.filter_gate import PublicEvidence,gate
from experiments.free_search.visible_adapter import parse_visible_facts
from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_stage_pipeline import StagePipeline

def has_detail_url(card):
    ident=card.get('listing_id');url=card.get('url')
    return (isinstance(ident,str) and re.fullmatch(r'[1-9][0-9]{0,11}',ident) is not None
            and isinstance(url,str) and re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                                     + ident+r'\.html',url) is not None)

def run(token, *, all_ukraine=False):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):raise Denied('production_configuration_present')
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(token):raise Denied('executor_lock_required')
    finally:lock.close()
    obs=Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
    budget=Budget(WORKSPACE/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
    fetch=PublicFetch(budget)
    queue=CandidateQueue(WORKSPACE/'night21-candidates.sqlite3');pipeline=StagePipeline(queue,obs)
    report=dict(candidates=[],receipts=[],paid_calls=0,telegram_calls=0,ready_for_delivery=False)
    try:
        saved=json.loads((WORKSPACE/'owner-search-snapshot-20261020.json').read_text(encoding='utf-8'))
        report['filter_snapshot_at']=saved['checked_at']
        if all_ukraine:
            # Explicit one-session user request; no saved search or production writes.
            saved['searches']=[{'search_id':'owner_session_all_ukraine','filters':{'minDiscount':10}}]
            report['search_scope']='owner_session_all_ukraine_any_price'
        sources=[WORKSPACE/'live-scan-20261020.jsonl',WORKSPACE/'head-window-20261020.jsonl']
        cards={c['listing_id']:c for source in sources if source.exists() for line in source.read_text(encoding='utf-8').splitlines()
               for p in json.loads(line)['pages'] for c in p['cards']}
        # Research may inspect a fresh observation from an incomplete scan;
        # it cannot authorize delivery or claim complete coverage.
        history_path=WORKSPACE/'fresh-candidate-history-20261020.jsonl'
        checked=set()
        if history_path.exists():
            for line in history_path.read_text(encoding='utf-8').splitlines():
                previous=json.loads(line)
                checked.update(c['listing_id'] for c in previous.get('candidates',[]))
        # Prefer unseen detail candidates; a fixed first-five slice starves later IDs.
        ordered=sorted(cards.items(),key=lambda item:item[0] in checked)
        report['cards_without_public_detail_url']=sum(not has_detail_url(c) for c in cards.values())
        selected=[c for ident,c in ordered if ident not in checked and has_detail_url(c)
                  and obs.signal(ident,time.time())['reason'] in ('eligible','scan_not_completed')
                  and c['preview_usd'] is not None and any(
                  (_filters(s['filters']).price_usd.minimum or 0)<=Decimal(c['preview_usd'])<=
                  (_filters(s['filters']).price_usd.maximum or Decimal('Infinity')) for s in saved['searches'])][:5]
        report['selected']=[c['listing_id'] for c in selected]
        def get(url,cap):
            budget.observe(url)
            time.sleep(max(0,budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())+.05)
            html,receipt=fetch.get(url,cap);report['receipts'].append(receipt)
            if html is None:raise Denied('source_unavailable_no_retry')
            return html,receipt
        if selected:get(ROBOTS,20000)
        for c in selected:
            ident=c['listing_id']
            signal=obs.signal(ident,time.time())
            if signal['reason'] not in ('eligible','scan_not_completed'):continue
            report.setdefault('observation_gates',{})[ident]=signal
            html,receipt=get(c['url'],2500000)
            try:
                page=PublicPage(ident,c['url'],html,receipt['completed_at'])
                row=_page(page,time.time(),300)
                queued=queue.get(ident)
                if queued and queued['stage']=='discovered':
                    pipeline.detail(page,time.time())
                visible=parse_visible_facts(html,ident)
                evidence=PublicEvidence(listing_id=ident,publication_proven=False,active=True,
                    category=visible.category,price_usd=Decimal(str(row['price'])),region=row['region'],
                    year=row['year'],brand=row['brand'],model=row['model'],mileage_km=row['mileage_km'],
                    body=row['body'],fuel=row['fuel'],transmission=row['transmission'])
                decisions=[dict(search_id=s['search_id'],**asdict(gate(evidence,_filters(s['filters'])))) for s in saved['searches']]
                for decision in decisions:
                    # Same first-observed policy as owner_pilot; never assert publication.
                    decision['first_observed_filter_blockers']=[b for b in decision['blockers'] if b!='publication_not_proven']
                    decision['matches_saved_filters']=not decision['first_observed_filter_blockers']
                report['candidates'].append(dict(listing_id=ident,url=c['url'],record=row,decisions=decisions))
            except ValueError as exc:report['candidates'].append(dict(listing_id=ident,reason=str(exc)))
    except ValueError as exc:report['reason']=str(exc)
    finally:
        report['budget']=budget.summary();queue.close();obs.close();budget.close()
        report['completed_at']=time.time()
        with (WORKSPACE/'fresh-candidate-history-20261020.jsonl').open('a',encoding='utf-8') as history:
            history.write(json.dumps(report,default=str,ensure_ascii=False)+'\n')
        (WORKSPACE/'fresh-candidate-probe-20261020.json').write_text(json.dumps(report,default=str,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

if __name__=='__main__':print(json.dumps(run(sys.argv[1],all_ukraine='--all-ukraine' in sys.argv[2:]),default=str,ensure_ascii=True))
