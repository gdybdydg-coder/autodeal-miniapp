"""Two explicit one-shot phases, >=60s apart, shared night budget, no delivery."""
import json
import os
from pathlib import Path
import sys
import time
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, WORKSPACE, night_budget
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_budget import Denied
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.owner_control import freeze, compare, summary

CATALOGUE = 'https://auto.ria.com/uk/car/audi/a6/year/2015/'
FEED = 'https://auto.ria.com/uk/last/hour/'


def run(token, phase):
    if phase not in (0,1):
        raise ValueError('invalid_phase')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock = Coordinator(LOCK)
    try:
        if not lock.owns(token):
            raise Denied('executor_lock_required')
    finally:
        lock.close()
    prior = None
    if phase == 1:
        prior = json.loads((WORKSPACE/'night-control-0.json').read_text(encoding='utf-8'))
        if prior['status'] != 'complete':
            raise Denied('control_initial_failed')
        if time.time() - prior['catalogue_completed_at'] < 60:
            raise Denied('control_window_too_short')
    # Crash/restart never silently reselects the control or consumes another batch.
    with (WORKSPACE/f'night-control-{phase}.claim').open('x', encoding='utf-8') as claim:
        claim.write(str(time.time()))
    budget = night_budget()
    report = {'phase':phase, 'status':'started', 'receipts':[], 'paid_api_calls':0,
              'telegram_calls':0, 'publication_to_telegram':summary([]),
              'scope':'Audi_A6_2015_subset_of_planned_2012_2016'}
    try:
        fetch = PublicFetch(budget)
        def get(url):
            budget.observe(url)
            delay = budget.db.execute('SELECT next_at FROM budget').fetchone()[0] - time.time()
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(max(0, delay))
            body, receipt = fetch.get(url, 20000 if url == ROBOTS else 2500000)
            report['receipts'].append(receipt)
            if body is None:
                raise Denied('source_unavailable')
            return body, receipt
        get(ROBOTS)
        body, receipt = get(CATALOGUE)
        catalogue = parse_public_cards(body)
        report['catalogue_completed_at'] = receipt['completed_at']
        report['catalogue_ids'] = [card.listing_id for card in catalogue]
        report['control'] = freeze(catalogue) if prior is None else prior['control']
        # Persist the independent cohort BEFORE reading the prototype's feed.
        if phase == 0:
            with (WORKSPACE/'night-control-frozen.json').open('x', encoding='utf-8') as handle:
                json.dump(report['control'], handle, indent=2)
        body, receipt = get(FEED)
        feed = parse_public_cards(body)
        report['feed_ids'] = [card.listing_id for card in feed]
        report['comparison'] = compare(report['control']['cohort'], report['catalogue_ids'], report['feed_ids'])
        report['window_seconds'] = None if prior is None else report['catalogue_completed_at']-prior['catalogue_completed_at']
        report['status'] = 'complete'
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['budget'] = budget.summary()
        report['http_seconds'] = summary(r['completed_at']-r['started_at'] for r in report['receipts'])
        budget.close()
        (WORKSPACE/f'night-control-{phase}.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    result = run(sys.argv[1], int(sys.argv[2]))
    print(json.dumps({k:v for k,v in result.items() if k not in ('control','catalogue_ids','feed_ids')},indent=2))
