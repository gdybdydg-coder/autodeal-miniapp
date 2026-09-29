"""Real queue membership, acceptance-only distributions and read-only owner output."""
from dataclasses import replace
from datetime import datetime
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import bot_commands, operational_stats as ops, poll_schedule, telegram_setup
from backend.app import create_app
from backend.models import (Delivery, DeliveryTiming, MonitorControl, MonitorFeed,
    MonitorJob, MonitorMatch, MonitorMembership, MonitorSeen, MonitorWatch, Search, SourceBudget, User)
from backend.tests.test_owner_alerts import h, delivery
from backend.tests.test_backend import setup, headers, command


def add_timing(db, ident, now, delay, *, uid=222, state="sent", accepted=None, discovered=None):
    end = now-10 if accepted is None else accepted
    db.add(Delivery(id=ident, user_id=uid, listing_id=ident, state=state))
    db.add(DeliveryTiming(delivery_id=ident, queued_at=end-delay/2,
        discovered_at=end-delay if discovered is None else discovered, accepted_at=end))


def test_latency_uses_full_window_and_real_acceptance_with_private_scope(h):
    engine, _, clock = h
    now = clock[0]
    with Session(engine) as db:
        for i in range(1, 21):
            add_timing(db, i, now, i)
        add_timing(db, 21, now, 10000, uid=111)
        add_timing(db, 22, now, 9999, state="uncertain")
        add_timing(db, 23, now, 9999, state="failed")
        add_timing(db, 24, now, 9999, accepted=now-86401)
        add_timing(db, 25, now, 9999, accepted=now+1)
        add_timing(db, 26, now, 9999, discovered=now+1)
        db.commit()
        mine = ops.latency(db, 222)
        assert mine['accepted_messages'] == 21
        assert mine['discovery_to_telegram'] == {'samples': 20, 'p50_seconds': 10.5, 'p95_seconds': 19}
        assert mine['queue_to_telegram']['samples'] == 21
        assert ops.latency(db, 111)['discovery_to_telegram']['p95_seconds'] == 10000
        assert ops.latency(db)['discovery_to_telegram'] == {'samples': 21, 'p50_seconds': 11, 'p95_seconds': 20}
        assert ops.latency(db, 999)['discovery_to_telegram']['p50_seconds'] is None
        assert 'source_id' not in str(mine) and 'user_id' not in str(mine)


def test_missing_timestamps_and_empty_window_are_not_zero_latency(h):
    with Session(h[0]) as db:
        add_timing(db, 1, h[2][0], 10)
        db.flush()
        db.get(DeliveryTiming, 1).discovered_at = None
        db.commit()
        report = ops.latency(db)
        assert report['accepted_messages'] == 1
        assert report['discovery_to_telegram'] == {'samples': 0, 'p50_seconds': None, 'p95_seconds': None}
        text = ops.text(db, h[1])
        assert 'немає коректних вимірів' in text and 'Мало вимірів' in text


def test_valuation_counts_unique_current_cars_including_backlog_older_than_day(h):
    engine, _, clock = h
    with Session(engine) as db:
        db.add_all([Search(id=2, user_id=222, name='second', fingerprint='g', filters={}, enabled=True),
                    MonitorWatch(search_id=2, epoch='b')])
        for sid, state, epoch, extra in [('new', 'pending', 'a', False),
                ('old-active', 'pending', 'a', True), ('stale-epoch', 'pending', 'retired', False),
                ('done', 'checked', 'a', False)]:
            db.add(MonitorJob(source_id=sid, state=state, first_seen=clock[0]-90000,
                result={'discovery_kind': 'active_window'} if extra else {}))
            db.add(MonitorSeen(search_id=1, source_id=sid, epoch=epoch, state='pending', first_seen=clock[0]-90000))
        db.add(MonitorSeen(search_id=2, source_id='new', epoch='b', state='pending', first_seen=clock[0]))
        db.commit()
        assert ops.queues(db)['valuation'] == {'new_publications': 1, 'supplemental': 1, 'total': 2, 'unit': 'unique_cars'}
        assert ops.queues(db, 111)['valuation']['total'] == 0
        db.get(Search, 1).enabled = False
        db.commit()
        assert ops.queues(db)['valuation']['total'] == 1
        db.get(User, 222).ready = False
        db.commit()
        assert ops.queues(db)['valuation']['total'] == 0


