"""Bounded queue-to-CUA contract. No browser, credentials or sender here.

The ticket binds the visible capture to immutable discovery and detail evidence.
It is not an authorization token. Current owner gates still belong to the sender.
"""
import hashlib
import json
import math


def digest(ticket):
    return hashlib.sha256(json.dumps(ticket,sort_keys=True,separators=(',',':'),
                                     allow_nan=False).encode()).hexdigest()


def next_ticket(pipeline,now,deadline,*,listing_id=None):
    if not isinstance(deadline,(int,float)) or not math.isfinite(deadline):
        raise ValueError('invalid_handoff_deadline')
    if now>=deadline:return None
    # Only detail candidates: existing range evidence is not browsed again.
    rows=pipeline.queue.pending(100,stage='detail') if listing_id is None else [pipeline.queue.get(listing_id)]
    for row in rows:
        if row is None or row['stage']!='detail':continue
        try:
            pipeline._current(row['id'],now)
            page,_=pipeline._page(row['id'],now)
        except ValueError as exc:
            pipeline.queue.advance(row['id'],'detail','rejected',now,{'reason':str(exc)})
            continue
        detail=pipeline.queue.evidence(row['id'],'detail')
        ticket={'version':1,'listing_id':row['id'],'listing_url':row['url'],
                'first_seen':row['first_seen'],'detail_sha256':detail['html_sha256'],
                'issued_at':now,'expires_at':min(deadline,row['first_seen']+300,page.received_at+300)}
        if ticket['expires_at']<=now:continue
        return {'ticket':ticket,'ticket_sha256':digest(ticket)}
    return None


def accept_capture(pipeline,envelope,now,*,research_percent):
    ticket=envelope.get('ticket',{})
    if envelope.get('ticket_sha256')!=digest(ticket):raise ValueError('handoff_digest_mismatch')
    if ticket.get('version')!=1:raise ValueError('handoff_version')
    issued,expires=ticket.get('issued_at'),ticket.get('expires_at')
    if (type(issued) not in (int,float) or type(expires) not in (int,float)
            or not math.isfinite(issued) or not math.isfinite(expires)
            or not issued<=now<expires):raise ValueError('handoff_expired_or_future')
    ident=ticket.get('listing_id')
    row=pipeline._current(ident,now)
    page,_=pipeline._page(ident,now)
    detail=pipeline.queue.evidence(ident,'detail')
    if (ticket.get('listing_url')!=row['url'] or ticket.get('first_seen')!=row['first_seen']
            or ticket.get('detail_sha256')!=detail['html_sha256']):
        raise ValueError('handoff_evidence_changed')
    capture=envelope.get('capture',{})
    observed=capture.get('observed_at')
    if type(observed) not in (int,float) or not issued<=observed<=now:
        raise ValueError('capture_outside_handoff')
    from experiments.free_search.owner_handoff_timings import handoff_timings
    timings=handoff_timings(row['first_seen'],page.received_at,observed,now)
    if row['stage']=='detail':pipeline.capture(ident,capture,now)
    elif row['stage']=='range':
        if pipeline.queue.evidence(ident,'range')['capture']!=capture:
            raise ValueError('handoff_restart_conflict')
    else:raise ValueError('handoff_already_consumed')
    rule=pipeline.qualify(ident,research_percent,now)
    return {'listing_id':ident,'rule':rule,'send_authorized':False,'timings':timings}


def finish_owner_handoff(engine,settings,pipeline,envelope,search_id,transport,*,
                         condition_review,enabled=False,session_all_ukraine=False,clock=None):
    """Explicit one-shot completion; current saved percentage, no cached owner data.

    A consumed capture cannot be replayed here. A qualified restart can use the
    separate send_queued bridge, which revalidates evidence/access and dedup.
    """
    import time
    from .owner_access import current_searches
    from .owner_queue_sender import send_queued
    from .owner_risk_review import review_text
    if enabled is not True:return {'state':'disabled'}
    clock=clock or time.time
    searches,reason=current_searches(engine,settings,clock())
    if reason!='eligible':return {'state':'blocked','reason':reason}
    snapshot=next((s for s in searches if s.search_id==search_id),None)
    if snapshot is None:return {'state':'blocked','reason':'current_owner_search_missing'}
    ident=envelope.get('ticket',{}).get('listing_id')
    try:
        page,_=pipeline._page(ident,clock())
        review_text(condition_review,page,clock())
        result=accept_capture(pipeline,envelope,clock(),
                              research_percent=snapshot.filters.get('minDiscount',15))
    except ValueError as exc:
        return {'state':'blocked','reason':str(exc)}
    if not result['rule']['eligible']:
        return {'state':'blocked','reason':'owner_discount_threshold','rule':result['rule']}
    return send_queued(engine,settings,pipeline,ident,search_id,transport,enabled=True,
                       session_all_ukraine=session_all_ukraine,clock=clock,
                       condition_review=condition_review)


def main():
    import argparse
    from pathlib import Path
    import sys
    import time
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    from experiments.free_search.owner_night_runtime import Coordinator,LOCK,RENEWED_DEADLINE
    from experiments.free_search.owner_candidate_queue import CandidateQueue
    from experiments.free_search.owner_observations import Observations
    from experiments.free_search.owner_stage_pipeline import StagePipeline
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['next','accept'])
    parser.add_argument('token')
    parser.add_argument('file',help='Output handoff for next; input CUA envelope for accept')
    parser.add_argument('--research-percent',default=None)
    args=parser.parse_args()
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(args.token):raise ValueError('executor_lock_required')
        if time.time()>=RENEWED_DEADLINE:raise ValueError('night_ended')
        queue=CandidateQueue(root.parent/'night21-candidates.sqlite3')
        obs=Observations(root.parent/'night-20261020.owner-observations.sqlite3')
        try:
            pipeline=StagePipeline(queue,obs)
            if args.action=='next':
                result=next_ticket(pipeline,time.time(),RENEWED_DEADLINE)
                Path(args.file).write_text(json.dumps(result,indent=2),encoding='utf-8')
            else:
                if args.research_percent is None:raise ValueError('research_percent_required')
                envelope=json.loads(Path(args.file).read_text(encoding='utf-8'))
                result=accept_capture(pipeline,envelope,time.time(),research_percent=args.research_percent)
            print(json.dumps(result))
        finally:obs.close();queue.close()
    finally:lock.close()


if __name__=='__main__':main()
