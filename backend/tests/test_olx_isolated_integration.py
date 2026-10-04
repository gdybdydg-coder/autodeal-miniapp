"""Current production policy with TEMP databases and local fake OLX/receipts."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend import paid_source_access
from backend.billing_models import Base as BillingBase, Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend.models import Base, Filters, Search, User
from backend.olx_test_adapter import IsolatedOLX, Switch, TEST_ENGINE
from experiments.olx_offline.pipeline import Pipeline, canonical
from experiments.olx_offline.test_pipeline import NOW, raw, comps


@pytest.fixture
def bench(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'backend.sqlite'))
    Base.metadata.create_all(engine)
    ManualBase.metadata.create_all(engine)
    BillingBase.metadata.create_all(engine)
    engine.update_execution_options(**{TEST_ENGINE: True})
    paid_source_access.configure(engine, SimpleNamespace(admin_telegram_id=900,
                                  stats_excluded_user_ids='901'))
    clock = [NOW]
    pipeline = Pipeline(tmp_path / 'olx.sqlite')
    switch = Switch()
    integration = IsolatedOLX(engine, pipeline, switch=switch, clock=lambda: clock[0])
    yield SimpleNamespace(engine=engine, pipeline=pipeline, switch=switch,
                          integration=integration, clock=clock)
    pipeline.db.close()
    engine.dispose()


def client(b, uid=100, *, sid=None, paid=True, ready=True, enabled=True, filters=None):
    sid = uid if sid is None else sid
    f = Filters.model_validate(filters or {})
    with Session(b.engine) as db:
        db.merge(User(id=uid, ready=ready))
        db.add(Search(id=sid, user_id=uid, name='isolated test', filters=f.canonical(),
                      fingerprint=f.fingerprint(), enabled=enabled))
        if paid:
            db.merge(PaymentRequest(id='test-' + str(uid), user_id=uid, state='approved',
                amount_minor=25000, currency='UAH', days=30, created_at=NOW-10,
                updated_at=NOW-10, expires_at=NOW+1000))
            db.merge(Entitlement(user_id=uid, updated_at=NOW-10, expires_at=NOW+1000))
        db.commit()
    return sid


def cycle(b, *, mutate=None, peer_rows=None, rows=None):
    return b.integration.run_cycle(lambda _: {'items': rows or [raw('123')], 'next': None},
                                  comps() if peer_rows is None else peer_rows,
                                  before_dispatch=mutate)


def activate(b, *searches):
    b.switch.enabled = True
    b.switch.opted_searches.update(searches)


def test_default_off_and_no_automatic_old_search_optin(bench):
    client(bench)
    def fail(_):
        pytest.fail('Disabled OLX must not collect')
    for enabled in (False, True):
        bench.switch.enabled = enabled
        result = bench.integration.run_cycle(fail, comps())
        assert result['source_calls'] == result['local_receipts'] == 0


@pytest.mark.parametrize('state', ['none', 'review', 'expired', 'revoked', 'gift', 'future', 'zero', 'service', 'stopped', 'disabled'])
def test_nonbuyers_or_disallowed_searches_do_not_collect(bench, state):
    uid = 901 if state == 'service' else 100
    sid = client(bench, uid, paid=state not in ('none', 'gift'),
                 ready=state != 'stopped', enabled=state != 'disabled')
    with Session(bench.engine) as db:
        p = db.get(PaymentRequest, 'test-' + str(uid))
        if state == 'review': p.state = 'review'
        if state == 'expired': p.expires_at = NOW
        if state == 'future': p.updated_at = NOW+1
        if state == 'zero': p.amount_minor = 0
        if state == 'revoked': db.get(Entitlement, uid).expires_at = NOW
        if state == 'gift': db.add(Entitlement(user_id=uid, expires_at=NOW+1000, updated_at=NOW))
        db.commit()
    activate(bench, sid)
    def fail(_): pytest.fail('Disallowed paid planning must not invoke source')
    assert bench.integration.run_cycle(fail, comps())['source_calls'] == 0


def test_unpaid_first_paid_owner_and_two_clients_share_one_collection(bench):
    sids = [client(bench, 10, paid=False), client(bench, 100),
            client(bench, 200), client(bench, 900)]
    activate(bench, *sids)
    writes = []
    def inspect_sql(conn, cursor, statement, *args):
        if statement.lstrip().split(' ', 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(statement)
    event.listen(bench.engine, 'before_cursor_execute', inspect_sql)
    result = cycle(bench)
    assert result['source_calls'] == 1
    assert result['local_receipts'] == 3
    assert sorted(m['user'] for m in bench.integration.receipts.messages) == ['100', '200', '900']
    assert not writes  # OLX did not change payments, filters, quotas or RIA rows.
    assert result['AUTO_RIA_requests'] == result['actual_Telegram_sends'] == 0


@pytest.mark.parametrize('mutation', ['stop', 'expiry', 'disable_search', 'remove_optin', 'kill_switch', 'filter', 'threshold'])
def test_current_policy_and_search_rechecked_before_fake_transport(bench, mutation):
    sid = client(bench)
    activate(bench, sid)
    def change():
        if mutation == 'remove_optin': bench.switch.opted_searches.clear()
        elif mutation == 'kill_switch': bench.switch.enabled = False
        else:
            with Session(bench.engine) as db:
                if mutation == 'stop': db.get(User, 100).ready = False
                elif mutation == 'expiry': db.get(PaymentRequest, 'test-100').expires_at = NOW
                elif mutation == 'disable_search': db.get(Search, sid).enabled = False
                else:
                    f = Filters(brand='Toyota') if mutation == 'filter' else Filters(minDiscount=90.0)
                    s = db.get(Search, sid)
                    s.filters, s.fingerprint = f.canonical(), f.fingerprint()
                db.commit()
    result = cycle(bench, mutate=change)
    assert result['local_receipts'] == 0
    assert result['queue']['queued'] == 1


def test_refuses_unlabelled_or_non_strict_engines(bench):
    other = create_engine('sqlite:///:memory:')
    with pytest.raises(ValueError, match='labelled'):
        IsolatedOLX(other, bench.pipeline, clock=lambda: NOW)
    other.update_execution_options(**{TEST_ENGINE: True})
    with pytest.raises(ValueError, match='purchase'):
        IsolatedOLX(other, bench.pipeline, clock=lambda: NOW)
    other.dispose()


def test_unknown_valuation_and_forbidden_cars_not_deals(bench):
    sid = client(bench)
    activate(bench, sid)
    rows = [raw('123', generation=None), raw('124', customs_status='uncleared'),
            raw('125', description='Під розбір по запчастинах')]
    result = cycle(bench, rows=rows)
    assert result['local_receipts'] == 0
    assert len(bench.pipeline.cars()) == 3


def test_OLX_does_not_use_injected_ria_valuations(bench):
    sid = client(bench)
    activate(bench, sid)
    peers = [dict(car, source='auto_ria') for car in comps()]
    result = cycle(bench, peer_rows=peers)
    assert result['local_receipts'] == 0
    assert bench.pipeline.assessment_summary()['unknown'] == 1


def test_200_paid_users_share_source_work_no_administrator_copies(bench):
    for uid in range(1000, 1200):
        activate(bench, client(bench, uid))
    result = cycle(bench)
    assert result['source_calls'] == 1
    assert result['local_receipts'] == 200
    assert len({m['user'] for m in bench.integration.receipts.messages}) == 200
    assert all(m['transport'] == 'local_fake' for m in bench.integration.receipts.messages)


def test_missing_optional_filter_fields_does_not_block_all_new_mode(bench):
    sid = client(bench, filters={'onlyDeals': False, 'fuel': ['Дизель'],
                                'transmission': ['Автоматична']})
    activate(bench, sid)
    result = cycle(bench, rows=[raw('123', fuel=None, transmission=None, photos=[])], peer_rows=[])
    assert result['local_receipts'] == 1
    assert bench.integration.receipts.messages[0]['card']['photo'] is None
    assert 'Вигідність не підтверджена' in bench.integration.receipts.messages[0]['card']['text']


@pytest.mark.parametrize('mutation', ['expiry', 'stop', 'switch'])
def test_collect_rechecks_each_page_and_queue_access(bench, mutation):
    sid = client(bench)
    activate(bench, sid)
    calls = []
    def fetch(cursor):
        calls.append(cursor)
        if mutation == 'switch': bench.switch.enabled = False
        else:
            with Session(bench.engine) as db:
                if mutation == 'expiry': db.get(PaymentRequest, 'test-100').expires_at = NOW
                else: db.get(User, 100).ready = False
                db.commit()
        return {'items': [raw('123')], 'next': 'two'}
    result = bench.integration.run_cycle(fetch, comps())
    assert calls == [None]
    assert result['local_receipts'] == result['queue']['queued'] == 0
    assert result['collection']['status'] == 'incomplete'


def test_card_contains_saved_independent_valuation(bench):
    activate(bench, client(bench))
    result = cycle(bench)
    assert result['local_receipts'] == 1
    text = bench.integration.receipts.messages[0]['card']['text']
    assert 'OLX' in text and '12' in text
    assert 'AUTO.RIA' not in text


def test_unsupported_first_paid_search_does_not_poison_valid_paid_search(bench):
    activate(bench, client(bench, 10, filters={'brand': '84'}), client(bench, 100))
    result = cycle(bench)
    assert result['source_calls'] == result['local_receipts'] == 1
    assert result['plan']['held_searches'] == 1
    assert bench.integration.search_holds == {10: 'stored_filter_mapping_unavailable'}
    assert [m['user'] for m in bench.integration.receipts.messages] == ['100']


def test_access_read_failure_is_technical_hold_without_source_call(bench, monkeypatch):
    from sqlalchemy.exc import OperationalError
    activate(bench, client(bench))
    def fail(*_): raise OperationalError('SELECT', {}, RuntimeError('test unavailable'))
    monkeypatch.setattr(paid_source_access, 'allowed', fail)
    result = cycle(bench)
    assert result['status'] == 'access_temporarily_unavailable'
    assert result['source_calls'] == result['local_receipts'] == 0


def test_shared_synthetic_FX_conversion_and_rate_change_not_new_alert(bench):
    from datetime import datetime, timezone
    from decimal import Decimal
    from experiments.olx_offline.fx import FXQuote, KYIV, nbu_url
    activate(bench, client(bench))
    current = datetime.fromtimestamp(NOW, timezone.utc)
    day = current.astimezone(KYIV).date()
    def quote(rate): return FXQuote(Decimal(rate), day, current, nbu_url(day))
    def fetch(_): return {'items': [raw('123', price=294000, currency='UAH')], 'next': None}
    result = bench.integration.run_cycle(fetch, comps(), fx_quote=quote('42'))
    assert result['local_receipts'] == 1
    text = bench.integration.receipts.messages[0]['card']['text']
    assert '≈' in text and '294 000' in text and 'НБУ' in text
    changed = bench.integration.run_cycle(fetch, comps(), fx_quote=quote('40'))
    assert changed['local_receipts'] == 0
    saved = bench.pipeline.cars()[0]
    assert saved['price'] == '294000' and saved['usd_price']['usd_amount'] == '7350'
