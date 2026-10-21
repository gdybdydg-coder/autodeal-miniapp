"""Actual isolated PostgreSQL; fabricated source and Telegram, never production."""
from pathlib import Path
import sys,json,time,unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent),str(Path(__file__).parent/'tests')]
from unittest.mock import patch
import test_owner_queue_postgres as pg
from test_owner_comparison_postgres import PostgreSQLComparisonTests
from test_owner_local_bridge import LocalBridgeTests
from experiments.free_search.network_guard import OutboundGuard
from sqlalchemy import create_engine

def bridge_engine(*args,**kwargs):
    initial=pg.isolated_engine(*args,**kwargs);url=initial.url;initial.dispose()
    return create_engine(url,pool_size=2,max_overflow=0,pool_timeout=2,
        connect_args={'options':'-c statement_timeout=3000 -c lock_timeout=1000'})

class PostgreSQLBridgeTests(LocalBridgeTests):
    def setUp(self):
        with patch.object(pg.fixtures,'create_engine',bridge_engine):super().setUp()

    def test_single_connection_regression_does_not_replay_claim(self):
        from sqlalchemy.exc import TimeoutError
        single=create_engine(self.engine.url,pool_size=1,max_overflow=0,pool_timeout=.05)
        single.update_execution_options(**self.engine.get_execution_options())
        self.assertEqual(self.engine.pool.size(),2)
        self.bridge.engine=single
        try:
            with self.assertRaises(TimeoutError):self.bridge.deliver_ready()
            self.assertEqual(self.calls,[])
        finally:single.dispose();self.bridge.engine=self.engine
        # Even a known test failure before transport cannot erase a durable claim.
        self.assertEqual(self.bridge.deliver_ready()[0]['reason'],'existing_free_comparison')
        self.assertEqual(self.calls,[])

if __name__=='__main__':
    guard=OutboundGuard();started=time.monotonic()
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
        for c in (PostgreSQLComparisonTests,PostgreSQLBridgeTests))
    with guard.isolated():result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'status':'passed' if result.wasSuccessful() else 'failed','tests':result.testsRun,
        'seconds':time.monotonic()-started,'scope':'isolated localhost PostgreSQL; synthetic sources/transport',
        'source_requests':0,'real_telegram_calls':0,'production_changes':0,'network':guard.summary()}
    (ROOT.parent/'autonomous-postgres-20261021.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));raise SystemExit(not result.wasSuccessful())
