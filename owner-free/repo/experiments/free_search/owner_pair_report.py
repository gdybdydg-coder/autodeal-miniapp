"""Pure reporting of owner-scoped readbacks. Missing source remains unknown."""
import math

def comparison(pair):
    rows=pair.get('free',[])
    if len(rows)!=1:raise ValueError('one_free_identity_required')
    free=rows[0];ident=free['source_id'];search=free['search_id'];epoch=free['epoch']
    if free['state']!='sent' or type(free.get('message_id')) is not int:raise ValueError('free_receipt_required')
    seen=[s for s in pair.get('paid_seen',[]) if s['search_id']==search and s['epoch']==epoch]
    paid=[s for s in pair.get('paid_deliveries',[]) if s['source_id']==ident and s['search_id']==search
          and s['epoch']==epoch and s['state']=='sent']
    if len(seen)>1 or len(paid)>1:raise ValueError('ambiguous_paid_identity')
    paid_seen=seen[0]['first_seen'] if seen else None
    paid_accepted=paid[0]['accepted_at'] if paid else None
    def delta(a,b):
        if a is None or b is None:return None
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)):raise ValueError('invalid_timing')
        return a-b
    return {'listing_id':ident,'search_id':search,'epoch':epoch,'checked_at':pair['at'],
        'free_message_id':free['message_id'],'paid_message_id':paid[0]['message_id'] if paid else None,
        'free_first_observed_to_accepted_seconds':delta(free['accepted_at'],free['first_seen']),
        'free_transport_seconds':delta(free['accepted_at'],free['send_started_at']),
        'free_minus_paid_first_seen_seconds':delta(free['first_seen'],paid_seen),
        'free_minus_paid_accepted_seconds':delta(free['accepted_at'],paid_accepted),
        'publication_at':None,'missing_paid_means':'not_recorded_for_same_search_epoch',
        'acceptance_is_phone_notification_time':False}
