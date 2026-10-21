from dataclasses import replace
import unittest
import test_owner_scan as fixtures
from experiments.free_search.owner_scan import HEAD
from experiments.free_search.owner_budget import public_url, Denied


class ScopeTransitionTests(unittest.TestCase):
    setUp=fixtures.ScanTests.setUp
    tearDown=fixtures.ScanTests.tearDown

    def blocked(self):
        self.scan.start(100)
        old = fixtures.page()
        self.scan.accept(old,fixtures.cards(1,2),101)
        self.scan.accept(fixtures.page(1,True,scope='custom=1'),fixtures.cards(3),102)
        return old.scope_digest,replace(fixtures.page(scope='custom=1'),url=HEAD+'?page=1')

    def transition(self,old,head):
        return self.scan.transition_initial_scope(103,old_scope=old,head=head,evidence_sha256='a'*64)

    def test_preserves_history_and_no_initial_alerts(self):
        old,head=self.blocked()
        before=self.obs.db.execute('SELECT * FROM observations ORDER BY id').fetchall()
        r=self.transition(old,head)
        self.assertEqual(before,self.obs.db.execute('SELECT * FROM observations ORDER BY id').fetchall())
        self.assertEqual(r['baseline'],1)
        self.assertEqual(r['expected'],HEAD+'?page=1')
        self.assertEqual(self.obs.db.execute('SELECT reason FROM scans WHERE id=1').fetchone()[0],'scope_metadata_changed')
        self.scan.accept(head,fixtures.cards(2,3),104)
        self.assertEqual(self.obs.signal('3',104)['reason'],'baseline_listing')

    def test_mismatched_old_scope_and_unobserved_head_rejected(self):
        old,head=self.blocked(); before=self.scan.status()
        for a,b in [('incorrect',head),(old,replace(head,url=HEAD))]:
            with self.assertRaises(ValueError):self.transition(a,b)
        self.assertEqual(before,self.scan.status())

    def test_completed_control_cannot_be_reset(self):
        old,head=self.blocked()
        self.obs.db.execute('UPDATE scan_control SET initial_complete=1')
        with self.assertRaises(ValueError):self.transition(old,head)

    def test_failed_audit_write_rolls_back_parent(self):
        old,head=self.blocked();before=self.scan.status()
        self.obs.db.execute("CREATE TRIGGER fail_transition BEFORE INSERT ON scan_scope_transition BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception):self.transition(old,head)
        self.assertEqual(before,self.scan.status())

    def test_extended_routes_are_opt_in_not_guessed_parameters(self):
        with self.assertRaises(Denied):public_url(HEAD+'?page=3')
        public_url(HEAD+'?page=3',allow_feed_pagination=True)
        for suffix in ('?page=0','?page=1001','?page=3&custom=1'):
            with self.assertRaises(Denied):public_url(HEAD+suffix,allow_feed_pagination=True)
