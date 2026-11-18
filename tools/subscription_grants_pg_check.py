"""Real PostgreSQL acceptance tests in a new, retained local database only.

Run: python tools/subscription_grants_pg_check.py --cluster-root ../.local-postgres
The database host/port are deliberately fixed; no production DSN is accepted.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app import Settings
from backend import billing, radar_monitor, radar_service, subscription_grants as grants
from backend.billing_models import AccessEvent, BillingControl, BillingOrder, Entitlement
from backend.manual_payment_models import ManualBase, PaymentNotice, PaymentRequest
from backend.models import (Base, Delivery, MonitorMembership, MonitorWatch, Search,
                            SourceBudget, StarsTestOrder, User)
from backend.radar_catalog import import_catalog
from backend.radar_models import MIGRATION_VERSION, Radar, RadarMigration, RadarSearch

OWNER = 99001
DAY = 86400


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cluster-root', required=True)
    options = parser.parse_args()
    cluster = Path(options.cluster_root).resolve()
    password = (cluster / 'password.txt').read_text(encoding='utf-8').strip()
    url = URL.create('postgresql+psycopg', username='pilot', password=password,
                     host='127.0.0.1', port=55439, database='postgres')
    name = 'autodeal_grants_' + uuid.uuid4().hex[:12]
    report = {'database': name, 'host': '127.0.0.1', 'port': 55439,
              'provider_requests': 0, 'telegram_sends': 0, 'checks': [], 'passed': False}
    attempts = []
    original_connect = socket.socket.connect
    original_resolve = socket.getaddrinfo

    def connect(sock, address):
        if isinstance(address, tuple) and address[:2] == ('127.0.0.1', 55439):
            return original_connect(sock, address)
        attempts.append('network_connect')
        raise AssertionError('Only the isolated PostgreSQL connection is permitted')

    def resolve(host, port, *args, **kwargs):
        if host == '127.0.0.1' and int(port) == 55439:
            return original_resolve(host, port, *args, **kwargs)
        attempts.append('dns')
        raise AssertionError('No external DNS is permitted')

    def passed(label):
        report['checks'].append(label)
        print(label + ' PASS', flush=True)

    def denied_transport(*_args, **_kwargs):
        attempts.append('telegram_transport')
        raise AssertionError('A grant must not send Telegram messages')

    engine = None
    try:
        with patch.dict(os.environ, {'SUBSCRIPTION_EXPECTED_ADMIN_ID': str(OWNER)}), \
                patch('socket.socket.connect', connect), patch('socket.getaddrinfo', resolve), \
                patch('backend.telegram_setup.call', denied_transport):
            admin = create_engine(url, isolation_level='AUTOCOMMIT', poolclass=NullPool)
            try:
                with admin.connect() as db:
                    db.execute(text('CREATE DATABASE ' + name))
            finally:
                admin.dispose()
            engine = create_engine(url.set(database=name), pool_size=10, max_overflow=2,
                connect_args={'options': '-c statement_timeout=20000 -c lock_timeout=15000 -c jit=off'})
            Base.metadata.create_all(engine)
            ManualBase.metadata.create_all(engine)
            settings = Settings('unused-local-harness', 'test-only-token', 'test-only-secret-' * 4,
                False, False, admin_telegram_id=OWNER, radar_enabled=True,
                manual_payment_review_enabled=True)
            now = time.time()
            users = range(501, 509)
            with Session(engine) as db, db.begin():
                db.add_all([User(id=OWNER, ready=True), *[User(id=uid, ready=True) for uid in users]])
                db.add(BillingControl(id=billing.CONTROL, version=1, enforce=True, sales=False, offer={}))
                db.add(RadarMigration(version=MIGRATION_VERSION, applied_at=now))
                db.add(SourceBudget(id='auto_ria', total=123, calls=[]))
                db.flush()
                db.add_all(Entitlement(user_id=uid, expires_at=now+3600, updated_at=now-10) for uid in users)
                import_catalog(db, {'brands': [{'id': 9, 'name': 'BMW', 'models_available': True,
                    'models': [{'id': 3219, 'name': '3 Series'}]}], 'regions': [{'id': 10, 'name': 'Київська'}]})
            radars = {}
            for uid in (504, 505, 506, 507, 508):
                with Session(engine) as db, db.begin():
                    payload = radar_service.RadarCreate(name='Тестовий радар', brands=[{'brand_id': 9, 'model_ids': [3219]}],
                        price_category=3, region_ids=[10], year_from=2010, year_to=2026, min_profit=10)
                    value = radar_service.create(db, uid, payload)
                    if uid != 507:
                        value = radar_service.set_status(db, uid, value['id'],
                            radar_service.RadarStatus(active=True, revision=value['revision']))
                    radars[uid] = value['id']
            with Session(engine) as db, db.begin():
                for uid in (504, 505, 508):
                    db.get(Entitlement, uid).expires_at = now-60
                    # UID508 has an inconsistent future activation ceiling:
                    # pre-grant entitlement expiry must still pause it.
                    if uid != 508:
                        db.get(Radar, radars[uid]).active_until = now-60

            def preview(uid, days=7, suffix=''):
                return grants.preview(engine, settings, OWNER, uid, days,
                    'Безоплатний доступ: ізольована перевірка' + suffix, now=now)

            def confirm(value):
                try:
                    return {'result': grants.confirm(engine, settings, OWNER,
                        value['confirmation_token'], now=now)}
                except grants.GrantError as exc:
                    return {'error': exc.code, 'status': exc.status}

            def count_rows(db, model):
                return db.scalar(select(func.count()).select_from(model))

            def payment_counts():
                with Session(engine) as db:
                    return {model.__tablename__: count_rows(db, model)
                            for model in (PaymentRequest, BillingOrder, StarsTestOrder, PaymentNotice, Delivery)}

            initial_payment_counts = payment_counts()
            one = preview(501)
            barrier = threading.Barrier(8)
            def identical(_):
                barrier.wait(timeout=15)
                return confirm(one)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(identical, range(8)))
            assert all('result' in item for item in results), results
            assert sum(not item['result']['replayed'] for item in results) == 1, results
            assert len({item['result']['expires_at'] for item in results}) == 1
            with Session(engine) as db:
                assert db.get(Entitlement, 501).expires_at == now+3600+7*DAY
                assert count_rows(db, AccessEvent) == 1
                record = db.scalar(select(AccessEvent).where(AccessEvent.user_id == 501))
                assert record.kind == 'gift' and record.actor == OWNER
            delayed = grants.confirm(engine, settings, OWNER, one['confirmation_token'],
                                     now=now+grants.TOKEN_TTL+10)
            assert delayed['replayed'] and delayed['expires_at'] == now+3600+7*DAY
            passed('eight_identical_confirms_extend_exactly_once_and_keep_one_gift_audit')

            proposals = [preview(502, 7, ' A'), preview(502, 30, ' B')]
            barrier = threading.Barrier(2)
            def distinct(value):
                barrier.wait(timeout=15)
                return confirm(value)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(distinct, proposals))
            assert sum('result' in item for item in results) == 1, results
            assert sum(item.get('error') == 'access_changed' and item.get('status') == 409 for item in results) == 1
            with Session(engine) as db:
                until = db.get(Entitlement, 502).expires_at
                assert until in (now+3600+7*DAY, now+3600+30*DAY)
                assert db.scalar(select(func.count()).select_from(AccessEvent).where(AccessEvent.user_id == 502)) == 1
            passed('competing_previews_conflict_instead_of_double_extending_or_shortening')

            failed = preview(504)
            def fail_audit(_conn, _cursor, statement, _params, _context, _many):
                if statement.lstrip().upper().startswith('INSERT INTO BILLING_ACCESS_EVENTS'):
                    raise RuntimeError('injected_gift_audit_failure')
            event.listen(engine, 'before_cursor_execute', fail_audit)
            try:
                try:
                    grants.confirm(engine, settings, OWNER, failed['confirmation_token'], now=now)
                except RuntimeError as exc:
                    assert str(exc) == 'injected_gift_audit_failure'
                else:
                    raise AssertionError('Injected audit failure did not abort the transaction')
            finally:
                event.remove(engine, 'before_cursor_execute', fail_audit)
            with Session(engine) as db:
                assert db.get(Entitlement, 504).expires_at == now-60
                assert db.get(Radar, radars[504]).status == 'active'
                assert db.scalar(select(func.count()).select_from(AccessEvent).where(AccessEvent.user_id == 504)) == 0
                sid = db.scalar(select(RadarSearch.search_id).where(RadarSearch.radar_id == radars[504]))
                assert db.get(Search, sid).enabled and db.get(MonitorWatch, sid) is not None
            assert 'result' in confirm(failed)
            passed('audit_failure_rolls_back_entitlement_radar_pause_and_watch_changes_then_safe_retry')

            before_configs = {}
            active_execution = {}
            with Session(engine) as db:
                for uid in (505, 506, 507, 508):
                    radar = db.get(Radar, radars[uid])
                    config = radar_service.config(db, radar)
                    before_configs[uid] = {key: config[key] for key in
                        ('name', 'brands', 'regions', 'price_category', 'year_from', 'year_to', 'min_profit', 'slot')}
                for sid in db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id == radars[506])):
                    watch, member = db.get(MonitorWatch, sid), db.get(MonitorMembership, sid)
                    active_execution[sid] = (watch.epoch, member.feed_id, member.started_at)
            for uid in (505, 506, 507, 508):
                assert 'result' in confirm(preview(uid))
            with Session(engine) as db:
                for uid in (505, 506, 507, 508):
                    radar = db.get(Radar, radars[uid])
                    config = radar_service.config(db, radar)
                    assert {key: config[key] for key in before_configs[uid]} == before_configs[uid]
                    expected = 'active' if uid == 506 else 'paused' if uid == 507 else 'subscription_expired'
                    assert radar.status == expected, (uid, radar.status)
                    ids = list(db.scalars(select(RadarSearch.search_id).where(RadarSearch.radar_id == radar.id)))
                    assert ids and all(db.get(Search, sid).enabled == (uid == 506) for sid in ids)
                    if uid == 506:
                        assert radar.active_until == db.get(Entitlement, uid).expires_at
                        for sid in ids:
                            watch, member = db.get(MonitorWatch, sid), db.get(MonitorMembership, sid)
                            assert (watch.epoch, member.feed_id, member.started_at) == active_execution[sid]
                    else:
                        assert all(db.get(MonitorWatch, sid) is None for sid in ids)
                    assert db.get(User, uid).ready is True
            assert payment_counts() == initial_payment_counts
            passed('expired_radars_stay_paused_active_periods_extend_and_no_payment_or_delivery_rows_created')

            # The actual paid-purchase path takes the same control-row lock.
            # Let it win while a grant waits; the stale grant must then fail.
            value = preview(503)
            with Session(engine) as db, db.begin():
                db.add(BillingOrder(id='ad-sub:grant-race', user_id=503, update_id=12345,
                    amount=250, terms_version='isolated-test', created_at=now, state='pending'))
            paid_locked, release_paid, grant_entered = threading.Event(), threading.Event(), threading.Event()
            paid_thread = []
            original_control = billing.control
            def controlled(db, *, lock=False):
                result = original_control(db, lock=lock)
                if lock and paid_thread == [threading.get_ident()]:
                    paid_locked.set()
                    assert release_paid.wait(timeout=10)
                return result
            def paid_purchase():
                paid_thread.append(threading.get_ident())
                data = {'invoice_payload': 'ad-sub:grant-race', 'currency': 'XTR',
                        'total_amount': 250, 'telegram_payment_charge_id': 'isolated-grants-race-charge'}
                return billing.apply_payment(engine, 503, data, now)
            def waiting_grant():
                grant_entered.set()
                return confirm(value)
            with patch.object(billing, 'control', controlled), ThreadPoolExecutor(max_workers=2) as pool:
                paid = pool.submit(paid_purchase)
                try:
                    assert paid_locked.wait(timeout=10)
                    waiting = pool.submit(waiting_grant)
                    assert grant_entered.wait(timeout=5)
                finally:
                    release_paid.set()
                assert paid.result(timeout=20)
                result = waiting.result(timeout=20)
            assert result == {'error': 'access_changed', 'status': 409}, result
            with Session(engine) as db:
                paid_expiry = db.get(BillingOrder, 'ad-sub:grant-race').expires_at
                assert db.get(Entitlement, 503).expires_at == paid_expiry == now+3600+30*DAY
                events = list(db.scalars(select(AccessEvent).where(AccessEvent.user_id == 503)))
                assert len(events) == 1 and events[0].kind == 'paid'
            passed('real_paid_renewal_and_grant_serialize_without_overwriting_or_shortening_access')
            with Session(engine) as db:
                assert db.get(SourceBudget, 'auto_ria').total == 123
                assert count_rows(db, PaymentRequest) == 0
                assert count_rows(db, PaymentNotice) == 0 and count_rows(db, Delivery) == 0
            assert attempts == [], attempts
            report['passed'] = True
    finally:
        if engine is not None:
            engine.dispose()
        report['blocked_network_attempts'] = len(attempts)
        output = ROOT / 'subscription-grants-postgres-results.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'database': name}), flush=True)


if __name__ == '__main__':
    main()
