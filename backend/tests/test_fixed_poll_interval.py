"""All-day target, restart checkpoints and unchanged request authorization."""
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest

from backend import poll_schedule
from backend.models import Delivery, MonitorFeed, MonitorWatch, SourceBudget
from backend.monitor import Monitor
from backend.ria_budget import BudgetLimits
from backend.tests.test_monitor import p, drain, searches
from backend.tests.test_poll_schedule import configured, utc


@pytest.mark.parametrize('instant', ['2026-10-27T00:00:00+02:00',
    '2026-10-27T08:00:00+02:00','2026-10-27T18:00:00+02:00',
    '2026-10-27T23:00:00+02:00','2026-10-25T00:30:00+00:00',
    '2026-10-25T01:30:00+00:00'])
def test_fixed_40_across_all_periods_and_dst(monkeypatch, instant):
    monkeypatch.setenv('RIA_POLL_INTERVAL_SECONDS','40')
    report=poll_schedule.policy(74,BudgetLimits(60000,1102160,1102160),utc(instant))
    assert report['interval_seconds']==40 and not report['budget_limited']
    assert [p['interval_seconds'] for p in report['periods']]==[40,40,40]
    assert report['mode']=='fixed_interval'
    assert report['minimum_planning_limits']['hourly']==8880
    assert report['requested_search_calls_per_day']==159840


@pytest.mark.parametrize('value',['','0','-1','29','40.5','3601','true'])
def test_invalid_fixed_interval_rejected(monkeypatch,value):
    monkeypatch.setenv('RIA_POLL_INTERVAL_SECONDS',value)
    with pytest.raises(ValueError):
        poll_schedule.policy(1,BudgetLimits(4500,90000,1102160))


def test_fixed_target_keeps_rate_caps(monkeypatch):
    monkeypatch.setenv('RIA_POLL_INTERVAL_SECONDS','40')
    report=poll_schedule.policy(74,BudgetLimits(4500,90000,1102160))
    assert report['requested_interval_seconds']==40
    assert report['interval_seconds']>40 and report['budget_limited']
    assert 74*86400/report['interval_seconds']<=90000*.75


def test_live_switch_retains_cursor_receipts_and_hard_quota(p,monkeypatch):
    configured(p,monkeypatch,'2026-09-25T20:00:00+00:00')
    drain(p)
    with Session(p.engine) as db:
        feed=db.scalar(select(MonitorFeed))
        epoch=db.get(MonitorWatch,1).epoch
        cursor=feed.cursor
        assert feed.next_poll-feed.checked_at==3600
    monkeypatch.setenv('RIA_POLL_INTERVAL_SECONDS','40')
    p.ads['124']=p.clock[0]+1
    p.clock[0]+=41
    drain(p)
    assert [car.source_id for _,car in p.sent]==['124']
    with Session(p.engine) as db:
        feed=db.scalar(select(MonitorFeed))
        assert feed.cursor>=cursor and feed.next_poll-feed.checked_at==40
        assert db.get(MonitorWatch,1).epoch==epoch
        assert db.scalar(select(Delivery)).state=='sent'
        used=db.get(SourceBudget,'auto_ria').total
    p.runner=Monitor(p.engine,p.runner.settings,p.runner.search_factory,p.runner.sender)
    drain(p)
    assert len(p.sent)==1
    before=len(searches(p))
    with Session(p.engine) as db:
        assert db.get(SourceBudget,'auto_ria').total==used
        db.get(SourceBudget,'auto_ria').total=90000
        db.commit()
    p.ads['125']=p.clock[0]+1
    p.clock[0]+=41
    drain(p)
    assert len(searches(p))==before and len(p.sent)==1
