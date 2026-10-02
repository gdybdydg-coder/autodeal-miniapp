import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from experiments.olx_offline.pipeline import Pipeline,canonical,estimate
from experiments.olx_offline.test_pipeline import raw,comps,users,NOW

class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite';self.p=Pipeline(self.path)
        self.net=patch('socket.socket',side_effect=AssertionError('No network'));self.net.start()
    def tearDown(self):
        self.p.db.close();self.tmp.cleanup();self.net.stop()
    def collect(self,car):
        return self.p.collect(lambda _:dict(items=[car],next=None),NOW,page_budget=1,row_budget=10)
    def queue(self):return self.p.enqueue(users(),comps(),NOW,capacity=10,olx_enabled=True)
    def test_price_change_held_then_reassessed(self):
        self.collect(raw(1));self.queue();self.collect(raw(1,price=11000))
        r=self.p.deliver_fake(lambda *_:self.fail('Stale price delivered'),lambda _:True,now=NOW,olx_enabled=True)
        self.assertEqual(r['held'],1);self.assertEqual(self.queue()['queued'],0)
        self.collect(raw(1,price=6500));self.assertEqual(self.queue()['queued'],1)
        self.assertEqual(self.p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True)['accepted'],1)
    def test_expired_assessment_held(self):
        self.collect(raw(1));self.queue()
        self.assertEqual(self.p.deliver_fake(lambda *_:self.fail(),lambda _:True,now=NOW+301,olx_enabled=True)['held'],1)
    def test_observation_time_change_not_price_change(self):
        self.collect(raw(1));self.queue();self.collect(raw(1,checked_at=NOW))
        self.assertEqual(self.p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True)['accepted'],1)
    def test_old_pending_without_proof_is_not_sent(self):
        self.collect(raw(1));self.p.db.execute("INSERT INTO deliveries VALUES('0','olx','1','pending')");self.p.db.commit()
        self.assertEqual(self.p.deliver_fake(lambda *_:self.fail(),lambda _:True,now=NOW,olx_enabled=True)['held'],1)
    def test_first_seen_cannot_prove_price_freshness(self):
        records=comps()
        for c in records:c['checked_at']=None
        self.assertEqual(estimate(canonical(raw(1),NOW),records,NOW)['sample'],0)
    def test_fresh_check_accepts_old_discovery(self):
        records=comps()
        for c in records:c['first_seen_at']=NOW-90*86400;c['checked_at']=NOW
        self.assertEqual(estimate(canonical(raw(1),NOW),records,NOW)['status'],'experimental_estimate')
    def test_stale_or_future_check_rejected(self):
        for timestamp in (NOW-31*86400,NOW+1):
            records=comps()
            for c in records:c['checked_at']=timestamp
            self.assertEqual(estimate(canonical(raw(1),NOW),records,NOW)['sample'],0)
    def test_cursor_cycle_detected_across_restart(self):
        pages={None:dict(items=[raw(1)],next='two'),'two':dict(items=[raw(2)],next='two')}
        self.p.collect(pages.__getitem__,NOW,page_budget=1,row_budget=10)
        self.p.db.close();self.p=Pipeline(self.path)
        self.p.collect(pages.__getitem__,NOW,page_budget=1,row_budget=10)
        self.p.db.close();self.p=Pipeline(self.path)
        r=self.p.collect(lambda _:self.fail('Repeated cyclic request'),NOW,page_budget=1,row_budget=10)
        self.assertEqual(r['reason'],'cursor_cycle');self.assertEqual(len(self.p.cars()),2)
    def test_malformed_card_leaves_checkpoint_incomplete(self):
        r=self.p.collect(lambda _:dict(items=['invalid'],next=None),NOW,page_budget=1,row_budget=10)
        self.assertEqual(r['status'],'incomplete');self.assertEqual(len(self.p.cars()),0)
