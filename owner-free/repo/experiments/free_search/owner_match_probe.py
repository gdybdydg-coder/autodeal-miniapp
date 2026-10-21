"""Bounded local matching against a recent read-only owner search snapshot."""
from dataclasses import asdict
from decimal import Decimal
import json
import os
import sys
import time
from pathlib import Path

from backend.models import Filters
from .network_guard import CREDENTIAL_NAMES
from .owner_budget import Budget, Denied
from .owner_public_fetch import PublicFetch, ROBOTS
from .owner_probe import FEED
from .owner_pagination_probe import next_page
from .public_cards import parse_public_cards
from .public_pagination import inspect_page, assess_scan
from .public_peer_pilot import public_peer_record
from .public_detail_crosscheck import crosscheck_details
from .visible_adapter import parse_visible_facts
from .filter_gate import PublicEvidence, gate
from .owner_pilot import _filters
from .owner_peer_cache import PeerCache


def validate_snapshot(snapshot, now):
    if not (snapshot.get('owner_identity_verified') is True and snapshot.get('owner_confirmed_purchase') is True
            and snapshot.get('owner_ready') is True and 0 <= now-snapshot['checked_at'] <= 900
            and snapshot['expires_at'] > now):
        raise Denied('owner_snapshot_stale_or_ineligible')
    for search in snapshot['searches']:
        if not search['epoch'] or Filters.model_validate(search['filters']).fingerprint() != search['fingerprint']:
            raise Denied('owner_snapshot_filter_conflict')
    return snapshot['searches']


def run(ledger, snapshot_path, cache_path, emit=lambda value: None):
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    snapshot = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    searches = validate_snapshot(snapshot, time.time())
    budget = Budget(ledger, 6, 12520000)
    cache = PeerCache(cache_path)
    result = dict(paid_api_calls=0, telegram_calls=0, matches=[], ready_for_delivery=False,
                  snapshot_checked_at=snapshot['checked_at'], candidates_checked=0)
    try:
        fetch = PublicFetch(budget)
        def get(url):
            budget.observe(url)
            delay = max(0,budget.db.execute('SELECT next_at FROM budget').fetchone()[0]-time.time())
            if delay > 60:
                raise Denied('pause_requires_later_invocation')
            time.sleep(delay)
            body, receipt = fetch.get(url, 20000 if url == ROBOTS else 2500000)
            emit({'receipt':receipt})
            if body is None:
                raise Denied('source_unavailable')
            return body, receipt
        get(ROBOTS)
        pages, cards = [], []
        url = FEED
        for i in range(2):
            body, receipt = get(url)
            metadata = inspect_page(body,url)
            pages.append(metadata)
            cards.extend(parse_public_cards(body))
            if i == 0:
                url = next_page(metadata)
        result['coverage'] = assess_scan(pages)
        selected, seen = [], set()
        for card in cards:
            if card.listing_id in seen or card.issues or not card.url or card.preview_usd is None:
                continue
            seen.add(card.listing_id)
            if any((_filters(s['filters']).price_usd.minimum or 0) <= card.preview_usd <=
                   (_filters(s['filters']).price_usd.maximum or Decimal('Infinity')) for s in searches):
                selected.append(card)
            if len(selected) == 3:
                break
        result['frozen_candidates'] = [card.listing_id for card in selected]
        for card in selected:
            validate_snapshot(snapshot,time.time())
            body, receipt = get(card.url)
            result['candidates_checked'] += 1
            try:
                row = public_peer_record(body, card.listing_id, receipt['completed_at'])
                visible = parse_visible_facts(body,card.listing_id)
                cross = crosscheck_details(body,card.listing_id)
                if cross['issues']:
                    raise ValueError('public_detail_conflict')
                row['drive'] = cross['drive_text']
                cache.put(card.listing_id,receipt['completed_at'],row=row)
                evidence = PublicEvidence(listing_id=card.listing_id,publication_proven=False,
                    active=True,category=visible.category,price_usd=Decimal(str(row['price'])),
                    region=row['region'],year=row['year'],brand=row['brand'],model=row['model'],
                    mileage_km=row['mileage_km'],body=row['body'],fuel=row['fuel'],transmission=row['transmission'])
                decisions = [{'search_id':s['search_id'],**asdict(gate(evidence,_filters(s['filters'])))} for s in searches]
                result['matches'].append(dict(listing_id=card.listing_id,url=card.url,record=row,
                                               source_add_date=card.added_at,decisions=decisions))
            except ValueError as exc:
                result['matches'].append(dict(listing_id=card.listing_id,reason=str(exc)))
        result['status'] = 'bounded_matching_complete'
    except ValueError as exc:
        result.update(status='stopped',reason=str(exc))
    finally:
        result['budget'] = budget.summary()
        budget.close()
        cache.close()
    return result


if __name__ == '__main__':
    result = run(*sys.argv[1:4], emit=lambda value:print(json.dumps(value,default=str),flush=True))
    print(json.dumps({'result':result},default=str),flush=True)
