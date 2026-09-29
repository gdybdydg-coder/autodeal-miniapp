"""Saved evidence only; private owner command, durable replies and unchanged /stop."""
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import bot_commands, launch, listing_check as check, quota_management as qm
from backend.app import create_app
from backend.models import (Delivery, DeliveryTiming, Listing, MonitorJob, MonitorMatch,
    MonitorSeen, MonitorWatch, Search, SourceBudget, SourceProbe, User)
from backend.ria_search import RiaSearch
from backend.tests.test_backend import command, SECRET
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_quota_management import q
from backend.tests.test_ria_ai_price import enable


@pytest.mark.parametrize('value', ['40345395', ' 40345395 ',
    'https://auto.ria.com/uk/auto_volkswagen_golf_40345395.html',
    'https://auto.ria.com/auto_volkswagen_golf_40345395.html?utm_source=telegram#photos',
    'http://auto.ria.com/ru/auto_volkswagen_golf_40345395.html',
    'auto.ria.com/uk/auto_volkswagen_golf_40345395.html',
    'https://www.auto.ria.com/uk/auto_volkswagen_golf_40345395.html'])
def test_id_or_official_listing_url(value):
    assert check.parse_id(value) == '40345395'


@pytest.mark.parametrize('value', ['', None, '0', '-1', '00123', '1234567890123', '１２３',
    'https://auto.ria.com.evil.test/auto_a_40345395.html',
    'https://auto.ria.com@evil.test/auto_a_40345395.html',
    'https://name:secret@auto.ria.com/auto_a_40345395.html',
    'https://evil.test/auto_a_40345395.html', 'file:///auto_a_40345395.html',
    'https://auto.ria.com/search/?id=40345395', '40345395 40345396',
    'https://auto.ria.com:bad/auto_a_40345395.html', 'https://[invalid/',
    'https://auto.ria.com/auto_a_40345395.html\n/check 9', 'x'*1025])
def test_invalid_input_is_not_guessed_or_fetched(value):
    assert check.parse_id(value) is None


def discover(p):
    drain(p)
    p.ads['124'] = p.clock[0]+1
    wake(p)
    drain(p)


def test_unconfirmed_quote_has_clear_reason_without_market_formula_or_calls(p, monkeypatch):
    enable(p, monkeypatch, response={})
    p.runner.settings = replace(p.runner.settings, ria_confirmed_deals_only=True)
    discover(p)
    before = len(p.calls)
    with Session(p.engine) as db:
        text = check.explain(db, 111, '124')
        assert 'Придатну ринкову оцінку не підтверджено' in text
        assert 'Запису про надсилання у твій чат немає' in text
        assert '0.95' not in text and 'мінус 5%' not in text
        assert launch.listing_trace(db, 111, '124')['subscriptions'][0]['state'] == 'market_unconfirmed'
    assert len(p.calls) == before and not p.sent


def test_below_threshold_explains_saved_min_discount(p):
    p.prices['124'] = 14900
    discover(p)
    with Session(p.engine) as db:
        text = check.explain(db, 111, '124')
        assert 'нижча від порога' in text and 'Поріг пошуку: 15%' in text


@pytest.mark.parametrize('change,reason', [({'price_usd': 0}, 'invalid_price'),
    ({'condition_exclusions': ['abroad']}, 'source_exclusion'),
    ({'condition_exclusions': ['custom']}, 'source_exclusion'),
    ({'fuel_id': 1}, 'filter_mismatch'), ({'region_id': 99}, 'filter_mismatch')])
def test_saved_exclusions_and_known_filter_conflicts(p, change, reason):
    discover(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, '124')
        job.result = {**job.result, 'candidate': {**job.result['candidate'], **change}}
        match = db.scalar(select(MonitorMatch))
        db.delete(match)
        db.commit()
        report = launch.listing_trace(db, 111, '124')
        assert report['subscriptions'][0]['state'] == reason
        if reason == 'filter_mismatch':
            assert report['subscriptions'][0]['reason'] in {'filter_fuel', 'filter_region'}


def test_optional_missing_details_and_damage_do_not_become_exclusion_reasons(p):
    discover(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, '124')
        job.result = {**job.result, 'candidate': {**job.result['candidate'], 'fuel_id': None,
            'gear_id': None, 'condition_exclusions': ['damage', 'technical_condition', 'onRepairParts']}}
        db.delete(db.scalar(select(MonitorMatch)))
        db.commit()
        entry = launch.listing_trace(db, 111, '124')['subscriptions'][0]
        assert entry['state'] == 'checked_without_match'
        assert entry['reason'] is None


