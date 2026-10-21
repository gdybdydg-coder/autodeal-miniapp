import unittest
from dataclasses import replace
from experiments.free_search.owner_scan import ScanCursor, HEAD
from experiments.free_search.owner_observations import Observations
import test_owner_scan as fixtures


class RecoveryTests(unittest.TestCase):
    setUp=fixtures.ScanTests.setUp
    tearDown=fixtures.ScanTests.tearDown

    def moving(self, start=100, begin=True):
        head=replace(fixtures.page(),url=HEAD+'?page=1')
        if begin:self.scan.start(start,head_url=head.url)
        self.scan.accept(head,fixtures.cards(1,2),start+1)
        self.scan.accept(fixtures.page(1,True),fixtures.cards(3),start+2)
        self.scan.accept(head,fixtures.cards(4,1),start+3)
        return head

    def test_restart_preserves_ids_times_baseline_and_exact_head(self):
        head=self.moving()
        before=self.obs.db.execute('SELECT * FROM observations ORDER BY id').fetchall()
        state=self.scan.recover_moving_head(104)
        self.assertEqual(state['expected'],HEAD+'?page=1')
        self.assertEqual(before,self.obs.db.execute('SELECT * FROM observations ORDER BY id').fetchall())
        self.obs.close();self.obs=Observations(self.path);self.scan=ScanCursor(self.obs)
        self.scan.accept(head,fixtures.cards(4,1),105)
        self.scan.accept(fixtures.page(1,True),fixtures.cards(2,3),106)
        self.assertEqual(self.scan.accept(head,fixtures.cards(4,1),107)['phase'],'complete')
        self.assertEqual(self.obs.db.execute('SELECT count(*) FROM observations').fetchone()[0],4)
        for ident in ('1','2','3','4'):
            self.assertEqual(self.obs.signal(ident,108)['reason'],'baseline_listing')

    def test_limit_survives_restart_and_does_not_mutate_on_denial(self):
        self.moving()
        self.scan.recover_moving_head(104);self.moving(105,False)
        self.scan.recover_moving_head(109);self.moving(110,False)
        self.obs.close();self.obs=Observations(self.path);self.scan=ScanCursor(self.obs)
        before=self.scan.status()
        with self.assertRaisesRegex(ValueError,'recovery_limit'):
            self.scan.recover_moving_head(114)
        self.assertEqual(self.scan.status(),before)

    def test_scope_failure_cannot_be_recovered_as_head_churn(self):
        self.scan.start(100)
        self.scan.accept(fixtures.page(),fixtures.cards(1,2),101)
        self.scan.accept(fixtures.page(1,True,scope='custom=1'),fixtures.cards(3),102)
        with self.assertRaisesRegex(ValueError,'not_applicable'):
            self.scan.recover_moving_head(103)

    def test_failed_recovery_insert_rolls_back_abandon(self):
        self.moving();before=self.scan.status()
        self.obs.db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON scan_recovery BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception):self.scan.recover_moving_head(104)
        self.assertEqual(self.scan.status(),before)
        self.assertEqual(self.obs.db.execute('SELECT count(*) FROM scans').fetchone()[0],1)

    def test_clock_regression_rejected(self):
        self.moving()
        with self.assertRaisesRegex(ValueError,'clock_regression'):
            self.scan.recover_moving_head(102)
