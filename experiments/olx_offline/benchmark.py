"""Finite synthetic benchmark, no network or production config."""
import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from prototype import Store, FixtureTransport


def run(users):
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'fixture.sqlite';store=Store(path)
        store.collect(FixtureTransport({None:{'items':[],'next':None}}),1000)
        pages={}
        for p in range(20):
            pages[None if p==0 else str(p)]={'items':[{'id':str(p*25+i),'title':'Synthetic car',
                'category':'whole_passenger_car','price':5000,'currency':'USD','price_kind':'full',
                'published_at':1001,'publication_verified':True} for i in range(25)],
                'next':str(p+1) if p<19 else None}
        transport=FixtureTransport(pages);tracemalloc.start();start=time.perf_counter()
        store.collect(transport,1002)
        result=store.fanout([(i,{'currency':'USD','price_max':7000},True) for i in range(users)])
        result.update(seconds=round(time.perf_counter()-start,4),python_peak_bytes=tracemalloc.get_traced_memory()[1],
                      sqlite_bytes=path.stat().st_size,shared_fixture_pages=len(transport.calls),olx_requests=0,telegram_requests=0)
        tracemalloc.stop();store.close();return result

if __name__=='__main__':print(json.dumps([run(100),run(200)],indent=2))