@pytest.mark.parametrize('field', ['epoch', 'fingerprint'])
def test_retired_match_is_not_reported_as_current_match(p, field):
    discover(p)
    with Session(p.engine) as db:
        setattr(db.scalar(select(MonitorMatch)), field, 'retired')
        db.commit()
        assert launch.listing_trace(db, 111, '124')['subscriptions'][0]['state'] != 'matched'


@pytest.mark.parametrize('state', ['pending', 'sending', 'sent', 'uncertain', 'failed', 'cancelled'])
def test_delivery_outcome_is_explicit_and_diagnostic_never_replays(p, state):
    discover(p)
    with Session(p.engine) as db:
        receipt = db.scalar(select(Delivery))
        receipt.state = state
        db.commit()
        before = db.get(SourceBudget, 'auto_ria').total
        epoch = db.get(MonitorWatch, 1).epoch
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().split()[0].upper())
        event.listen(p.engine, 'before_cursor_execute', capture)
        try:
            text = check.explain(db, 111, '124')
        finally:
            event.remove(p.engine, 'before_cursor_execute', capture)
        assert check.DELIVERY[state] in text
        assert set(statements) == {'SELECT'}
        assert db.get(SourceBudget, 'auto_ria').total == before
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(Delivery, receipt.id).state == state
    assert len(p.sent) == 1


def test_unobserved_owner_gets_no_other_users_job_or_receipt(p):
    discover(p)
    with Session(p.engine) as db:
        text = check.explain(db, 222, '124')
        assert 'немає збереженого запису' in text and 'точну причину пропуску встановити не можна' in text
        assert 'підтвердив прийняття' not in text and 'Golf' not in text
        assert launch.listing_trace(db, 222, '124') == {'source_id': '124', 'state': 'not_observed', 'subscriptions': []}


def test_receipt_survives_deleted_subscription_history(p):
    discover(p)
    with Session(p.engine) as db:
        db.delete(db.get(MonitorSeen, (1, '124')))
        db.commit()
        text = check.explain(db, 111, '124')
        assert 'немає збереженого запису' in text
        assert 'Telegram підтвердив прийняття' in text
        assert 'Прийнято Telegram:' in text


def test_private_command_is_durable_deduplicated_and_costs_no_source_requests(q, monkeypatch):
    engine, settings, api = q
    monkeypatch.setattr(RiaSearch, 'request', lambda *a, **kw: pytest.fail('paid source call'))
    for _ in range(2):
        assert command(api, '/check https://auto.ria.com/uk/auto_vw_golf_40345395.html', update=10).status_code == 200
    calls = []
    def accepted(token, method, payload):
        assert method == 'sendMessage' and payload['chat_id'] == 111
        calls.append(payload)
        return {'ok': True, 'result': {'message_id': 41}}
    assert check.deliver_one(engine, settings, accepted) == 'sent'
    assert check.deliver_one(engine, settings, lambda *a: pytest.fail('duplicate')) == 'idle'
    assert len(calls) == 1 and '40345395' in calls[0]['text']
    with Session(engine) as db:
        assert db.get(SourceBudget, 'auto_ria').total == 80000
        assert db.get(SourceProbe, check.OUTBOX+'10').status == 'sent'
        assert db.get(User, 111).last_update == -1


@pytest.mark.parametrize('kind', ['other_user', 'group', 'forged_chat', 'bot', 'wrong_secret', 'wrong_mention'])
def test_check_authorization_precedes_any_private_read(q, monkeypatch, kind):
    engine, _, api = q
    monkeypatch.setattr(check, 'explain', lambda *a: pytest.fail('private read'))
    msg = {'from': {'id': 111}, 'chat': {'id': 111, 'type': 'private'}, 'text': '/check 40345395', 'date': int(time.time())}
    secret = SECRET
    if kind == 'other_user': msg['from']['id'] = msg['chat']['id'] = 222
    if kind == 'group': msg['chat']['type'] = 'group'
    if kind == 'forged_chat': msg['chat']['id'] = 222
    if kind == 'bot': msg['from']['is_bot'] = True
    if kind == 'wrong_secret': secret = 'wrong'
    if kind == 'wrong_mention': msg['text'] = '/check@another_bot 40345395'
    result = api.post('/telegram/webhook', headers={'X-Telegram-Bot-Api-Secret-Token': secret},
                      json={'update_id': 10, 'message': msg})
    assert result.status_code == (403 if kind == 'wrong_secret' else 200)
    with Session(engine) as db:
        assert db.get(SourceProbe, check.OUTBOX+'10') is None


