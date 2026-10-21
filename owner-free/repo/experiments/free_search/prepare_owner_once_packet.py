"""Prepare an in-memory, owner-only execution packet. Does not send anything."""
import sys,pathlib,json,time,base64,zlib,sqlite3
ROOT=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from experiments.free_search.owner_delivery import send_one
from experiments.free_search.owner_pilot import PublicPage,_page
from experiments.free_search.owner_public_mean import browser_quote
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_budget import Budget
from experiments.free_search.owner_public_fetch import PublicFetch,ROBOTS
from experiments.free_search.owner_night_runtime import Coordinator,LOCK

def main():
    root=ROOT.parent;lock=Coordinator(LOCK)
    assert lock.owns(sys.argv[1]);lock.close()
    obs=Observations(root/'night-20261020.owner-observations.sqlite3')
    ident=sys.argv[2];assert obs.signal(ident,time.time(),max_age=900)['reason']=='eligible'
    capture=json.loads((root/sys.argv[3]).read_text(encoding='utf-8'))
    budget=Budget(root/'free-public-audit-20261020.sqlite3',2**63-1,2**63-1,allow_feed_pagination=True)
    fetch=PublicFetch(budget);receipts=[]
    for url in (ROBOTS,capture['listing_url']):
        budget.observe(url);time.sleep(max(0,budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())+.05)
        html,receipt=fetch.get(url,20000 if url==ROBOTS else 2500000);receipts.append(receipt);assert html is not None
    page=PublicPage(ident,url,html,receipt['completed_at']);row=_page(page,time.time(),300);mean=browser_quote(capture,page,time.time())
    from experiments.free_search.owner_price_rule import evaluate
    rule=evaluate(mean['lower_usd'],row['price'],sys.argv[4]);assert rule['eligible']
    modules={name:pathlib.Path(m.__file__).read_text(encoding='utf-8') for name,m in list(sys.modules.items()) if name.startswith('experiments.free_search.') and getattr(m,'__file__',None) and m.__file__.endswith('.py')}
    packet=dict(modules=modules,subject=page.__dict__,capture=capture,observations=base64.b64encode(obs.db.serialize()).decode())
    (root/'owner-lower-packet.b64').write_text(base64.b64encode(zlib.compress(json.dumps(packet).encode())).decode())
    (root/'lower-final-check-20261021.json').write_text(json.dumps(dict(record=row,valuation={**mean,**rule},signal=obs.signal(ident,time.time()),receipts=receipts,budget=budget.summary()),default=str,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'prepared':True,'discount_percent':rule['discount_percent'],'age':time.time()-obs.signal(ident,time.time())['first_seen']}));obs.close();budget.close()
if __name__=='__main__':main()
