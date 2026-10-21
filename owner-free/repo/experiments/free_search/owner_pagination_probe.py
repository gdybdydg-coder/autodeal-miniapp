"""Fixed robots/head/observed-page2/head/detail experiment; no sender."""
from dataclasses import asdict
import json
import os
import sys
import time

from .network_guard import CREDENTIAL_NAMES
from .owner_budget import Budget, Denied, public_url
from .owner_observations import Observations
from .owner_public_fetch import PublicFetch, ROBOTS
from .owner_probe import FEED
from .public_cards import parse_public_cards
from .public_pagination import inspect_page, assess_scan, nominal_windows
from .public_peer_pilot import public_peer_record
from .visible_adapter import parse_visible_facts
from .public_detail_crosscheck import crosscheck_details


def next_page(metadata):
    url = metadata.next_url
    if metadata.issues or url != FEED + '?page=2':
        raise Denied('observed_page_two_unverified')
    public_url(url)
    return url


def compare(pages, card_sets):
    a, b, c = (set(card.listing_id for card in cards) for cards in card_sets)
    # Measured lower bound only: departing IDs not found on the fetched next
    # page might have been removed or moved deeper; these are NOT proven losses.
    unaccounted = (a-c)-b
    return dict(scan=assess_scan(pages[:2]), nominal_windows=nominal_windows(pages[:2]),
                first_head_page_two_overlap=len(a & b),
                initial_union=len(a | b), head_entered=len(c-a), head_left=len(a-c),
                left_head_seen_on_page_two=len((a-c) & b),
                departed_ids_not_reobserved=sorted(unaccounted),
                recovery_needed=bool(unaccounted or pages[0].scope_digest != pages[1].scope_digest),
                repeated_head_scope_equal=pages[0].scope_digest == pages[2].scope_digest,
                repeated_head_size_equal=pages[0].page_size == pages[2].page_size,
                missed_listing_count=None, publication_latency=None)


def run(ledger_path, observation_path, emit=lambda value: None):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    budget = Budget(ledger_path, 5, 10020000)
    observations = None
    report = dict(paid_api_calls=0, telegram_calls=0, samples=[], ready_for_delivery=False)
    try:
        observations = Observations(observation_path)
        fetch = PublicFetch(budget)

        def get(url):
            budget.observe(url)
            due = budget.db.execute('SELECT next_at FROM budget').fetchone()[0]
            delay = max(0, due-time.time())
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(delay)
            body, receipt = fetch.get(url, 20000 if url == ROBOTS else 2500000)
            emit({'receipt': receipt})
            if body is None:
                raise Denied('source_unavailable')
            return body, receipt

        _, report['robots_receipt'] = get(ROBOTS)
        pages, card_sets = [], []
        url = FEED
        for index in range(3):
            body, receipt = get(url)
            cards = parse_public_cards(body)
            metadata = inspect_page(body, url)
            pages.append(metadata)
            card_sets.append(cards)
            sample = dict(receipt=receipt, metadata=asdict(metadata),
                          cards=len(cards), intake=observations.ingest(cards, receipt['completed_at']))
            report['samples'].append(sample)
            emit({'sample': sample})
            url = next_page(metadata) if index == 0 else FEED
        report['comparison'] = compare(pages, card_sets)
        # Selection fixed by source order, before inspecting detail/price.
        subject = next((card for card in card_sets[2] if card.url and not card.issues), None)
        if subject is None:
            raise Denied('no_clean_detail_candidate')
        body, receipt = get(subject.url)
        detail = dict(receipt=receipt, listing_id=subject.listing_id)
        try:
            detail.update(record=public_peer_record(body, subject.listing_id, receipt['completed_at']),
                          visible=asdict(parse_visible_facts(body, subject.listing_id)),
                          crosscheck=crosscheck_details(body, subject.listing_id))
        except ValueError as exc:
            detail['reason'] = str(exc)
        report.update(detail=detail, status='bounded_experiment_complete')
    except ValueError as exc:
        report.update(status='stopped', reason=str(exc))
    finally:
        report['budget'] = budget.summary()
        budget.close()
        if observations is not None:
            observations.close()
    return report


if __name__ == '__main__':
    report = run(sys.argv[1], sys.argv[2],
                 lambda item: print(json.dumps(item, default=str), flush=True))
    print(json.dumps({'result': report}, default=str), flush=True)
