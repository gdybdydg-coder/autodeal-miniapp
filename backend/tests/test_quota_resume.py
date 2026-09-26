import copy
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest

from backend.models import SourceBudget, MonitorFeed, MonitorJob, MonitorWatch, Search
from backend.tests.test_monitor import p, drain, wake


def arrange(p, monkeypatch):
    for name,value in [('HOURLY','4500'),('DAILY','90000'),('TOTAL','102060')]:
        monkeypatch.setenv('RIA_REQUESTS_'+name+'_CAP', value)
    drain(p)
    with Session(p.engine) as db:
        budget=db.get(SourceBudget,'auto_ria');budget.total=90000;budget.calls=[]
        feed=db.scalar(select(MonitorFeed))
        feed.status='quota_exceeded';feed.next_poll=p.clock[0]+3600
        db.add(MonitorJob(source_id='124',first_seen=p.clock[0],state='pending',reason='quota_exceeded',next_run=p.clock[0]+3600,
            result={'discovery_kind':'new_publication'}))
        db.add(MonitorJob(source_id='125',first_seen=p.clock[0],state='pending',reason='connection_error',next_run=p.clock[0]+3600))
        db.commit()
        return feed.id, feed.cursor, copy.deepcopy(feed.context), db.get(MonitorWatch,1).epoch


def resume(p, group):
    assert p.runner.claim()
    try:
        with Session(p.engine) as db: p.runner.resume_available_quota(db,{group})
    finally: p.runner.release('idle')


def test_increased_cap_wakes_quota_waits_without_resetting_accounting_or_checkpoints(p,monkeypatch):
    group,cursor,context,epoch=arrange(p,monkeypatch)
    before=len(p.calls)
    resume(p,group)
    with Session(p.engine) as db:
        feed=db.get(MonitorFeed,group)
        assert feed.next_poll==p.clock[0] and feed.cursor==cursor and feed.context==context
        assert db.get(MonitorJob,'124').next_run==p.clock[0]
        assert db.get(MonitorJob,'125').next_run>p.clock[0]
        assert db.get(SourceBudget,'auto_ria').total==90000
        assert db.get(MonitorWatch,1).epoch==epoch
    assert len(p.calls)==before


@pytest.mark.parametrize('gate',['upstream','hourly','daily','total'])
def test_remaining_budget_gates_cannot_be_bypassed(p,monkeypatch,gate):
    group,*_=arrange(p,monkeypatch)
    with Session(p.engine) as db:
        budget=db.get(SourceBudget,'auto_ria')
        if gate=='upstream': budget.blocked_until=p.clock[0]+3600
        if gate=='hourly': budget.calls=[p.clock[0]]*4500
        if gate=='daily': budget.calls=[p.clock[0]-4000]*90000
        if gate=='total': budget.total=102060
        db.commit()
    resume(p,group)
    with Session(p.engine) as db:
        assert db.get(MonitorFeed,group).next_poll>p.clock[0]
        assert db.get(MonitorJob,'124').next_run>p.clock[0]


def test_inactive_group_is_not_rescheduled_or_reenabled(p,monkeypatch):
    group,*_=arrange(p,monkeypatch)
    with Session(p.engine) as db:
        db.get(Search,1).enabled=False;db.commit()
    assert p.runner.claim()
    try:
        with Session(p.engine) as db:p.runner.resume_available_quota(db,set())
    finally:p.runner.release('idle')
    with Session(p.engine) as db:
        assert not db.get(Search,1).enabled
        assert db.get(MonitorFeed,group).next_poll>p.clock[0]
