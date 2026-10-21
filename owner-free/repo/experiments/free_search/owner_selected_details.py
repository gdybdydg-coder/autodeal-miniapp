"""Bounded details for URLs already observed through the public browser UI."""
import json, sys, time, os
from .owner_budget import Budget, Denied, public_url
from .owner_public_fetch import PublicFetch, ROBOTS
from .owner_peer_cache import PeerCache
from .public_peer_pilot import public_peer_record
from .public_detail_crosscheck import crosscheck_details
from .owner_peer_pool import pool
from .network_guard import CREDENTIAL_NAMES

def run(ledger, cache_path, urls):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES): raise Denied('production_configuration_present')
    budget=Budget(ledger,13,30020000); cache=PeerCache(cache_path)
    receipts=[]
    try:
        fetch=PublicFetch(budget)
        for url in [ROBOTS,*urls]:
            public_url(url); budget.observe(url)
            delay=max(0,budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())
            if delay>60: raise Denied('pause_requires_later_invocation')
            time.sleep(delay)
            body,receipt=fetch.get(url,20000 if url==ROBOTS else 2500000)
            receipts.append(receipt)
            if body is None: raise Denied('source_unavailable')
            if url==ROBOTS: continue
            ident=url.rsplit('_',1)[1].split('.')[0]
            try:
                row=public_peer_record(body,ident,receipt['completed_at'])
                cross=crosscheck_details(body,ident)
                if cross['issues']: raise ValueError('public_detail_conflict')
                row['drive']=cross['drive_text']; cache.put(ident,receipt['completed_at'],row=row)
            except ValueError as exc:
                cache.put(ident,receipt['completed_at'],reason=str(exc))
        subject=next(r for r in cache.snapshot(time.time())['rows'] if r['listing_id']=='37251708')
        return dict(receipts=receipts,pool=pool(subject,cache,time.time()),budget=budget.summary())
    finally: budget.close(); cache.close()

if __name__=='__main__': print(json.dumps(run(sys.argv[1],sys.argv[2],sys.argv[3:])))
