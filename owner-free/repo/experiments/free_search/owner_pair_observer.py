"""Bounded, read-only owner receipt comparison; never asks either source API."""
import time
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from backend.models import Listing,Delivery,DeliveryTiming,MonitorMatch,MonitorSeen,Search
from .owner_comparison_delivery import deliveries
from .owner_pair_report import comparison


def observe_pairs(engine,owner,*,limit=5,clock=time.time):
    if type(owner) is not int or owner<=0:raise ValueError('verified_owner_required')
    if type(limit) is not int or not 1<=limit<=5:raise ValueError('bounded_pair_limit_required')
    result={'at':clock(),'pairs':[],'paid_api_calls':0,'phone_push_time_known':False,
            'publication_at':None,'clock_measurement':None}
    with Session(engine) as db:
        if engine.dialect.name=='postgresql':
            db.execute(text('SET TRANSACTION READ ONLY'))
            start=clock()
            server=float(db.execute(text('SELECT EXTRACT(EPOCH FROM clock_timestamp())')).scalar_one())
            end=clock()
            result['clock_measurement']={'server_epoch':server,'local_midpoint':(start+end)/2,
                'estimated_server_minus_local_seconds':server-(start+end)/2,'round_trip_seconds':end-start}
        free=db.execute(select(deliveries).where(deliveries.c.owner==owner,
            deliveries.c.state=='sent').order_by(deliveries.c.accepted_at.desc()).limit(limit)).mappings().all()
        for row in free:
            ident,search,epoch=row['source_id'],row['search_id'],row['epoch']
            seen=db.execute(select(MonitorSeen.search_id,MonitorSeen.epoch,MonitorSeen.state,MonitorSeen.first_seen)
                .join(Search,Search.id==MonitorSeen.search_id).where(Search.user_id==owner,
                MonitorSeen.source_id==ident,MonitorSeen.search_id==search,MonitorSeen.epoch==epoch)).mappings().all()
            paid=db.execute(select(Listing.source_id,Delivery.state,Delivery.message_id,DeliveryTiming.accepted_at,
                MonitorMatch.search_id,MonitorMatch.epoch).select_from(Listing)
                .join(Delivery,Delivery.listing_id==Listing.id)
                .join(DeliveryTiming,DeliveryTiming.delivery_id==Delivery.id)
                .join(MonitorMatch,(MonitorMatch.listing_id==Listing.id)&(MonitorMatch.search_id==search)&(MonitorMatch.epoch==epoch))
                .join(Search,Search.id==MonitorMatch.search_id)
                .where(Delivery.user_id==owner,Search.user_id==owner,Listing.source=='auto_ria',Listing.source_id==ident,
                       Delivery.state=='sent',Delivery.message_id.not_in((27366,28005,28006)))).mappings().all()
            result['pairs'].append(comparison({'at':result['at'],'free':[dict(row)],
                'paid_seen':[dict(v) for v in seen],'paid_deliveries':[dict(v) for v in paid]}))
    result['matched_pairs']=sum(p['paid_message_id'] is not None for p in result['pairs'])
    return result
