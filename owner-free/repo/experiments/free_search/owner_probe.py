"""One bounded public-source probe; never starts the application or sends messages.

Run from repository root: python -m experiments.free_search.owner_probe PATH
PATH is a fresh or resumable request ledger. A spent ledger cannot be reset here.
Only aggregate evidence is printed; HTML is kept in memory.
"""
import json
import os
import sys
import time
from collections import Counter

from .network_guard import CREDENTIAL_NAMES
from .owner_budget import Budget, Denied
from .owner_public_fetch import PublicFetch, ROBOTS
from .public_cards import parse_public_cards, CardParseError
from .public_pagination import inspect_page, assess_scan
from .owner_observations import Observations

FEED = 'https://auto.ria.com/uk/last/hour/'


def run(path, observation_path=None):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    budget = Budget(path, max_gets=2, max_bytes=2520000)
    report = dict(scope='local_bounded_probe', paid_api_calls=0,
                  telegram_calls=0, receipts=[], coverage='one_page_only',
                  publication_semantics_verified=False)
    try:
        fetch = PublicFetch(budget)
        for url, cap in ((ROBOTS, 20000), (FEED, 2500000)):
            budget.observe(url)
            next_at = budget.db.execute('SELECT next_at FROM budget').fetchone()[0]
            if next_at - time.time() > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(max(0, next_at - time.time()))
            body, evidence = fetch.get(url, cap)
            report['receipts'].append(evidence)
            if body is None:
                report['status'] = 'source_unavailable'
                break
            if url == FEED:
                try:
                    cards = parse_public_cards(body)
                    report['pagination'] = assess_scan([inspect_page(body, FEED)])
                    if observation_path is not None:
                        observations = Observations(observation_path)
                        try:
                            report['intake'] = observations.ingest(cards, evidence['completed_at'])
                        finally:
                            observations.close()
                    report.update(status='parsed', cards=len(cards),
                        explicit_add_dates=sum(c.added_at is not None for c in cards),
                        valid_urls=sum(c.url is not None for c in cards),
                        issues=dict(Counter(i for c in cards for i in c.issues)))
                except CardParseError as exc:
                    report.update(status='parser_unresolved', reason=str(exc))
    except Denied as exc:
        report.update(status='denied', reason=str(exc))
    finally:
        report['reserved_requests'], report['reserved_bytes'] = budget.db.execute(
            'SELECT count(*),coalesce(sum(cap),0) FROM requests').fetchone()
        budget.close()
    return report


if __name__ == '__main__':
    print(json.dumps(run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), indent=2))
