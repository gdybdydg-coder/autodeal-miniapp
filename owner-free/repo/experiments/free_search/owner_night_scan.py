"""Resume a bounded public scan under the shared night budget. Never sends."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_budget import Denied
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, WORKSPACE, night_budget
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_scan import ScanCursor, HEAD
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.public_pagination import inspect_page


def run(token, max_pages=2, *, budget_factory=None, head_url=HEAD,
        recover_moving=False):
    if type(max_pages) is not int or not 1 <= max_pages <= 3:
        raise ValueError('invalid_page_limit')
    if type(recover_moving) is not bool or head_url not in (HEAD,HEAD+'?page=1'):
        raise ValueError('invalid_scan_options')
    if any(os.environ.get(name) for name in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock = Coordinator(LOCK)
    try:
        if not lock.owns(token):
            raise Denied('executor_lock_required')
    finally:
        lock.close()
    budget = night_budget() if budget_factory is None else budget_factory()
    observations = Observations(WORKSPACE / 'night-20261020.owner-observations.sqlite3')
    scan = ScanCursor(observations)
    report = {'kind':'bounded_night_scan', 'receipts':[], 'telegram_calls':0,
              'paid_source_calls':0, 'whole_market_recall':None, 'publication_at':None,
              'atomic_source_snapshot_proven':False}
    try:
        state = scan.status()
        if state is None or state['phase'] in ('complete', 'abandoned'):
            state = scan.start(time.time(),head_url=head_url)
        if (state['phase'] == 'blocked' and recover_moving and
                state['reason'] == 'head_changed_recovery_required'):
            previous_id = state['id']
            state = scan.recover_moving_head(time.time())
            report['recovery'] = {'parent_id':previous_id,'child_id':state['id']}
        if state['phase'] == 'blocked':
            report['status'] = 'persisted_scan_block_no_retry'
            return report
        fetch = PublicFetch(budget)

        def get(url, cap):
            budget.observe(url)
            delay = budget.db.execute('SELECT next_at FROM budget').fetchone()[0] - time.time()
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(max(0, delay))
            body, receipt = fetch.get(url, cap)
            report['receipts'].append(receipt)
            return body, receipt

        body, _ = get(ROBOTS, 20000)
        if body is None:
            report['status'] = 'robots_unavailable'
            return report
        for _ in range(max_pages):
            url = scan.status()['expected']
            body, receipt = get(url, 2500000)
            if body is None:
                report['status'] = 'source_unavailable_cursor_retained'
                break
            cards = parse_public_cards(body)
            state = scan.accept(inspect_page(body, url), cards, receipt['completed_at'])
            report.setdefault('pages', []).append({'url':url, 'ids':[c.listing_id for c in cards],
                                                   'phase':state['phase'], 'reason':state['reason']})
            if state['phase'] in ('blocked', 'complete'):
                report['status'] = state['phase']
                break
        else:
            report['status'] = 'bounded_pause_cursor_retained'
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['scan'] = scan.status()
        report['budget'] = budget.summary()
        observations.close()
        budget.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('token')
    parser.add_argument('--max-pages', type=int, default=2)
    args = parser.parse_args()
    result = run(args.token, args.max_pages)
    with (WORKSPACE / 'night-scan-20261020.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=True) + '\n')
    print(json.dumps(result, ensure_ascii=True))
