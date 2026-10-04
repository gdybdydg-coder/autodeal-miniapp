"""Reproducible full offline pipeline and synthetic valuation holdout experiment."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import tracemalloc
from unittest.mock import patch
from .pipeline import Pipeline, canonical, estimate
from .test_pipeline import NOW, raw, comps, users
from .fx import FXQuote, instant, KYIV, nbu_url


def benchmark(n):
    with tempfile.TemporaryDirectory() as tmp, patch('socket.socket',side_effect=AssertionError('Network forbidden')):
        path=Path(tmp)/'bench.sqlite';p=Pipeline(path)
        # 42 is a deliberately fictional test rate, not an observed NBU quote.
        day=instant(NOW).astimezone(KYIV).date()
        quote=FXQuote(Decimal('42'),day,instant(NOW-100),nbu_url(day))
        inputs=[raw(i,price=(7000 if i%4 else 11000)*(42 if i%2 else 1),currency='UAH' if i%2 else 'USD') for i in range(240)]
        pages={str(i) if i else None:dict(items=inputs[i*20:(i+1)*20],next=str(i+1) if i<11 else None) for i in range(12)}
        tracemalloc.start();start=time.perf_counter()
        collected=p.collect(pages.__getitem__,NOW,page_budget=12,row_budget=240,fx_quote=quote);t1=time.perf_counter()
        queue=p.enqueue(users(n),comps(),NOW,capacity=50000,olx_enabled=True);t2=time.perf_counter()
        delivered=p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True);t3=time.perf_counter()
        peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop();p.db.close()
        p=Pipeline(path);replayed=p.enqueue(users(n),comps(),NOW,capacity=50000,olx_enabled=True)['queued'];p.db.close()
        return dict(users=n,input_cards=240,collect=collected,queue=queue,fake_delivery=delivered,restart_requeued=replayed,
                    seconds=dict(normalize_store=t1-start,valuation_filter_queue=t2-t1,fake_delivery=t3-t2,total=t3-start),
                    peak_python_bytes=peak,sqlite_bytes=path.stat().st_size,live_requests=0,paid_requests=0,
                    eligibility_reviewed=240,usd_native=120,uah_converted=120,fx_basis='fictional_test_rate_42_not_current_NBU')


def quality():
    # Ground truth is synthetic latent asking-market value, never verified sales.
    confusion={k:0 for k in ('tp','fp','tn','fn','unknown')};naive={k:0 for k in ('tp','fp','tn','fn')}
    for i in range(100):
        expected=i%2==0
        target=canonical(raw('holdout'+str(i),price=7000 if expected else 10500,generation=None if i%10==0 else 'VII'),NOW)
        result=estimate(target,comps(),NOW)
        if result['status']=='profitability_unconfirmed':confusion['unknown']+=1
        else:
            predicted=Decimal(result['discount_percent'])>=15
            confusion['tp' if predicted and expected else 'fp' if predicted else 'fn' if expected else 'tn']+=1
        # Deliberately confounded broad brand/year cohort includes expensive models.
        predicted=float(target['price']) <= 15000*.85
        naive['tp' if predicted and expected else 'fp' if predicted else 'fn' if expected else 'tn']+=1
    return dict(label_basis='synthetic latent asking-market scenarios',strict=confusion,brand_year_baseline=naive,
                unknown_positive=10,real_accuracy_measured=False,independent_real_control_size=0)

if __name__=='__main__':
    print(json.dumps(dict(kind='synthetic_only',quality=quality(),runs=[benchmark(100),benchmark(200)]),indent=2))