def test_delivery_queue_deduplicates_matches_and_excludes_stopped_stale_uncertain(h):
    engine, _, clock = h
    for ident, state in [(1, 'pending'), (2, 'sending'), (3, 'uncertain'), (4, 'failed'), (5, 'pending')]:
        delivery(h, ident=ident, state=state, age=600)
    with Session(engine) as db:
        db.get(MonitorMatch, (1, 5)).fingerprint = 'old-filter'
        db.add_all([Search(id=2, user_id=222, name='second', fingerprint='g', filters={}, enabled=True),
                    MonitorWatch(search_id=2, epoch='b'),
                    MonitorMatch(search_id=2, listing_id=1, epoch='b', fingerprint='g')])
        db.commit()
        assert ops.queues(db)['delivery'] == {'pending': 1, 'sending': 1, 'oldest_wait_seconds': 600, 'unit': 'messages'}
        assert ops.queues(db, 111)['delivery']['pending'] == 0
        db.get(MonitorWatch, 2).epoch = 'new-epoch'
        db.get(Search, 1).enabled = False
        db.commit()
        assert ops.queues(db)['delivery']['pending'] == 0
        assert ops.queues(db)['delivery']['sending'] == 0


@pytest.mark.parametrize('instant,expected', [('2026-09-28T04:59:59+00:00', 'нічна пауза до 08:00'),
    ('2026-09-28T05:00:00+00:00', 'кожні 300 с')])
def test_owner_text_schedule_and_last_success_survive_current_feed_error(h, instant, expected):
    engine, settings, clock = h
    clock[0] = datetime.fromisoformat(instant).timestamp()
    with Session(engine) as db:
        feed = db.get(MonitorFeed, 'f')
        feed.checked_at, feed.status = clock[0]-100, 'quota_exceeded'
        db.get(MonitorControl, 'pilot').heartbeat = clock[0]
        db.add(MonitorFeed(id='unused', filters={}, started_at=clock[0], cursor=clock[0], checked_at=clock[0]))
        db.add_all([Search(id=3, user_id=222, name='new', fingerprint='h', filters={}, enabled=True),
                    MonitorWatch(search_id=3, epoch='c'),
                    MonitorMembership(search_id=3, epoch='c', feed_id='missing', started_at=clock[0])])
        db.commit()
        text = bot_commands.stats_text(db, 111, 111, settings=replace(settings, ria_active_window_enabled=True))
        assert expected in text and ops.timestamp(clock[0]-100) in text
        assert 'Груп пошуку: 2' in text and 'Груп без успішної перевірки: 1' in text
        assert 'не від публікації' in text and 'не підтверджує push' in text
        assert len(text.encode('utf-16-le'))//2 < 4096


def test_stats_does_not_mutate_state_and_non_owner_never_reads_operational_data(h, monkeypatch):
    engine, settings, _ = h
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with Session(engine) as db:
            bot_commands.stats_text(db, 111, 111, settings=settings)
            assert statements and set(statements) == {'SELECT'}
            assert db.get(SourceBudget, 'auto_ria').total == 130000
            assert db.get(MonitorWatch, 1).epoch == 'a'
            assert db.get(Search, 1).enabled
        monkeypatch.setattr(ops, 'text', lambda *_: pytest.fail('private statistics accessed'))
        with Session(engine) as db:
            assert 'лише адміністратору' in bot_commands.stats_text(db, 222, 111, settings=settings)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)


def test_command_transport_contains_extended_stats_and_does_not_replay(setup, monkeypatch):
    engine, settings, _ = setup
    settings = replace(settings, admin_telegram_id=111)
    calls = []
    def fake_send(token, method, payload):
        calls.append(payload)
        return {'ok': True, 'result': {'message_id': 99}}
    monkeypatch.setattr(telegram_setup, 'call', fake_send)
    with TestClient(create_app(settings, engine)) as api:
        # A redelivered Telegram update retains its original message date.
        message_date = int(time.time())
        assert command(api, '/stats', date=message_date).status_code == 200
        assert command(api, '/stats', date=message_date).status_code == 200
    assert 'Черги зараз' in calls[0]['text'] and 'Затримки за останні 24 год' in calls[0]['text']
    assert 'Додаткова перевірка: вимкнено' in calls[0]['text']
    assert len(calls) == 1


def test_public_aggregates_and_authenticated_statistics_keep_user_scope(setup):
    engine, _, api = setup
    with Session(engine) as db:
        import time
        add_timing(db, 1, time.time(), 10, uid=111)
        add_timing(db, 2, time.time(), 80, uid=222)
        db.commit()
    one = api.get('/api/notifications/status', headers=headers(111)).json()['activity']
    two = api.get('/api/notifications/status', headers=headers(222)).json()['activity']
    public = api.get('/api/source-status').json()['launch']['activity']
    assert one['latency']['discovery_to_telegram']['p95_seconds'] == 10
    assert two['latency']['discovery_to_telegram']['p95_seconds'] == 80
    assert public['latency']['discovery_to_telegram']['samples'] == 2
    assert 'user_id' not in str(public['latency'])
    assert api.get('/api/notifications/status').status_code == 401


def test_missing_timezone_uses_explicit_utc_label(monkeypatch):
    monkeypatch.setattr(poll_schedule, 'KYIV', None)
    assert ops.timestamp(1790583000).endswith('(UTC)')
