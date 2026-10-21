"""Opt-in owner comparison sender. Never writes paid Listing/Delivery/jobs.

Schema creation is explicit and separate; no production import or auto-migration.
Claim before transport survives crash. Unique owner/listing across search epochs.
"""
import time
from sqlalchemy import Table,Column,MetaData,BigInteger,String,Float,JSON,select,insert,update,func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .owner_access import current_searches
from .owner_delivery import _access
from .owner_pilot import prepare
from .owner_card import card
from backend.models import Delivery,DeliveryTiming

metadata=MetaData()
deliveries=Table('owner_free_comparison_deliveries',metadata,
    Column('owner',BigInteger,primary_key=True),Column('source_id',String(100),primary_key=True),
    Column('search_id',BigInteger),Column('epoch',String(64)),Column('state',String(24)),
    Column('first_seen',Float),Column('claimed_at',Float),Column('send_started_at',Float),Column('accepted_at',Float),
    Column('message_id',BigInteger),Column('evidence',JSON))
PRIOR_OWNER_SIGNALS=frozenset({'40459429','40587738'})


def send_comparison(engine,settings,pipeline,ident,search_id,transport,*,condition_review,
                    enabled=False,clock=time.time):
    if enabled is not True:return {'state':'disabled'}
    if ident in PRIOR_OWNER_SIGNALS:return {'state':'blocked','reason':'historical_owner_signal'}
    searches,reason=current_searches(engine,settings,clock())
    snapshot=next((s for s in searches if s.search_id==search_id),None)
    if reason!='eligible' or snapshot is None:
        return {'state':'blocked','reason':reason if reason!='eligible' else 'current_owner_search_missing'}
    owner=settings.admin_telegram_id
    key=(deliveries.c.owner==owner)&(deliveries.c.source_id==ident)
    def evidence():
        row=pipeline._current(ident,clock())
        if row['stage']!='qualified':raise ValueError('queue_not_qualified')
        page,_=pipeline._page(ident,clock())
        saved=pipeline.queue.evidence(ident,'range')
        if not saved:raise ValueError('range_evidence_missing')
        from .owner_public_mean import browser_quote
        if browser_quote(saved['capture'],page,clock())!=saved['source']:
            raise ValueError('range_evidence_changed')
        draft=prepare(engine,settings,snapshot,page,[],[],clock(),
                      browser_mean=saved['capture'],comparison=True)
        if draft['status']!='local_draft':raise ValueError(draft['reason'])
        return row,draft,card(page,draft['valuation'],condition_review,clock())
    try:row,draft,message=evidence()
    except ValueError as exc:return {'state':'blocked','reason':str(exc)}
    try:
        with Session(engine) as db,db.begin():
            reason=_access(db,settings,snapshot,clock)
            if reason!='eligible':return {'state':'blocked','reason':reason}
            if db.execute(select(deliveries.c.state).where(key)).first():
                return {'state':'blocked','reason':'existing_free_comparison'}
            paid_last=db.scalar(select(func.max(DeliveryTiming.send_started_at)).join(
                Delivery,Delivery.id==DeliveryTiming.delivery_id).where(Delivery.user_id==owner))
            free_last=db.scalar(select(func.max(deliveries.c.claimed_at)).where(deliveries.c.owner==owner))
            if any(value is not None and clock()<value+1.05 for value in (paid_last,free_last)):
                return {'state':'blocked','reason':'chat_interval'}
            db.execute(insert(deliveries).values(owner=owner,source_id=ident,
                search_id=search_id,epoch=snapshot.epoch,state='claimed',first_seen=row['first_seen'],
                claimed_at=clock(),evidence={'valuation':draft['valuation'],'review':condition_review}))
    except IntegrityError:return {'state':'blocked','reason':'concurrent_free_comparison'}
    with Session(engine) as db,db.begin():
        reason=_access(db,settings,snapshot,clock)
        claim=db.execute(select(deliveries).where(key).with_for_update()).mappings().one()
        if claim['state']!='claimed':return {'state':'blocked','reason':'comparison_claim_changed'}
        try:
            if reason!='eligible':raise ValueError(reason)
            row,draft,message=evidence()
        except ValueError as exc:
            db.execute(update(deliveries).where(key).values(state='cancelled'))
            return {'state':'cancelled','reason':str(exc)}
        db.execute(update(deliveries).where(key).values(send_started_at=clock()))
        try:response=transport(owner,message)
        except Exception:response=None
        result=response.get('result') if isinstance(response,dict) else None
        ok=(isinstance(response,dict) and response.get('ok') is True and isinstance(result,dict)
            and type(result.get('message_id')) is int and result['message_id']>0)
        state='sent' if ok else 'uncertain'
        accepted=clock() if ok else None
        message=result['message_id'] if ok else None
        db.execute(update(deliveries).where(key).values(state=state,accepted_at=accepted,message_id=message))
        return {'state':state,'source':'free','listing_id':ident,'first_seen':row['first_seen'],
                'accepted_at':accepted,'message_id':message,'publication_at':None,'paid_calls':0}


def finish_comparison_handoff(engine,settings,pipeline,envelope,search_id,transport,*,
                              condition_review,enabled=False,clock=time.time):
    """Fresh visible capture to separate owner receipt with current saved percentage."""
    if enabled is not True:return {'state':'disabled'}
    from .owner_browser_handoff import accept_capture
    searches,reason=current_searches(engine,settings,clock())
    snapshot=next((s for s in searches if s.search_id==search_id),None)
    if reason!='eligible' or snapshot is None:return {'state':'blocked','reason':'current_owner_search_missing'}
    try:
        result=accept_capture(pipeline,envelope,clock(),research_percent=snapshot.filters.get('minDiscount',15))
    except ValueError as exc:return {'state':'blocked','reason':str(exc)}
    if not result['rule']['eligible']:return {'state':'rejected','reason':'owner_discount_threshold','rule':result['rule']}
    receipt=send_comparison(engine,settings,pipeline,result['listing_id'],search_id,transport,
        condition_review=condition_review,enabled=True,clock=clock)
    receipt['timings']=result['timings']
    return receipt
