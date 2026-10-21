"""One-shot frozen control details, shared night ledger, no delivery."""
import json
import os
from pathlib import Path
import re
import sys
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from experiments.free_search.network_guard import CREDENTIAL_NAMES
from experiments.free_search.owner_night_runtime import Coordinator, LOCK, WORKSPACE, night_budget
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS
from experiments.free_search.owner_budget import Denied
from experiments.free_search.owner_peer_cache import PeerCache
from experiments.free_search.owner_peer_pool import pool
from experiments.free_search.owner_pilot import PublicPage, _page
from experiments.free_search.owner_valuation_policy import OWNER_VALUATION_POLICY

SUBJECT = '40578077'
SUBJECT_URL = 'https://auto.ria.com/uk/auto_audi_a6_40578077.html'
OUTPUT = WORKSPACE / 'night-control-details.json'


def validate_cohort(document):
    rows = document['cohort']
    if len(rows) != 10 or len({r['id'] for r in rows}) != 10:
        raise ValueError('invalid_control_size')
    for row in rows:
        ident = row['id']
        if not re.fullmatch(r'[1-9][0-9]{0,11}', ident) or ident == SUBJECT:
            raise ValueError('invalid_control_id')
        if row['url'] != f'https://auto.ria.com/uk/auto_audi_a6_{ident}.html':
            raise ValueError('invalid_control_url')
    return rows


def run(token):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    lock = Coordinator(LOCK)
    try:
        if not lock.owns(token):
            raise Denied('executor_lock_required')
    finally:
        lock.close()
    cohort = validate_cohort(json.loads(
        (WORKSPACE/'night-control-frozen.json').read_text(encoding='utf-8')))
    # Claim before opening mutable resources; a repeated invocation does no work.
    with (WORKSPACE/'night-control-details.claim').open('x') as claim:
        claim.write(str(time.time()))
    budget = night_budget()
    cache_path = WORKSPACE/'verified.owner-peers.sqlite3'
    cache = PeerCache(cache_path)
    report = dict(status='started', subject=None, cohort=[
        {**r, 'status':'not_attempted', 'row':None} for r in cohort],
        receipts=[], publication_at=None, paid_api_calls=0, telegram_calls=0,
        ready_for_delivery=False, cache_hits=0,
        selection='all_frozen_control_ids_including_outside_owner_price',
        cache_path=cache_path.name)
    try:
        fetch = PublicFetch(budget)

        def get(url):
            budget.observe(url)
            delay = budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time()
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(max(0, delay))
            body, receipt = fetch.get(url, 20000 if url == ROBOTS else 2500000)
            report['receipts'].append(receipt)
            return body, receipt

        def record(ident, url):
            body, receipt = get(url)
            if body is None:
                cache.put(ident, receipt['completed_at'], reason='source_unavailable')
                raise Denied('source_unavailable')
            try:
                row = _page(PublicPage(ident, url, body, receipt['completed_at']),
                            time.time(), OWNER_VALUATION_POLICY.max_subject_price_age_seconds)
            except ValueError as exc:
                cache.put(ident, receipt['completed_at'], reason=str(exc))
                raise
            cache.put(ident, receipt['completed_at'], row=row)
            return row

        body, _ = get(ROBOTS)
        if body is None:
            raise Denied('robots_unavailable')
        report['subject'] = record(SUBJECT, SUBJECT_URL)
        for outcome in report['cohort']:
            try:
                outcome['row'] = record(outcome['id'], outcome['url'])
                outcome['status'] = 'verified_public_detail'
            except Denied as exc:
                outcome.update(status='source_stopped', reason=str(exc))
                raise
            except ValueError as exc:
                outcome.update(status='parse_rejected', reason=str(exc))
        report['accumulated_pool'] = pool(report['subject'], cache, time.time())
        accepted = {r['listing_id'] for r in report['accumulated_pool']['rows']}
        rejected = report['accumulated_pool']['rejected']
        for outcome in report['cohort']:
            outcome['compatible'] = outcome['id'] in accepted
            outcome['valuation_rejection'] = rejected.get(outcome['id'])
        report['status'] = 'comparison_complete'
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['budget'] = budget.summary()
        report['completed_at'] = time.time()
        budget.close()
        cache.close()
        # Exclusive report creation also prevents overwriting prior evidence.
        with OUTPUT.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, default=str)
    return report


if __name__ == '__main__':
    result = run(sys.argv[1])
    print(json.dumps({k:v for k,v in result.items() if k not in ('cohort','subject','accumulated_pool')},
                     indent=2, default=str))
