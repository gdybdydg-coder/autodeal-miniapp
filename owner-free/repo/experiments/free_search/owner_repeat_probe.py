"""Fixed experiment: robots plus three head observations, at least 30s apart.

One immutable four-request ledger, no retries, no API or message transport.
Printed reports contain aggregate evidence only, never source HTML.
"""
from dataclasses import asdict
import json
import os
import sys
import time

from .network_guard import CREDENTIAL_NAMES
from .owner_budget import Budget, Denied
from .owner_observations import Observations
from .owner_public_fetch import PublicFetch, ROBOTS
from .owner_probe import FEED
from .public_cards import parse_public_cards
from .public_pagination import inspect_page, assess_scan


def run(ledger_path, observation_path, emit=lambda value: None):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    budget = Budget(ledger_path, max_gets=4, max_bytes=7520000)
    observations = None
    report = dict(paid_api_calls=0, telegram_calls=0, samples=[],
                  publication_latency=None, whole_market_recall=None)
    previous_ids = None
    previous_at = None
    try:
        observations = Observations(observation_path)
        fetch = PublicFetch(budget)
        for index, url in enumerate((ROBOTS, FEED, FEED, FEED)):
            budget.observe(url)
            next_at = budget.db.execute('SELECT next_at FROM budget').fetchone()[0]
            due = max(next_at, previous_at + 30 if previous_at is not None else 0)
            delay = max(0, due - time.time())
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(delay)
            body, receipt = fetch.get(url, 20000 if index == 0 else 2500000)
            if body is None:
                report.update(status='source_unavailable', stopped_receipt=receipt)
                break
            if index == 0:
                report['robots_receipt'] = receipt
                continue
            cards = parse_public_cards(body)
            metadata = inspect_page(body, FEED)
            now = receipt['completed_at']
            ids = {card.listing_id for card in cards}
            dated = [now - card.added_at for card in cards if card.added_at is not None]
            sample = dict(receipt=receipt, cards=len(cards),
                intake=observations.ingest(cards, now),
                pagination=asdict(metadata), coverage=assess_scan([metadata]),
                previous_overlap=None if previous_ids is None else len(ids & previous_ids),
                left_head=None if previous_ids is None else len(previous_ids - ids),
                entered_head=None if previous_ids is None else len(ids - previous_ids),
                interval_seconds=None if previous_at is None else now - previous_at,
                minimum_source_date_age_seconds=min(dated) if dated else None,
                maximum_source_date_age_seconds=max(dated) if dated else None,
                older_than_hour=sum(age > 3600 for age in dated),
                future_dates=sum(age < 0 for age in dated),
                promoted=sum(card.promoted for card in cards))
            report['samples'].append(sample)
            emit(sample)
            previous_ids, previous_at = ids, now
        else:
            report['status'] = 'bounded_observation_complete'
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['budget'] = budget.summary()
        budget.close()
        if observations is not None:
            observations.close()
    return report


if __name__ == '__main__':
    result = run(sys.argv[1], sys.argv[2],
                 emit=lambda sample: print(json.dumps({'sample': sample}), flush=True))
    print(json.dumps({'result': result}), flush=True)
