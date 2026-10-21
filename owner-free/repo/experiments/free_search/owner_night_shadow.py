"""One head observation under the shared night ledger. No delivery or valuation."""
import json
import os
from pathlib import Path
import sys
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, WORKSPACE, night_budget
from experiments.free_search.owner_budget import Denied
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.public_pagination import inspect_page, assess_scan

FEED = 'https://auto.ria.com/uk/last/hour/'


def run(token):
    if any(os.environ.get(name) for name in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock = Coordinator(LOCK)
    try:
        if not lock.owns(token):
            raise Denied('executor_lock_required')
    finally:
        lock.close()
    budget = night_budget()
    report = {'kind':'night_shadow_head_only', 'receipts':[], 'telegram_calls':0,
              'paid_source_calls':0, 'whole_market_recall':None, 'publication_at':None}
    try:
        fetch = PublicFetch(budget)
        for url, cap in ((ROBOTS, 20000), (FEED, 2500000)):
            budget.observe(url)
            delay = budget.db.execute('SELECT next_at FROM budget').fetchone()[0] - time.time()
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(max(0, delay))
            body, receipt = fetch.get(url, cap)
            report['receipts'].append(receipt)
            if body is None:
                report['status'] = 'source_unavailable'
                break
            if url == FEED:
                cards = parse_public_cards(body)
                observations = Observations(WORKSPACE / 'night-20261020.owner-observations.sqlite3')
                try:
                    report['intake'] = observations.ingest(cards, receipt['completed_at'])
                finally:
                    observations.close()
                report['ids'] = [card.listing_id for card in cards]
                report['pagination'] = assess_scan([inspect_page(body, FEED)])
                report['status'] = 'observed_head_only'
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['budget'] = budget.summary()
        budget.close()
    return report


if __name__ == '__main__':
    result = run(sys.argv[1])
    # Append each receipt; never overwrite another invocation's evidence.
    with (WORKSPACE / 'night-shadow-20261020.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=True) + '\n')
    print(json.dumps(result, ensure_ascii=True))
