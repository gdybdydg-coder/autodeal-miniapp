"""Offline capacity/failure drill: production queue, fake provider and Telegram.

This verifies completeness and isolation, not Render or Telegram throughput.
"""
import asyncio
from collections import Counter
from dataclasses import replace
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Delivery, MonitorJob, Search, SourceBudget, User
from backend.monitor import Monitor, delivery_batch, reset_watch
from backend.tests.test_monitor import p, add_search, drain, details
from backend.tests.test_parallel_monitor import queue
from backend.tests.test_ria_ai_price import enable
from backend.worker import enqueue


def test_200_users_800_deliveries_survive_stop_retry_block_timeout_and_restart(p, monkeypatch):
    p.clock[0] -= 2
    for sid in range(2, 201):
        add_search(p, sid=sid, uid=1000 + sid)
    p.clock[0] += 2
    quotes = enable(p, monkeypatch)
    p.runner.settings = replace(p.runner.settings, ria_confirmed_deals_only=True)
    drain(p)
    queue(p, 4)
    with Session(p.engine) as db:
        before = db.get(SourceBudget, 'auto_ria').total
    p.runner.tick()
    enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
    with Session(p.engine) as db:
        assert db.get(SourceBudget, 'auto_ria').total - before == 8
        assert len(list(db.scalars(select(Delivery)))) == 800
        # Same state transition as /stop, after all four messages were queued.
        db.get(User, 1002).ready = False
        search = db.get(Search, 2)
        search.enabled = False
        reset_watch(db, search.id, False)
        db.commit()

    calls, accepted, lock = [], [], threading.Lock()
    retry_pair = uncertain_pair = None
    peak = active = 0
    def sender(uid, car):
        nonlocal retry_pair, uncertain_pair, peak, active
        pair = (uid, car.source_id)
        with lock:
            calls.append((pair, p.clock[0]))
            active += 1
            peak = max(peak, active)
        try:
            if uid == 1003:
                return {'ok': False, 'error_code': 403}
            with lock:
                if uid == 1004 and retry_pair is None:
                    retry_pair = pair
                    return {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 3}}
                if uid == 1005 and uncertain_pair is None:
                    uncertain_pair = pair
                    raise TimeoutError('unknown acceptance')
                accepted.append(pair)
                message_id = len(accepted)
            return {'ok': True, 'result': {'message_id': message_id}}
        finally:
            with lock:
                active -= 1

    started = time.perf_counter()
    async def dispatch():
        # SQLite lacks PostgreSQL SKIP LOCKED, so concurrent claims can be busy
        # and require more batches. Keep this a correctness, not speed, gate.
        for batch in range(1200):
            if batch == 25:
                # Reconnect DB sessions and recreate the worker, retaining the
                # on-disk queue (not a real process crash or PostgreSQL drill).
                p.engine.dispose()
                p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, sender)
                enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
            states = await delivery_batch(p.engine, p.runner.settings, sender)
            p.clock[0] += .3
            if all(state == 'empty' for state in states):
                with Session(p.engine) as db:
                    if db.scalar(select(Delivery.id).where(Delivery.state == 'pending').limit(1)) is None:
                        return batch + 1
        raise AssertionError('queue did not drain')
    batches = asyncio.run(dispatch())
    elapsed = time.perf_counter() - started
    with Session(p.engine) as db:
        rows = list(db.scalars(select(Delivery)))
        assert Counter(row.state for row in rows) == {
            'sent': 791, 'cancelled': 7, 'failed': 1, 'uncertain': 1}
        assert len(rows) == len({(row.user_id, row.listing_id) for row in rows}) == 800
        assert not db.get(User, 1002).ready and not db.get(User, 1003).ready
        assert all(job.state == 'checked' for job in db.scalars(select(MonitorJob)))
        assert db.get(SourceBudget, 'auto_ria').total - before == 8
    assert len(accepted) == len(set(accepted)) == 791
    assert not any(pair[0] == 1002 for pair, _ in calls)
    assert sum(pair == uncertain_pair for pair, _ in calls) == 1
    retry_times = [at for pair, at in calls if pair == retry_pair]
    assert len(retry_times) == 2 and retry_times[1] - retry_times[0] >= 3
    assert peak <= 4
    assert sorted(quotes) == ['200', '201', '202', '203']
    assert all(len(details(p, sid)) == 1 for sid in quotes)
    print(f'capacity: users=200 queued=800 accepted=791 batches={batches} '
          f'local_sqlite_seconds={elapsed:.3f} fake_telegram=true')
