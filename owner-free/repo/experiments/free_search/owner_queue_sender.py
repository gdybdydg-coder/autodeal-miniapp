"""Explicit one-shot bridge from local evidence to the existing owner sender.

No scheduler or credentials. Local research percentage/qualification is never
authorization: fetch current owner search and recompute in send_one. A local
claim survives crashes; ambiguous outcomes cannot be replayed automatically.
"""
import time
from .owner_access import current_searches
from .owner_public_mean import browser_quote
from .owner_delivery import send_one
from .owner_risk_review import review_text


def send_queued(engine,settings,pipeline,ident,search_id,transport,*,enabled=False,
                session_all_ukraine=False,clock=time.time,condition_review=None):
    if enabled is not True:return {'state':'disabled'}
    queue=pipeline.queue
    row=queue.get(ident)
    if row is None or row['stage']!='qualified':
        return {'state':'blocked','reason':'queue_not_qualified_or_already_claimed'}
    try:
        pipeline._current(ident,clock())
        page,_=pipeline._page(ident,clock())
        evidence=queue.evidence(ident,'range')
        if not evidence:raise ValueError('range_evidence_missing')
        source=browser_quote(evidence['capture'],page,clock())
        if source!=evidence['source']:raise ValueError('range_evidence_changed')
        disclosure=review_text(condition_review,page,clock())
    except ValueError as exc:
        return {'state':'blocked','reason':str(exc)}
    searches,reason=current_searches(engine,settings,clock())
    if reason!='eligible':return {'state':'blocked','reason':reason}
    snapshot=next((s for s in searches if s.search_id==search_id),None)
    if snapshot is None:return {'state':'blocked','reason':'current_owner_search_missing'}
    try:
        queue.advance(ident,'qualified','claimed',clock(),
                      {'search_id':snapshot.search_id,'epoch':snapshot.epoch,
                       'fingerprint':snapshot.fingerprint,'capture_sha256':source['capture_sha256'],
                       'condition_review':condition_review})
    except ValueError:
        return {'state':'blocked','reason':'concurrent_local_claim'}
    try:
        def reviewed_transport(uid,text):
            # Re-check freshness after any DB lock wait, before actual transport.
            current=review_text(condition_review,page,clock())
            if current!=disclosure:raise ValueError('condition_review_changed')
            return transport(uid,text+current)
        result=send_one(engine,settings,snapshot,page,[],[],pipeline.observations,reviewed_transport,
                        enabled=True,clock=clock,browser_mean=evidence['capture'],
                        session_all_ukraine=session_all_ukraine)
    except BaseException:
        # Do not store raw exception strings: they could contain service details.
        queue.advance(ident,'claimed','uncertain',clock(),{'reason':'sender_interrupted_no_retry'})
        raise
    terminal={'sent':'sent','free_uncertain':'uncertain','free_cancelled':'cancelled',
              'blocked':'cancelled'}.get(result.get('state'),'uncertain')
    queue.advance(ident,'claimed',terminal,clock(),{'sender_result':result})
    return result
