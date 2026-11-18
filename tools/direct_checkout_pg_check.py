"""Checkout acceptance on a new retained localhost-only PostgreSQL database.

Run: python tools/direct_checkout_pg_check.py --cluster-root ../.local-postgres
No production DSN, provider requests, bank operations or Telegram sends accepted.
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
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app import Settings
from backend import billing, manual_checkout as c, manual_payments as m, manual_receipts as receipts
from backend.billing_models import AccessEvent, BillingControl, Entitlement
from backend.manual_payment_models import (
    ManualBase, PaymentRequest, PaymentAudit, PaymentEvidence, PaymentNotice,
    ReceiptExpectation, ReceiptSubmission,
)
from backend.models import Base, User


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cluster-root', required=True)
    options = parser.parse_args()
    cluster = Path(options.cluster_root).resolve()
    password = (cluster / 'password.txt').read_text(encoding='utf-8').strip()
    url = URL.create('postgresql+psycopg', username='pilot', password=password,
                     host='127.0.0.1', port=55439, database='postgres')
    name = 'checkout_20261118_' + uuid.uuid4().hex[:12]
    report = {'database': name, 'host': '127.0.0.1', 'port': 55439,
              'provider_requests': 0, 'telegram_sends': 0, 'checks': [], 'passed': False}
    owner, uid = 99001, 501
    now = time.time()
    attempts = []
    original_connect, original_resolve = socket.socket.connect, socket.getaddrinfo

    def connect(sock, address):
        if isinstance(address, tuple) and address[:2] == ('127.0.0.1', 55439):
            return original_connect(sock, address)
        attempts.append('network_connect')
        raise AssertionError('Only isolated PostgreSQL is permitted')

    def resolve(host, port, *args, **kwargs):
        if host == '127.0.0.1' and int(port) == 55439:
            return original_resolve(host, port, *args, **kwargs)
        attempts.append('dns')
        raise AssertionError('External DNS forbidden')

    def denied_transport(*args, **kwargs):
        attempts.append('telegram_transport')
        raise AssertionError('Real Telegram transport forbidden')

    def passed(label):
        report['checks'].append(label)
        print(label + ' PASS', flush=True)

    bban = '0' * 25
    iban = f"UA{98-int(bban+'301000')%97:02d}" + bban
    environment = {'SUBSCRIPTION_EXPECTED_ADMIN_ID': str(owner),
                   'MANUAL_PAYMENT_REVIEW_ENABLED': 'true', 'MANUAL_PAYMENT_PUBLIC_ENABLED': 'true',
                   c.subscription_preview.RECIPIENT_ENV: json.dumps({'mode': 'test_only',
                       'recipient_name': 'Synthetic test', 'recipient_code': '0000000000',
                       'iban': iban, 'bank_name': 'Synthetic bank'})}
    engine = None
    try:
        with patch.dict(os.environ, environment), patch('socket.socket.connect', connect), \
                patch('socket.getaddrinfo', resolve), patch('backend.telegram_setup.call', denied_transport):
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
            settings = Settings('unused-local-harness', 'test-only-token', 'test-only-secret-'*4,
                False, False, admin_telegram_id=owner, manual_payment_review_enabled=True)
            with Session(engine) as db, db.begin():
                db.add(User(id=owner, ready=True))
                db.add(BillingControl(id=billing.CONTROL, version=1, enforce=True,
                                      sales=True, offer=dict(c.OFFER)))

            def count(model):
                with Session(engine) as db:
                    return db.scalar(select(func.count()).select_from(model))

            barrier = threading.Barrier(8)
            def purchase(n):
                barrier.wait(timeout=15)
                return c.create(engine, settings, uid, now, c.TERMS_VERSION, telegram_update_id=1000+n)
            with ThreadPoolExecutor(max_workers=8) as pool:
                created = list(pool.map(purchase, range(8)))
            code = created[0]['code']
            assert len({r['code'] for r in created}) == 1 and all(r['awaiting_receipt'] for r in created)
            assert count(PaymentRequest) == count(ReceiptExpectation) == 1
            with Session(engine) as db:
                assert not db.get(User, uid).ready and not billing.expiry(db, uid)
                assert db.scalar(select(func.count()).select_from(PaymentAudit).where(
                    PaymentAudit.action == 'created')) == 1
            passed('eight_parallel_public_purchases_create_one_request_and_receipt_expectation')

            payload = {'update_id': 2000, 'message': {'message_id': 2000,
                'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'},
                'document': {'file_id': 'SYNTHETIC-PDF', 'mime_type': 'application/pdf', 'file_size': 1024}}}
            barrier = threading.Barrier(8)
            def attach(_):
                barrier.wait(timeout=15)
                return receipts.handle(engine, settings, payload, now+1)
            with ThreadPoolExecutor(max_workers=8) as pool:
                received = list(pool.map(attach, range(8)))
            assert all(r['text'] == receipts.ACK for r in received)
            assert count(ReceiptSubmission) == count(PaymentEvidence) == count(PaymentNotice) == 1
            assert count(Entitlement) == 0
            passed('eight_identical_pdf_updates_store_one_evidence_and_owner_notice_without_access')

            request = m.get_status(engine, settings, uid, code)
            token = m.preview(engine, settings, owner, code, request['revision'], now+2,
                              bank_verified=True)['confirmation']
            barrier = threading.Barrier(8)
            def approve(_):
                barrier.wait(timeout=15)
                return m.confirm(engine, settings, owner, token, now+3)
            with ThreadPoolExecutor(max_workers=8) as pool:
                approved = list(pool.map(approve, range(8)))
            assert sum(not r['replayed'] for r in approved) == 1
            assert len({r['expires_at'] for r in approved}) == 1
            assert count(AccessEvent) == count(Entitlement) == 1
            passed('eight_concurrent_owner_confirms_grant_once_and_keep_one_access_audit')

            for n in range(8):
                replay = c.create(engine, settings, uid, now+4, c.TERMS_VERSION, telegram_update_id=1000+n)
                assert replay['code'] == code and replay['state'] == 'approved'
            assert count(PaymentRequest) == 1
            passed('replayed_purchase_updates_after_approval_do_not_create_renewal_requests')

            def fail(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith('INSERT INTO manual_receipt_expectations'):
                    raise OperationalError('synthetic', {}, Exception('injected expectation failure'))
            event.listen(engine, 'before_cursor_execute', fail)
            try:
                try:
                    c.create(engine, settings, 502, now+5, c.TERMS_VERSION, telegram_update_id=3000)
                except OperationalError:
                    pass
                else:
                    raise AssertionError('Expected injected rollback')
            finally:
                event.remove(engine, 'before_cursor_execute', fail)
            with Session(engine) as db:
                assert db.get(User, 502) is None
            assert count(PaymentRequest) == count(ReceiptExpectation) == 1
            passed('expectation_failure_rolls_back_registration_request_terms_and_audit')
            assert not attempts
            report['passed'] = True
    finally:
        if engine is not None:
            engine.dispose()
        report['network_attempts_denied'] = len(attempts)
        destination = ROOT / 'test-results' / 'direct-checkout-postgres.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print('Report: ' + str(destination), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
