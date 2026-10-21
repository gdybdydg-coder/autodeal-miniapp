"""Explicit resumed session: four known public pages maximum, old budget retained."""
import json
import os
from pathlib import Path
import sys
import time
if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.free_search.owner_budget import Budget, Denied
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_scope_links import inspect_links
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, LEDGER, WORKSPACE
from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.public_pagination import inspect_page
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.owner_preview import parse_previews
from dataclasses import asdict


def run(token, phase='discovery'):
    if phase not in ('discovery','canonical'):
        raise ValueError('invalid_phase')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock=Coordinator(LOCK)
    try:
        if not lock.owns(token): raise Denied('executor_lock_required')
    finally: lock.close()
    prefix='resumed-scope-20261020' if phase=='discovery' else 'resumed-canonical-20261020'
    urls=[ROBOTS,'https://auto.ria.com/uk/last/hour/',
          'https://auto.ria.com/uk/last/hour/?page=2',
          'https://auto.ria.com/uk/car/audi/a6/year/2015/']
    if phase=='canonical':
        prior=json.loads((WORKSPACE/'resumed-scope-20261020.json').read_text())
        observed={url for page in prior['pages'] for url in page['observed_catalogue_links']}
        first='https://auto.ria.com/uk/last/hour/?page=1'
        catalogue='https://auto.ria.com/uk/car/audi/a6/c7-4g-fl/year/2015/'
        if not {first,catalogue} <= observed:
            raise Denied('source_links_not_observed')
        urls=[ROBOTS,first,'https://auto.ria.com/uk/last/hour/?page=2',first,catalogue]
    with (WORKSPACE/(prefix+'.claim')).open('x') as handle:
        handle.write(str(time.time()))
    # User explicitly resumed after the night ended. Do not alter NightBudget
    # or its deadline; keep the SAME cumulative ledger and caps for this probe.
    budget=Budget(LEDGER,40,100000000)
    report={'session':'user_resumed_daytime','receipts':[],'pages':[],
            'paid_calls':0,'telegram_calls':0,'status':'started'}
    try:
        fetch=PublicFetch(budget)
        for url in urls:
            budget.observe(url)
            delay=budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time()
            if delay>60: raise Denied('pause_requires_later_invocation')
            time.sleep(max(0,delay))
            body,receipt=fetch.get(url,20000 if url==ROBOTS else 2500000)
            report['receipts'].append(receipt)
            if body is None: raise Denied('source_unavailable')
            if url!=ROBOTS:
                page=inspect_links(body,url)
                cards=parse_public_cards(body)
                page['cards']=[{'id':c.listing_id,'url':c.url,'issues':c.issues,
                                'promoted':c.promoted} for c in cards]
                if '/last/hour/' in url:
                    page['pagination']=asdict(inspect_page(body,url))
                else:
                    page['previews']=parse_previews(body)
                report['pages'].append(page)
        report['status']='observed_links_only'
    except ValueError as exc:
        report.update(status='stopped',reason=str(exc))
    finally:
        report['budget']=budget.summary();budget.close()
        with (WORKSPACE/(prefix+'.json')).open('x',encoding='utf-8') as handle:
            json.dump(report,handle,indent=2,default=str)
    return report


if __name__=='__main__':
    result=run(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else 'discovery')
    print(json.dumps({k:v for k,v in result.items() if k!='pages'},indent=2))
