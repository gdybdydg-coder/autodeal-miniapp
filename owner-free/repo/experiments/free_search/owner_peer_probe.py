"""One public comparable-price experiment, ten frozen peers maximum."""
import json
import os
import re
import sys
import time

from .network_guard import CREDENTIAL_NAMES
from .owner_budget import Budget, Denied, public_url
from .owner_public_fetch import PublicFetch, ROBOTS
from .public_cards import parse_public_cards
from .public_peer_pilot import public_peer_record
from .public_detail_crosscheck import crosscheck_details
from .valuation import estimate
from .owner_preview import parse_previews, reject_reason
from .owner_peer_cache import PeerCache
from .owner_peer_pool import pool
from .owner_valuation_policy import OWNER_VALUATION_POLICY


def freeze(cards, subject_id, previews=None, subject=None, rejections=None):
    # Source order; never pick by price or by the eventual estimation outcome.
    selected = []
    seen = {subject_id}
    for card in cards:
        if card.listing_id in seen or not card.url or card.issues or card.promoted:
            continue
        if subject is not None:
            reason = reject_reason((previews or {}).get(card.listing_id, {}), subject)
            if reason:
                if rejections is not None:
                    rejections[card.listing_id] = reason
                continue
        public_url(card.url)
        seen.add(card.listing_id)
        selected.append((card.listing_id, card.url))
        if len(selected) == 10:
            break
    return selected


def run(ledger, catalogue, subject_url, cache_path=None, emit=lambda item: None,
        *, budget_factory=None, peer_limit=10):
    if type(peer_limit) is not int or not 1 <= peer_limit <= 10:
        raise ValueError('invalid_peer_limit')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    public_url(catalogue)
    public_url(subject_url)
    match = re.fullmatch(r'https://auto.ria.com/uk/auto_[A-Za-z0-9_-]+_([1-9][0-9]{0,11})\.html', subject_url)
    if not match:
        raise Denied('invalid_subject_url')
    subject_id = match[1]
    budget = Budget(ledger, 13, 30020000) if budget_factory is None else budget_factory()
    cache = None
    result = dict(paid_api_calls=0, telegram_calls=0, ready_for_delivery=False,
                  peers=[], peer_errors={}, selection='first_clean_source_order', peer_limit=peer_limit,
                  independent_quality_validation=False, publication_at=None, cache_hits=0)
    try:
        if cache_path is not None:
            cache = PeerCache(cache_path)
        fetch = PublicFetch(budget)

        def get(url):
            budget.observe(url)
            delay = max(0, budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(delay)
            body, receipt = fetch.get(url, 20000 if url == ROBOTS else 2500000)
            emit({'receipt': receipt})
            if body is None:
                raise Denied('source_unavailable')
            return body, receipt

        def record(url, ident):
            if cache is not None:
                max_age = (OWNER_VALUATION_POLICY.max_subject_price_age_seconds if ident == subject_id
                           else OWNER_VALUATION_POLICY.max_peer_age_seconds)
                prior = next((row for row in cache.snapshot(time.time(), max_age)['rows']
                              if row['listing_id'] == ident), None)
                if prior is not None:
                    result['cache_hits'] += 1
                    return prior
            body, receipt = get(url)
            try:
                row = public_peer_record(body, ident, receipt['completed_at'])
                cross = crosscheck_details(body, ident)
                if cross['issues']:
                    raise ValueError('public_detail_conflict')
                row['drive'] = cross['drive_text']
            except ValueError as exc:
                if cache is not None:
                    cache.put(ident, receipt['completed_at'], reason=str(exc))
                raise
            if cache is not None:
                cache.put(ident, receipt['completed_at'], row=row)
            return row

        get(ROBOTS)
        subject = record(subject_url, subject_id)
        result['subject'] = subject
        body, receipt = get(catalogue)
        cards = parse_public_cards(body)
        result['preview_rejections'] = {}
        frozen = freeze(cards, subject_id, parse_previews(body), subject, result['preview_rejections'])[:peer_limit]
        result.update(catalogue_receipt=receipt, catalogue_cards=len(cards), frozen_peers=frozen)
        emit({'frozen_peers': frozen})
        selected = []
        for ident, url in frozen:
            try:
                peer = record(url, ident)
                result['peers'].append(peer)
                for field in ('generation', 'drive'):
                    if not subject.get(field) or not peer.get(field):
                        raise ValueError(field+'_unknown')
                    if peer[field] != subject[field]:
                        raise ValueError(field+'_mismatch')
                selected.append(peer)
            except Denied:
                raise  # Source denial must stop the entire experiment.
            except ValueError as exc:
                result['peer_errors'][ident] = str(exc)
        result['estimate'] = estimate(subject, selected, time.time(), OWNER_VALUATION_POLICY).as_dict()
        if cache is not None:
            result['accumulated_pool'] = pool(subject, cache, time.time())
        result['status'] = 'bounded_comparison_complete'
    except ValueError as exc:
        result.update(status='stopped', reason=str(exc))
    finally:
        result['budget'] = budget.summary()
        budget.close()
        if cache is not None:
            cache.close()
    return result


if __name__ == '__main__':
    report = run(*sys.argv[1:5], emit=lambda item: print(json.dumps(item, default=str), flush=True))
    print(json.dumps({'result': report}, default=str), flush=True)
