"""Bounded queue-driven detail dispatch; no browser automation or send authority."""
import time
from .owner_pilot import PublicPage


def dispatch(pipeline, fetch_page, *, limit=5, clock=time.time, deadline):
    if type(limit) is not int or not 1<=limit<=20: raise ValueError('invalid_dispatch_limit')
    report={'expired':[], 'details':[], 'rejected':[], 'send_authorized':False}
    # Remove old backlog before selecting new work. Each loop makes progress;
    # a fresh batch stops expiration, and stage filtering prevents browser waits
    # from starving detail work behind it.
    while clock()<deadline:
        expired=pipeline.expire(clock())
        report['expired'].extend(expired)
        if len(expired)<100: break
    for row in pipeline.queue.pending(limit,stage='discovered'):
        if clock()>=deadline:
            report['stop']='deadline';break
        try:
            pipeline._current(row['id'],clock())
        except ValueError as exc:
            pipeline.queue.advance(row['id'],'discovered','rejected',clock(),{'reason':str(exc)})
            report['rejected'].append({'id':row['id'],'reason':str(exc)})
            continue
        # Fetch exceptions stop this batch and persist a terminal outcome; never
        # blindly retry a denial or unknown transport result on the next run.
        try:
            page=fetch_page(row['id'],row['url'])
        except ValueError as exc:
            pipeline.queue.advance(row['id'],'discovered','rejected',clock(),{'reason':str(exc)})
            report['rejected'].append({'id':row['id'],'reason':str(exc)})
            report['stop']='source_unavailable_no_retry';break
        if not isinstance(page,PublicPage) or page.listing_id!=row['id'] or page.url!=row['url']:
            raise ValueError('dispatcher_fetch_identity_mismatch')
        try:
            if clock()>=deadline:
                report['stop']='deadline';break
            record=pipeline.detail(page,clock())
            report['details'].append({'id':row['id'],'url':row['url'],'record':record})
        except ValueError as exc:
            pipeline.queue.advance(row['id'],'discovered','rejected',clock(),{'reason':str(exc)})
            report['rejected'].append({'id':row['id'],'reason':str(exc)})
    return report
