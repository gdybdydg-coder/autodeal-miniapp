"""Reproducible synthetic benchmark; all source and Telegram events are fixtures.

Run: python -m experiments.free_search.replay_demo --users 200 --searches 5
No external requests, credentials, server startup or production imports.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time

from experiments.free_search.filter_gate import SearchFilter
from experiments.free_search.http_budget import HttpPolicy, SourcePermission
from experiments.free_search.offline_pipeline import (
    PipelineState, ingest_feed_pages_fixture, reserve_detail, complete_detail_fixture,
    complete_research_estimate, recover_pipeline_after_restart,
)
from experiments.free_search.offline_queue import (
    QueueState, UserState, Subscription, stop_user, begin_send, finish_send,
)
from experiments.free_search.replay_store import ReplayStore

BASE = 1790827200.0
POLICY = HttpPolicy(512, 1, 1000, 10000, 100000000, 1000000000, 20000, 3, 10, 30)
FIXTURE_PERMISSION = SourcePermission(True, True, True)


def card(sid, added=BASE+30):
    date = datetime.fromtimestamp(added, timezone.utc).isoformat()
    return f'<section class="ticket-item" data-advertisement-id="{sid}"><div data-add-date="{date}"><a class="m-link-ticket" href="https://auto.ria.com/uk/auto_fixture_{sid}.html">fixture</a></div></section>'


def detail(sid):
    url = f'https://auto.ria.com/uk/auto_fixture_{sid}.html'
    data = {'@type': 'Vehicle', 'url': url, 'brand': {'name': 'Fixture'}, 'model': 'Demo',
            'productionDate': '2015', 'bodyType': 'Легкові',
            'offers': {'@type': 'Offer', 'price': '5000', 'priceCurrency': 'USD',
                       'availability': 'https://schema.org/InStock'}}
    return f'<link rel="canonical" href="{url}"><script type="application/ld+json">{json.dumps(data)}</script><a>Легкові з пробігом</a><a>Тернопільська область</a><h1>Fixture</h1><p>Оголошення створене 01.10.2026</p><p>ID авто {sid}</p>'


def run(users=200, searches=5, database=None):
    if not 1 <= users <= 200 or not 1 <= searches <= 10:
        raise ValueError('benchmark_size_out_of_bounds')
    if database is not None and Path(database).exists():
        raise ValueError('demo_requires_new_database')
    tmp = tempfile.TemporaryDirectory() if database is None else None
    path = Path(tmp.name) / 'demo.free-search.sqlite3' if tmp else Path(database)
    store = ReplayStore(path)
    start = time.perf_counter()
    try:
        queue = QueueState(users=tuple(UserState(i, True) for i in range(1, users+1)),
            subscriptions=tuple(Subscription((uid-1)*searches+n, uid, f'e{uid}', BASE-1, True)
                for uid in range(1, users+1) for n in range(1, searches+1)))
        store.apply(BASE, 'setup', lambda _: (PipelineState(queue=queue), 'synthetic'))
        def feed(state, pages, at):
            updated, result = ingest_feed_pages_fixture(state, pages, at, POLICY)
            return updated, result.reason
        store.apply(BASE, 'discovery', lambda s: feed(s, [card('700', BASE-1)], BASE))
        # Independent fixture inventory is defined before the feed and is not
        # derived from collector output: 120 expected IDs, across two pages.
        control = {str(i) for i in range(800, 920)}
        ordered = sorted(control)
        pages = [''.join(card(sid) for sid in ordered[:70]), ''.join(card(sid) for sid in ordered[60:])]
        state = store.apply(BASE+60, 'discovery', lambda s: feed(s, pages, BASE+60))
        detected = {row.candidate.listing_id for row in state.pending_details}
        # Persist and reopen an actual SQLite file before continuing.
        store.close()
        store = ReplayStore(path)
        state = store.apply(BASE+61, 'restart', lambda s: (recover_pipeline_after_restart(s, BASE+61, POLICY), 'recovered'))
        bindings = {row.search_id: SearchFilter(brand='Fixture', model='Demo',
            regions=frozenset({'Тернопільська область'}), min_discount_percent=Decimal('10'))
            for row in state.queue.subscriptions}
        reservation = None
        def reserve(s):
            nonlocal reservation
            s, reservation = reserve_detail(s, BASE+62, FIXTURE_PERMISSION, POLICY)
            return s, reservation.reason
        state = store.apply(BASE+62, 'details', reserve)
        sid = next(row.candidate.listing_id for row in state.pending_details if row.key == reservation.key)
        def completed(s):
            s, result = complete_detail_fixture(s, reservation.reservation_id, BASE+63, detail(sid), bindings, POLICY)
            return s, result.reason
        state = store.apply(BASE+63, 'details', completed)
        pending_valuation = len(state.research_candidates)
        claims_without_estimate = len(state.queue.claims)
        # This is a caller-supplied synthetic research estimate, NEVER AUTO.RIA.
        def estimate(s):
            s, result = complete_research_estimate(s, sid, Decimal('6000'), True, bindings)
            return s, result.reason
        state = store.apply(BASE+64, 'valuation', estimate)
        state = store.apply(BASE+65, 'stop', lambda s: (replace(s, queue=stop_user(s.queue, users)), 'stopped'))
        # Simulated API acceptance, with one ambiguous timeout; no real sender.
        for claim in state.queue.claims:
            if claim.state != 'pending':
                continue
            def begin(s, uid=claim.user_id):
                q, allowed = begin_send(s.queue, uid, sid)
                return replace(s, queue=q), 'sending' if allowed else 'cancelled'
            store.apply(BASE+66, 'mock_delivery', begin)
            outcome = 'ambiguous' if claim.user_id == 1 else 'accepted'
            store.apply(BASE+67, 'mock_delivery', lambda s, uid=claim.user_id, out=outcome:
                (replace(s, queue=finish_send(s.queue, uid, sid, out)), out))
        state = store.load()
        counts = {kind: sum(c.state == kind for c in state.queue.claims)
                  for kind in ('sent', 'uncertain', 'cancelled', 'pending')}
        return {'mode': 'offline_synthetic', 'users': users, 'searches_per_user': searches,
            'filter_checks_one_listing': users*searches, 'control_listings': len(control),
            'detected': len(control & detected), 'missed': sorted(control-detected),
            'unexpected': sorted(detected-control), 'feed_pages': 2,
            'detail_fixture_requests': len(state.http.ledger), 'research_estimates': 1,
            'valuation_pending_before_estimate': pending_valuation,
            'claims_without_estimate': claims_without_estimate,
            'claims': len(state.queue.claims), 'mock_delivery_outcomes': counts,
            'remaining_details': len(state.pending_details), 'paid_api_requests': 0,
            'external_http_requests': 0, 'telegram_requests': 0,
            'elapsed_wall_seconds': round(time.perf_counter()-start, 4),
            'live_latency_percentiles': None, 'whole_market_recall': None,
            'diagnostics': store.diagnostics()}
    finally:
        store.close()
        if tmp:
            tmp.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users', type=int, default=200)
    parser.add_argument('--searches', type=int, default=5)
    parser.add_argument('--database')
    args = parser.parse_args()
    print(json.dumps(run(args.users, args.searches, args.database), ensure_ascii=False, indent=2))
