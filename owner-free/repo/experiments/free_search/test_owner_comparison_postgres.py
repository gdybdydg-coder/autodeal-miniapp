"""Isolated PostgreSQL race proof; synthetic input and Telegram transport only."""
from pathlib import Path
import sys,json,time,unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).parent))
sys.path.insert(0,str(Path(__file__).parent/'tests'))
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import select
from sqlalchemy.orm import Session
import test_owner_queue_postgres as pg
import test_owner_comparison_delivery as cases
from experiments.free_search.owner_comparison_delivery import send_comparison,deliveries
from experiments.free_search.network_guard import OutboundGuard
from backend.models import Delivery,Listing

class PostgreSQLComparisonTests(cases.ComparisonDeliveryTests):
    def setUp(self):
        with patch.object(pg.fixtures,'create_engine',pg.isolated_engine):super().setUp()

    def test_two_free_processes_leave_paid_path_available(self):
        barrier=Barrier(2);calls=[]
        def run():
            fixture=pg.stage_tests.StagePipelineTests();fixture.setUp()
            try:
                fixture.pipeline.detail(fixture.page,fixture.now)
                fixture.pipeline.capture(fixture.page.listing_id,pg.capture(fixture.page),fixture.now)
                fixture.pipeline.qualify(fixture.page.listing_id,0,fixture.now)
                barrier.wait(timeout=10)
                def transport(uid,text):
                    calls.append(uid)
                    return {'ok':True,'result':{'message_id':777}}
                return send_comparison(self.engine,self.settings,fixture.pipeline,fixture.page.listing_id,
                    111,transport,condition_review=pg.sender_tests.reviewed(fixture.page,fixture.now),
                    enabled=True,clock=lambda:fixture.now)
            finally:fixture.doCleanups()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=[f.result(timeout=30) for f in [pool.submit(run),pool.submit(run)]]
        self.assertEqual(sum(r['state']=='sent' for r in outcomes),1,outcomes)
        self.assertEqual(calls,[111])
        with Session(self.engine) as db,db.begin():
            self.assertIsNone(db.scalar(select(Delivery.id)))
            # Ordinary paid path can create the same identity after free acceptance.
            listing=Listing(source='auto_ria',source_id=self.page.listing_id,car={})
            db.add(listing);db.flush()
            db.add(Delivery(user_id=111,listing_id=listing.id,state='sent',message_id=888))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(select(deliveries.c.message_id)).scalars().all(),[777])

if __name__=='__main__':
    guard=OutboundGuard();start=time.monotonic()
    with guard.isolated():
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLComparisonTests))
    report={'status':'passed' if result.wasSuccessful() else 'failed','tests':result.testsRun,
        'failures':len(result.failures),'errors':len(result.errors),'seconds':time.monotonic()-start,
        'databases':pg.DATABASES,'scope':'localhost synthetic pages and transport',
        'source_gets':0,'telegram_calls':0,'production_mutations':0,'network':guard.summary()}
    (ROOT.parent/'comparison-postgres-20261021.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
