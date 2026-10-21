"""Reverse owner receipt audit, never an input to discovery or valuation."""
import json
import time
from sqlalchemy import text


def classify(receipt, observation, candidate, event, free, since):
    ident=receipt['source_id']
    result=dict(receipt, observed_at=observation['first_seen'] if observation else None,
                free_message_id=free['message_id'] if free else None,
                free_accepted_at=free['accepted_at'] if free else None,
                within_current_collection=receipt['discovered_at'] is not None and receipt['discovered_at']>=since)
    if free and free['state']=='sent':
        result['outcome']='both_sent'
        result['free_minus_paid_seconds']=free['accepted_at']-receipt['accepted_at']
    elif not observation:result['outcome']='not_observed'
    elif observation['baseline']:result['outcome']='baseline'
    elif not candidate:result['outcome']='observed_not_queued'
    else:
        result['outcome']=candidate['stage']
        result['reason']=(event or {}).get('reason') or (event or {}).get('receipt',{}).get('reason')
    return result


def observe_paid_coverage(engine, owner, queue, observations, *, clock=time.time, limit=25):
    if type(owner) is not int or owner<=0 or type(limit) is not int or not 1<=limit<=50:
        raise ValueError('bounded_owner_audit_required')
    now=clock()
    since_row=queue.db.execute('SELECT started_at FROM intake_dynamic').fetchone()
    since=since_row[0] if since_row else now
    with engine.connect() as db:
        if engine.dialect.name=='postgresql':db.execute(text('SET TRANSACTION READ ONLY'))
        receipts=db.execute(text('''
          SELECT l.source_id,d.message_id,t.accepted_at,t.discovered_at
          FROM deliveries d JOIN listings l ON l.id=d.listing_id
          JOIN delivery_timings t ON t.delivery_id=d.id
          WHERE d.user_id=:owner AND d.state='sent' AND l.source='auto_ria'
          AND t.accepted_at>=:after AND d.message_id NOT IN (27366,28005,28006)
          ORDER BY t.accepted_at DESC LIMIT :limit
        '''),dict(owner=owner,after=now-86400,limit=limit)).mappings().all()
        frees=db.execute(text('''SELECT source_id,state,message_id,accepted_at
          FROM owner_free_comparison_deliveries WHERE owner=:owner
          ORDER BY claimed_at DESC LIMIT 100'''),dict(owner=owner)).mappings().all()
    free_by_id={r['source_id']:r for r in frees}
    rows=[]
    for receipt in receipts:
        ident=receipt['source_id']
        raw=observations.db.execute('SELECT first_seen,baseline FROM observations WHERE id=?',(ident,)).fetchone()
        observation=dict(zip(('first_seen','baseline'),raw)) if raw else None
        candidate=queue.get(ident)
        raw=queue.db.execute('SELECT evidence FROM stage_events WHERE candidate=? ORDER BY id DESC LIMIT 1',(ident,)).fetchone()
        event=json.loads(raw[0]) if raw else None
        rows.append(classify(dict(receipt),observation,candidate,event,free_by_id.get(ident),since))
    return {'at':now,'collection_started_at':since,'limit':limit,'rows':rows,'paid_api_calls':0,
            'measurement':'paid receipt -> independent free history; no ingestion from paid IDs',
            'whole_market_recall':None,
            'current_paid_sample':sum(r['within_current_collection'] for r in rows),
            'current_unobserved':sum(r['within_current_collection'] and r['outcome']=='not_observed' for r in rows)}
