from dataclasses import replace
import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import html_shadow as hs
from backend.models import MonitorControl, MonitorJob, SourceProbe
from backend.tests.test_monitor import p


def cards(*ids):
    return ''.join(f'<section class="ticket-item new__ticket t" data-advertisement-id="{sid}" '
                   'data-user-id="PRIVATE">PRIVATE PHONE<script>PRIVATE</script></section>' for sid in ids)


def setup(p):
    settings = replace(p.settings, ria_html_shadow_run_id='test')
    with Session(p.engine) as db:
        db.get(MonitorControl, 'pilot').heartbeat = p.clock[0]
        db.commit()
    return settings


def test_parser_retains_only_ids():
    assert hs.parse_cards(cards('123','124')) == {'123','124'}
    for html in ('captcha', '<section class="ticket-item"></section>', cards('123','123')):
        with pytest.raises(hs.ProbeError): hs.parse_cards(html)


@pytest.mark.parametrize('rules,allowed', [
    ('User-agent: *\nDisallow: /',False),
    ('User-agent: *\nDisallow: /uk/*',False),
    ('User-agent: *\nDisallow: */api/*',True),
    ('User-agent: *\nDisallow: /\nAllow: /uk/last/hour/',True),
    ('User-agent: AUTODeal\nDisallow:\nUser-agent: *\nDisallow: /',True),
    ('User-agent: AUTODeal\nDisallow: /\nUser-agent: *\nAllow: /',False)])
def test_robots_rules(rules,allowed):
    assert hs.robots_allow(rules,hs.PAGES[0]) is allowed


def test_sample_is_durable_read_only_to_production_and_excludes_baseline(p):
    settings = setup(p)
    statements=[]
    def sql(conn,cursor,statement,*args):
        if statement.lstrip().split()[0].upper() in {'UPDATE','INSERT','DELETE'}:
            statements.append(statement)
    event.listen(p.engine,'before_cursor_execute',sql)
    payload=cards('123','124')
    calls=[]
    def fetch(url):
        calls.append(url)
        return ('User-agent: *\nDisallow: /api/' if url==hs.ROBOTS else payload),100
    hs.tick(p.engine,settings,fetch)
    assert len(calls)==3
    assert all('source_probes' in statement for statement in statements)
    event.remove(p.engine,'before_cursor_execute',sql)
    with Session(p.engine) as db:
        d=db.get(SourceProbe,hs.key(settings)).result
        assert set(d['html'])=={'123','124'} and 'PRIVATE' not in json.dumps(d)
    # Second worker/deploy cannot run the same cycle or reset its deadline.
    hs.tick(p.engine,settings,lambda *a:pytest.fail('duplicate cycle'))
    p.clock[0]+=301
    setup(p)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id='125',first_seen=p.clock[0]-10))
        db.commit()
    payload=cards('124','125')
    hs.tick(p.engine,settings,fetch)
    report=hs.status(p.engine,settings)
    assert report['successful_cycles']==2 and report['unique_html_ids']==3
    assert report['post_baseline_overlaps']==1 and report['api_observed_earlier']==1
    assert report['paid_api_calls']==0 and not report['creates_notifications']
    assert 'html' not in report and 'api' not in report and '125' not in json.dumps(report)


@pytest.mark.parametrize('reason,terminal',[('access_denied',True),('robots_denied',True),('rate_limited',False),('transport_error',False)])
def test_failure_backs_off_or_stops_without_touching_bot(p,reason,terminal):
    settings=setup(p)
    def fail(url):raise hs.ProbeError(reason,900)
    hs.tick(p.engine,settings,fail)
    report=hs.status(p.engine,settings)
    assert report['status']==(reason if terminal else 'backoff')
    assert report['unique_html_ids']==0
    hs.tick(p.engine,settings,lambda *a:pytest.fail('backoff ignored'))


def test_denied_robots_prevents_page_requests_and_expiry_survives_restart(p):
    settings=setup(p)
    def deny(url):
        assert url==hs.ROBOTS
        return 'User-agent: *\nDisallow: /uk/*',40
    hs.tick(p.engine,settings,deny)
    assert hs.status(p.engine,settings)['status']=='robots_denied'
    hs.tick(p.engine,settings,lambda *a:pytest.fail('terminal reset'))


def test_duration_survives_restart(p):
    settings=setup(p)
    hs.tick(p.engine,settings,lambda u:('User-agent: *\nAllow: /' if u==hs.ROBOTS else cards('123'),50))
    ends=hs.status(p.engine,settings)['ends_at']
    p.clock[0]=ends+1
    hs.tick(p.engine,settings,lambda *a:pytest.fail('expired request'))
    assert hs.status(p.engine,settings)['status']=='completed'
    assert hs.status(p.engine,settings)['ends_at']==ends
    assert hs.key(replace(settings,ria_html_shadow_run_id='../../bad')) is None


def test_unhealthy_monitor_yields_without_http(p):
    settings=replace(p.settings,ria_html_shadow_run_id='test')
    hs.tick(p.engine,settings,lambda *a:pytest.fail('monitor priority'))
    assert hs.status(p.engine,settings)['status']=='yielding_to_bot'


@pytest.mark.parametrize('cap,reason', [('MAX_REQUESTS','request_cap'),('MAX_BYTES','byte_cap'),('MAX_IDS','storage_cap')])
def test_caps_stop_run(p,monkeypatch,cap,reason):
    settings=setup(p)
    monkeypatch.setattr(hs,cap,1)
    def fetch(url):
        if cap!='MAX_IDS':pytest.fail('request beyond cap')
        return ('User-agent: *\nAllow: /' if url==hs.ROBOTS else cards('123','124')),50
    hs.tick(p.engine,settings,fetch)
    assert hs.status(p.engine,settings)['status']==reason


def test_three_errors_stop_and_default_off_uses_no_http(p):
    settings=setup(p)
    hs.tick(p.engine,p.settings,lambda *a:pytest.fail('disabled'))
    for _ in range(3):
        setup(p)
        hs.tick(p.engine,settings,lambda *a:(_ for _ in ()).throw(hs.ProbeError('transport_error')))
        p.clock[0]+=901
    assert hs.status(p.engine,settings)['status']=='failed'
    hs.tick(p.engine,settings,lambda *a:pytest.fail('terminal replay'))