def test_check_does_not_mask_delayed_stop_or_reenable_stopped_owner(q):
    engine, settings, api = q
    now = int(time.time())
    with Session(engine) as db:
        db.add_all([Search(id=1, user_id=111, name='x', fingerprint='f', filters={}, enabled=True),
                    MonitorWatch(search_id=1, epoch='a')])
        db.commit()
    command(api, '/check 40345395', update=11, date=now)
    command(api, '/stop', update=10, date=now-1)
    assert check.deliver_one(engine, settings, lambda *a: {'ok': True, 'result': {'message_id': 1}}) == 'sent'
    with Session(engine) as db:
        assert not db.get(User, 111).ready and not db.get(Search, 1).enabled
        assert db.get(MonitorWatch, 1) is None


@pytest.mark.parametrize('failure', ['timeout', 'unknown', 'crashed_claim'])
def test_check_reply_never_replays_uncertain_send(q, failure):
    engine, settings, api = q
    command(api, '/check 40345395', update=10)
    if failure == 'crashed_claim':
        with Session(engine) as db:
            db.get(SourceProbe, check.OUTBOX+'10').status = 'sending'
            db.commit()
    else:
        def send(*args):
            if failure == 'timeout': raise TimeoutError()
            return {'uncertain': True}
        assert check.deliver_one(engine, settings, send) == 'uncertain'
    assert check.deliver_one(engine, settings, lambda *a: pytest.fail('replayed')) == 'idle'


@pytest.mark.parametrize('text,age', [('/check', 0), ('/check https://evil.test/?id=40345395', 0),
    ('/check 40345395', 301), ('/check 40345395', -31)])
def test_bad_or_stale_command_does_not_read_listing(q, monkeypatch, text, age):
    now = time.time()
    monkeypatch.setattr(check.time, 'time', lambda: now)
    monkeypatch.setattr(check, 'explain', lambda *a: pytest.fail('unexpected lookup'))
    command(q[2], text, update=10, date=int(time.time())-age)
    with Session(q[0]) as db:
        assert db.get(SourceProbe, check.OUTBOX+'10').status == 'pending'


def test_owner_menu_upgrades_existing_config_once(q):
    engine, settings, _ = q
    with Session(engine) as db:
        db.add(SourceProbe(id='telegram-webhook-v1', status='configured', checked_at=time.time(), result={}))
        db.add(SourceProbe(id='telegram-quota-owner-menu-v1', status='configured', checked_at=time.time(), result={'uid': 111}))
        db.commit()
    calls = []
    def send(token, method, payload):
        calls.append(payload)
        return {'ok': True, 'result': True}
    qm.configure(engine, settings, send)
    qm.configure(engine, settings, send)
    assert len(calls) == 1 and calls[0]['scope'] == {'type': 'chat', 'chat_id': 111}
    assert any(item['command'] == 'check' for item in calls[0]['commands'])
    assert qm.public_status(engine, settings)['listing_check_command']


def test_many_subscriptions_fit_one_telegram_reply(p):
    discover(p)
    with Session(p.engine) as db:
        for sid in range(2, 18):
            db.add_all([Search(id=sid, user_id=111, name='🚘'*60, filters={}, fingerprint=str(sid), enabled=True),
                MonitorWatch(search_id=sid, epoch='a'),
                MonitorSeen(search_id=sid, source_id='124', epoch='a', state='pending', first_seen=p.clock[0])])
        db.get(MonitorJob, '124').reason = 'reserved_for_new_publications'
        db.commit()
        text = check.explain(db, 111, '124')
        assert 'Ще пошуків із записами: 9.' in text
        assert len(text.encode('utf-16-le'))//2 <= 4096


def test_owner_reply_loop_runs_without_quota_feature_or_webhook_setup(q, monkeypatch):
    started = []
    async def fake_loop(engine, settings, stop):
        started.append(True)
        await stop.wait()
    monkeypatch.setattr(bot_commands, 'run', fake_loop)
    with TestClient(create_app(replace(q[1], ria_quota_management_enabled=False, configure_webhook=False), q[0])):
        assert started
