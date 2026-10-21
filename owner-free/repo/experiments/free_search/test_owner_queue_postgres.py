"""Opt-in localhost PostgreSQL validation; fabricated pages, no Telegram/network.

Each case retains a new isolated database. Never accepts a connection URL.
"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).parent/'tests'))
import json
import time
import unittest
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import create_engine,select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
import psycopg
from psycopg import sql
import test_owner_queue_sender as sender_tests
import test_owner_stage_pipeline as stage_tests
import test_owner_pilot as fixtures
from test_owner_browser_mean import capture
from backend.models import Delivery,Search,Filters
from experiments.free_search.owner_queue_sender import send_queued
from experiments.free_search.network_guard import OutboundGuard

DATABASES=[]


def isolated_engine(*args,**kwargs):
    name='autodeal_queue_'+uuid.uuid4().hex[:12]
    password=(ROOT.parent/'.local-postgres/password.txt').read_text().strip()
    with psycopg.connect(host='127.0.0.1',port=55439,user='pilot',password=password,
                         dbname='postgres',connect_timeout=5,autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    DATABASES.append(name)
    return create_engine(URL.create('postgresql+psycopg',username='pilot',password=password,
                         host='127.0.0.1',port=55439,database=name),
                         connect_args={'options':'-c statement_timeout=10000 -c lock_timeout=5000'})


class PostgreSQLQueueTests(sender_tests.QueueSenderTests):
    def setUp(self):
        with patch.object(fixtures,'create_engine',isolated_engine):super().setUp()

    def test_independent_local_queues_share_one_delivery(self):
        barrier=Barrier(2)
        calls=[]
        def run():
            # Connections created inside each thread; no cross-thread SQLite use.
            fixture=stage_tests.StagePipelineTests()
            fixture.setUp()
            try:
                fixture.pipeline.detail(fixture.page,fixture.now)
                fixture.pipeline.capture(fixture.page.listing_id,capture(fixture.page),fixture.now)
                fixture.pipeline.qualify(fixture.page.listing_id,0,fixture.now)
                barrier.wait(timeout=10)
                def transport(uid,text):
                    calls.append(uid)
                    return {'ok':True,'result':{'message_id':789}}
                return send_queued(self.engine,self.settings,fixture.pipeline,fixture.page.listing_id,
                                   fixtures.OWNER,transport,enabled=True,clock=lambda:fixture.now,
                                   condition_review=sender_tests.reviewed(fixture.page,fixture.now))
            finally:fixture.doCleanups()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run) for _ in range(2)]
            outcomes=[f.result(timeout=30) for f in futures]
        self.assertEqual(sum(r['state']=='sent' for r in outcomes),1,outcomes)
        self.assertEqual(calls,[fixtures.OWNER])
        with Session(self.engine) as db:
            self.assertEqual(len(db.scalars(select(Delivery)).all()),1)
            self.assertEqual(db.get(Search,fixtures.CLIENT).user_id,fixtures.CLIENT)

    def test_session_scope_does_not_change_saved_filters(self):
        with Session(self.engine) as db,db.begin():
            search=db.get(Search,fixtures.OWNER)
            search.filters={**search.filters,'brand':'Different'}
            search.fingerprint=Filters(**search.filters).fingerprint()
            original=dict(search.filters)
        result=self.send(session_all_ukraine=True)
        self.assertEqual(result['state'],'sent',result)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Search,fixtures.OWNER).filters,original)
            self.assertEqual(db.scalars(select(Delivery.user_id)).all(),[fixtures.OWNER])


if __name__=='__main__':
    guard=OutboundGuard()
    started=time.monotonic()
    # libpq connects only to the literal loopback endpoint; Python outbound denied.
    with guard.isolated():
        result=unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLQueueTests))
    report={'status':'passed' if result.wasSuccessful() else 'failed',
            'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'seconds':time.monotonic()-started,'database_scope':'localhost_disposable',
            'databases':DATABASES,'inputs':'fabricated HTML and range; synthetic transport',
            'source_gets':0,'real_telegram_calls':0,'production_mutations':0,
            'python_network_guard':guard.summary()}
    (ROOT.parent/'night21-queue-postgres.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)
