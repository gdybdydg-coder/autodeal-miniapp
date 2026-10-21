"""Real localhost PostgreSQL; fabricated source HTML and synthetic transport only.

Explicit -I invocation. No production URLs, source requests or Telegram token.
Retains a uniquely named disposable database and observation file as evidence.
"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import json
import os
import time
import uuid
from types import SimpleNamespace
from experiments.free_search.network_guard import OutboundGuard


def main():
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine, select
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import Session
    from backend import paid_source_access
    from backend.models import Base, User, Search, Filters, MonitorWatch, Delivery, DeliveryTiming
    from backend.billing_models import Entitlement
    from backend.manual_payment_models import ManualBase, PaymentRequest
    from experiments.free_search.owner_access import current_searches
    from experiments.free_search.owner_observations import Observations
    from experiments.free_search.owner_scan import ScanCursor
    from experiments.free_search.owner_delivery import send_one
    from experiments.free_search.tests.test_owner_pilot import page, NOW, OWNER, CLIENT
    from experiments.free_search.tests.test_owner_scan import page as metadata, cards

    local = ROOT.parent / '.local-postgres'
    run_id = uuid.uuid4().hex[:10]
    name = 'autodeal_fullpath_' + run_id
    config = dict(host='127.0.0.1', port=55439, user='pilot',
                  password=(local/'password.txt').read_text().strip(), connect_timeout=5)
    with psycopg.connect(**config, dbname='postgres', autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    engine = create_engine(URL.create('postgresql+psycopg', database=name,
        username=config['user'], password=config['password'], host='127.0.0.1', port=55439),
        connect_args={'options':'-c statement_timeout=10000 -c lock_timeout=5000'})
    assert engine.url.host == '127.0.0.1' and engine.url.database.startswith('autodeal_fullpath_')
    engine.update_execution_options(**{paid_source_access.OPTION:True, paid_source_access.EXCLUSIONS:()})
    os.environ['SUBSCRIPTION_EXPECTED_ADMIN_ID'] = str(OWNER)
    settings = SimpleNamespace(live=True, admin_telegram_id=OWNER)
    Base.metadata.create_all(engine)
    ManualBase.metadata.create_all(engine)
    filters = Filters(brand='Example', region=['Вінницька область','Чернівецька область'])
    with Session(engine) as db, db.begin():
        for uid in (OWNER, CLIENT):
            db.add(User(id=uid, ready=True))
            db.add(PaymentRequest(id=str(uid), user_id=uid, state='approved', amount_minor=25000,
                currency='UAH', days=30, created_at=NOW-100, updated_at=NOW-100, expires_at=NOW+1000))
            db.add(Entitlement(user_id=uid, expires_at=NOW+1000, updated_at=NOW-100))
            db.add(Search(id=uid, user_id=uid, name='Synthetic', enabled=True,
                filters=filters.canonical(), fingerprint=filters.fingerprint()))
            db.add(MonitorWatch(search_id=uid, epoch='fixture-'+str(uid)))
    snapshot = current_searches(engine, settings, NOW)[0][0]
    peers = [page(str(20000000+i), 10000+i*100) for i in range(6)]
    path = local / ('fullpath-'+run_id+'.owner-observations.sqlite3')
    observations = Observations(path)
    scan = ScanCursor(observations)
    clock = [NOW]
    calls, cases = [], []

    def transport(uid, message):
        assert uid == OWNER
        assert 'Нове для нашого спостереження' in message
        assert 'Перша публікація не підтверджена' in message
        calls.append({'recipient':'synthetic_owner', 'message_id':1000+len(calls)})
        return {'ok':True, 'result':{'message_id':calls[-1]['message_id'], 'date':int(clock[0])}}

    def send(subject, sender=transport):
        return send_one(engine, settings, snapshot, subject, peers,
            [p.listing_id for p in peers], observations, sender, enabled=True, clock=lambda:clock[0])

    def start_scan(ident, at):
        scan.start(at)
        scan.accept(metadata(terminal=True), cards(ident), at+1)

    def finish_scan(ident, at):
        scan.accept(metadata(terminal=True), cards(ident), at)

    try:
        baseline = page('11111111')
        start_scan(baseline.listing_id, NOW-30)
        finish_scan(baseline.listing_id, NOW-28)
        result = send(baseline)
        assert result['reason'] == 'baseline_listing' and not calls
        cases.append({'case':'initial_scan_not_sent', 'result':result})

        subject = page()
        start_scan(subject.listing_id, NOW-5)
        result = send(subject)
        assert result['reason'] == 'scan_not_completed' and not calls
        with Session(engine) as db:
            assert db.scalar(select(Delivery.id)) is None
        cases.append({'case':'unfinished_scan_no_claim', 'result':result})

        finish_scan(subject.listing_id, NOW-2)
        result = send(subject)
        assert result['state'] == 'sent' and len(calls) == 1
        with Session(engine) as db:
            timing = db.get(DeliveryTiming, result['delivery_id'])
            assert timing.source_added_at is None
            assert timing.discovered_at == NOW-4
            assert timing.accepted_at == NOW
            assert db.get(Delivery, result['delivery_id']).user_id == OWNER
        cases.append({'case':'completed_scan_details_valuation_filters_claim_synthetic_receipt',
                      'result':result, 'compatible_fixture_peers':6,
                      'synthetic_clock_not_live_latency':True})

        observations.close()
        observations = Observations(path)
        scan = ScanCursor(observations)
        result = send(subject)
        assert result['reason'] == 'existing_owner_delivery' and len(calls) == 1
        cases.append({'case':'restart_shared_delivery_prevents_duplicate', 'result':result})

        clock[0] = NOW+10
        wrong = page('33333333')
        from dataclasses import replace
        wrong = replace(wrong, html=wrong.html.replace('Вінницька область', 'Львівська область'))
        start_scan(wrong.listing_id, NOW+5)
        finish_scan(wrong.listing_id, NOW+8)
        result = send(wrong)
        assert result['reason'] == 'owner_filter_mismatch' and len(calls) == 1
        cases.append({'case':'saved_multiregion_filter_rejects_outside_region', 'result':result})

        clock[0] = NOW+20
        ambiguous = page('44444444')
        start_scan(ambiguous.listing_id, NOW+15)
        finish_scan(ambiguous.listing_id, NOW+18)
        def unknown(uid, message):
            transport(uid, message)
            return None
        result = send(ambiguous, unknown)
        assert result['state'] == 'free_uncertain' and len(calls) == 2
        observations.close()
        observations = Observations(path)
        replay = send(ambiguous)
        assert replay['reason'] == 'existing_owner_delivery' and len(calls) == 2
        cases.append({'case':'ambiguous_acceptance_retained_after_restart_no_retry',
                      'result':result, 'replay':replay})

        with Session(engine) as db:
            assert paid_source_access.allowed(db, CLIENT, NOW+20)
            assert db.get(User, CLIENT).ready
            assert db.get(Search, CLIENT).filters == filters.canonical()
            assert db.get(Entitlement, CLIENT).expires_at == NOW+1000
            assert not db.scalars(select(Delivery).where(Delivery.user_id == CLIENT)).all()
        cases.append({'case':'other_synthetic_client_access_filters_unchanged', 'passed':True})
        return {'status':'passed', 'database_scope':'localhost_disposable', 'database':name,
                'cases':cases, 'synthetic_transport_calls':len(calls), 'real_telegram_calls':0,
                'source_gets':0, 'paid_api_calls':0, 'production_mutations':0,
                'publication_to_telegram':{'n':0,'p50':None,'p95':None,'max':None},
                'source_inputs':'fabricated_html_and_pagination_not_live_AUTO_RIA'}
    finally:
        observations.close()
        engine.dispose()


if __name__ == '__main__':
    guard = OutboundGuard()
    # libpq uses only the explicit loopback config above. Python HTTP/socket paths
    # remain denied throughout backend imports and pipeline invocation.
    with guard.isolated():
        report = main()
    report['python_network_guard'] = guard.summary()
    assert guard.attempted == 0
    report['completed_at'] = time.time()
    (ROOT.parent/'night-fullpath-postgres-20261020-0312.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
