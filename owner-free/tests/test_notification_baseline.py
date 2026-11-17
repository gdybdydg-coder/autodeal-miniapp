import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.models import Base, SourceProbe
from experiments.free_search import notification_baseline as b
from experiments.free_search.owner_sorted_service import Store


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(b.time, 'time', lambda: 1800000000)
    return 1800000000


def card(clock, age='2 хвилини тому', promoted=False):
    return {'id':'123','age_text':age,'publication_observed_at':clock,'promoted':promoted}


@pytest.mark.parametrize('age', ['оновлено 2 хвилини тому','вчора','сьогодні о 12:00','unknown',''])
def test_unknown_or_updated_labels_never_prove_publication(clock, age):
    assert b.publication_lower_bound(card(clock, age)) is None


def test_source_rounded_age_has_conservative_lower_bound(clock):
    assert b.publication_lower_bound(card(clock)) == clock-240
    assert b.publication_lower_bound(card(clock, promoted=True)) is None
    assert b.publication_lower_bound({'age_text':'2 хвилини тому'}) is None


def test_explicit_addition_date_and_dst_ambiguity(clock):
    from datetime import datetime, timezone
    c = card(clock)
    c['source_add_date'] = datetime.fromtimestamp(clock-10,timezone.utc).isoformat()
    assert b.publication_lower_bound(c) == clock-10
    c['source_add_date'] = '2026-10-25 03:30:00'
    assert b.publication_lower_bound(c) is None


def test_first_head_preserves_post_cutoff_cars_but_excludes_downtime_and_unknown(clock):
    new = card(clock)
    old = card(clock, '2 години тому')
    unknown = card(clock, 'оновлено 1 хвилину тому')
    report = {'reason':'head_baseline','observed_cards':[new,old,unknown], 'newly_observed':[]}
    assert b.candidates(report, clock-3600) == [new]
    assert b.candidates(report, None) == []  # Legacy behavior when no reset.
    assert b.candidates(report, clock) == []


def test_server_baseline_is_recipient_specific_and_requires_source_evidence(tmp_path, clock):
    engine = create_engine('sqlite:///'+str(tmp_path/'pg.db'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(SourceProbe(id='notification-baseline-111',status='active',checked_at=clock,
            result={'baseline_at':clock-3600,'reset_id':'once'}))
        db.commit()
    assert b.allowed(engine,111,card(clock))
    assert not b.allowed(engine,111,card(clock,'2 години тому'))
    assert not b.allowed(engine,111,{'first_seen':clock,'observed_at':clock})
    assert b.allowed(engine,222,{})  # Other recipients are untouched.
    engine.dispose()


def test_local_atomic_reset_cancels_backlog_and_keeps_send_claims_and_identity(tmp_path, clock):
    store = Store(tmp_path/'state.sqlite3',111)
    with store.db:
        store.db.execute("INSERT INTO checkpoints VALUES('old-search','{}')")
        store.db.execute("INSERT INTO jobs VALUES('old-search','123','https://example.org',?,'pending','')",(clock-1000,))
        store.db.execute("INSERT INTO sent VALUES('456','sent',12,'{}')")
        store.db.execute("INSERT INTO sent VALUES('789','uncertain',NULL,'{}')")
        store.db.execute("INSERT INTO detail_delays VALUES('123',?)",(clock+100,))
    before = store.db.execute('SELECT * FROM sent').fetchall()
    b.sync_store(store,clock)
    assert store.db.execute('SELECT state,reason FROM jobs').fetchall() == [('cancelled','notification_baseline_reset')]
    assert store.db.execute('SELECT key FROM checkpoints').fetchall() == [('notification-baseline',)]
    assert store.db.execute('SELECT * FROM sent').fetchall() == before
    assert store.db.execute('SELECT * FROM identity').fetchall() == [(111,)]
    assert store.db.execute('SELECT id FROM detail_delays').fetchall() == [('123',)]
    with store.db:
        store.db.execute("INSERT INTO checkpoints VALUES('new-search','{}')")
        store.db.execute("INSERT INTO jobs VALUES('new-search','new','https://example.org',?,'pending','')",(clock+1,))
    b.sync_store(store,clock)
    assert store.db.execute("SELECT state FROM jobs WHERE id='new'").fetchone() == ('pending',)
    assert store.db.execute("SELECT state FROM checkpoints WHERE key='new-search'").fetchone()
    with pytest.raises(ValueError,match='backwards'):
        b.sync_store(store,clock-1)
    store.db.close()
