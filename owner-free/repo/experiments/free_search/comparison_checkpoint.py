"""Persist verified readbacks and reconcile local queue; no external requests."""
from pathlib import Path
import sys,json,time,sqlite3
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_pair_report import comparison
from experiments.free_search.owner_session_runtime import session
from experiments.free_search.owner_night_runtime import LOCK,Coordinator
from experiments.free_search.owner_budget import Budget

root=ROOT.parent
receipt=json.loads((root/'comparison-telegram-receipt-20261021.json').read_text())
pair=json.loads((root/'comparison-live-pair-20261021.json').read_text())
assert receipt['state']==receipt['readback']['state']=='sent'
assert receipt['message_id']==receipt['readback']['message_id']==28057
assert receipt['listing_id']=='39082094'
with session(LOCK,60):
    queue=CandidateQueue(root/'night21-candidates.sqlite3')
    try:
        row=queue.get(receipt['listing_id']);assert row['first_seen']==receipt['first_seen']
        if row['stage']=='qualified':
            queue.advance(row['id'],'qualified','claimed',receipt['readback']['send_started_at'],{'receipt_readback':receipt['readback']})
            queue.advance(row['id'],'claimed','sent',receipt['accepted_at'],{'receipt_readback':receipt['readback'],'source':'free_comparison'})
        events=queue.db.execute('SELECT stage,at FROM stage_events WHERE candidate=? ORDER BY id',(row['id'],)).fetchall()
        detail=queue.evidence(row['id'],'detail')['page']['received_at']
        capture=queue.evidence(row['id'],'range')['capture']['observed_at']
        qualified=next(at for stage,at in events if stage=='qualified')
        intervals={'first_seen_to_detail':detail-row['first_seen'],'detail_to_browser_capture':capture-detail,
            'capture_to_qualified':qualified-capture,'qualified_to_accepted':receipt['accepted_at']-qualified,
            'first_seen_to_accepted':receipt['accepted_at']-row['first_seen'],
            'telegram_transport':receipt['accepted_at']-receipt['readback']['send_started_at']}
    finally:queue.close()
budget=Budget(root/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
summary=budget.summary();budget.close()
report={'verified_at':time.time(),'branch':'audit/free-owner-20261019','git_state':'uncommitted',
 'status':'owner_comparison_one_shot_sent_not_continuous','listing_id':'39082094',
 'price_usd':6500,'lower_usd':8017,'saved_percent':10,'maximum_price_usd':'6854.535',
 'formula':'lower * 0.95 * (1 - saved_percent/100)','telegram':receipt,
 'comparison':comparison(pair),'latencies_seconds':{k:{'n':1,'p50':v,'p95':v,'max':v} for k,v in intervals.items()},
 'publication_to_discovery':{'n':0,'p50':None,'p95':None,'max':None},
 'offline_tests':{'count':237,'seconds':26.279,'passed':True,'network_calls':0},
 'postgres_tests':json.loads((root/'comparison-postgres-20261021.json').read_text()),
 'public_ledger':summary,'public_gets_this_session':summary['reserved_gets']-360,
 'browser':{'navigations':1,'range_clicks':1,'all_http_requests':None},'paid_calls':0,
 'production_changes':['additive owner_free_comparison_deliveries table','one owner-only free receipt'],
 'unchanged':['deployed application files','worker configuration','paid Delivery/Listing/jobs','subscriptions','other clients'],
 'errors_fixed':['invalid queue batch size before detail selection','Decimal report serialization; source receipts preserved in Budget'],
 'coverage':'three public head pages twice per batch; whole-market completeness unknown',
 'continuous_runtime':'not installed; current CUA browser valuation depends on active agent session',
 'next_action':'implement and validate persistent browser valuation runtime and delivery bridge; then pair same-ID paid receipts without forcing paid queries'}
(root/'COMPARISON_RESULT_20261021.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
with (root/'comparison-browser-actions-20261021.jsonl').open('a',encoding='utf-8') as f:
    f.write(json.dumps({'at':capture,'listing_id':'39082094',**report['browser'],'paid_calls':0})+'\n')
print(json.dumps({'result':report['status'],'budget':summary,'latency':intervals,'pair':report['comparison']}))
