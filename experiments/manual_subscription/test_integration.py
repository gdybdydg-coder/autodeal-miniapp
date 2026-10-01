import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from adapter import Adapter, FixtureSessions
from ledger import Ledger, DAY


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'integration.sqlite')
        self.admin_uid = 777292211
        self.ledger = Ledger(self.path,self.admin_uid)
        self.sessions = FixtureSessions()
        self.admin = self.sessions.issue(self.admin_uid)
        self.client = self.sessions.issue(111)
        self.other = self.sessions.issue(222)
        self.api = Adapter(self.ledger,self.sessions)

    def tearDown(self):
        self.ledger.close();self.tmp.cleanup()

    def review(self, token=None, now=1000):
        token = token or self.client
        oid = self.api.create(token,{},now)
        self.api.submit(token,oid,{'receipt_ref':'SYNTH-RECEIPT-001','paid_at':now+1},now+1)
        return oid

    def approval(self, reference='SYNTH-BANK-001'):
        return {'payment_ref':reference,'actual_amount':250,'bank_verified':True}

    def test_full_lifecycle_restart_audit_outbox_and_stop(self):
        self.ledger.seed_search_guard_fixture(111,enabled=True,epoch=4,sent_claims=13,uncertain_claims=2)
        oid = self.review()
        self.assertFalse(self.ledger.active(111,1001))
        self.assertEqual(self.api.review_queue(self.admin)[0][0],oid)
        self.api.clarify(self.admin,oid,{'note':'Уточніть тестовий час'},1002)
        self.ledger.close();self.ledger = Ledger(self.path,self.admin_uid)
        self.api = Adapter(self.ledger,self.sessions)
        self.assertEqual(self.api.status(self.client,oid)['clarification'],'Уточніть тестовий час')
        self.api.submit(self.client,oid,{'receipt_ref':'SYNTH-CORRECTED-002','paid_at':1002},1003)
        self.ledger.stop_search(111,1003)
        first_expiry = self.api.approve(self.admin,oid,self.approval(),1004)
        self.assertEqual(first_expiry,1004+30*DAY)
        self.assertEqual(self.api.approve(self.admin,oid,self.approval(),1005),first_expiry)
        second = self.review(now=2000)
        expiry = self.api.approve(self.admin,second,self.approval('SYNTH-BANK-002'),2002)
        self.assertEqual(expiry,first_expiry+30*DAY)
        self.assertFalse(self.ledger.record_expiry(111,expiry-1))
        self.assertTrue(self.ledger.record_expiry(111,expiry))
        self.assertFalse(self.ledger.record_expiry(111,expiry+1))
        guard = self.ledger.entitlement(111,expiry)
        self.assertEqual(guard,{'membership_active':False,'search_enabled':False,
            'effective_access':False,'epoch':5,'sent_claims':13,'uncertain_claims':2})
        actions = [r[0] for r in self.ledger.db.execute('SELECT action FROM audit ORDER BY id')]
        self.assertEqual(actions,['created','receipt_submitted','clarification_requested',
            'receipt_resubmitted','approved','created','receipt_submitted','approved'])
        kinds = [r[0] for r in self.ledger.db.execute('SELECT kind FROM outbox ORDER BY id')]
        self.assertEqual(kinds,['review_requested','clarification_requested','review_requested',
            'membership_approved','review_requested','membership_approved','membership_expired'])
        events = self.ledger.claim_outbox('SYNTH-WORKER-001',expiry+2,limit=100)
        # All seven history records remain, but only the current expiry notice
        # is eligible. Stage 4's drain of seven notices exposed stale payloads.
        self.assertEqual([row[3] for row in events],['membership_expired'])
        reasons = [r[0] for r in self.ledger.db.execute('SELECT reason FROM outbox ORDER BY id')]
        self.assertEqual(reasons,['order_changed','order_changed','order_changed',
            'membership_changed','order_changed','membership_expired',None])
        self.assertEqual(self.ledger.outbox_status()['states'],{'cancelled':6,'claimed':1})
        self.assertTrue(self.ledger.prepare_outbox(events[-1][0],'SYNTH-WORKER-001',expiry+3))
        self.assertTrue(self.ledger.mark_outbox_uncertain(events[-1][0],'SYNTH-WORKER-001',expiry+3))
        self.ledger.close();self.ledger = Ledger(self.path,self.admin_uid)
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER-002',expiry+4),[])

    def test_session_required_and_body_identity_cannot_be_spoofed(self):
        oid = self.review()
        for token in [None,'unknown',self.admin_uid,True]:
            with self.subTest(token_type=type(token).__name__),self.assertRaises(PermissionError):
                self.api.approve(token,oid,self.approval(),1002)
        with self.assertRaises(PermissionError):self.api.approve(self.client,oid,self.approval(),1002)
        with self.assertRaises(PermissionError):self.api.status(self.other,oid)
        with self.assertRaises(PermissionError):self.api.submit(self.other,oid,{'receipt_ref':'SYNTH-OTHER','paid_at':1001},1002)
        with self.assertRaises(PermissionError):self.api.review_queue(self.client)
        for data in [{'uid':111},{'actor':self.admin_uid},{'amount':1},{'expires_at':99999999}]:
            with self.assertRaises(ValueError):self.api.create(self.client,data,1002)
        with self.assertRaises(ValueError):self.api.approve(self.admin,oid,{**self.approval(),'actor':self.admin_uid},1002)
        self.assertFalse(self.ledger.active(111,1002))
        self.assertEqual(self.api.status(self.client,oid)['state'],'review')

    def test_revoked_session_does_not_change_order(self):
        oid = self.review();self.sessions.revoke(self.admin)
        with self.assertRaises(PermissionError):self.api.reject(self.admin,oid,{},1002)
        self.assertEqual(self.api.status(self.client,oid)['state'],'review')

    def test_concurrent_create_is_one_open_order(self):
        def create(_):
            ledger = Ledger(self.path,self.admin_uid)
            try:return Adapter(ledger,self.sessions).create(self.client,{},1000)
            finally:ledger.close()
        with ThreadPoolExecutor(max_workers=4) as pool: ids=list(pool.map(create,range(4)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM orders').fetchone()[0],1)

    def test_concurrent_workers_claim_disjoint_batches_and_crash_is_not_replayed(self):
        self.review(); self.review(self.other)
        def claim(i):
            ledger = Ledger(self.path,self.admin_uid)
            try:return ledger.claim_outbox('SYNTH-WORKER-'+str(i),1002,limit=1)
            finally:ledger.close()
        with ThreadPoolExecutor(max_workers=4) as pool: batches=list(pool.map(claim,range(4)))
        ids=[row[0] for batch in batches for row in batch]
        self.assertEqual(len(ids),2);self.assertEqual(len(set(ids)),2)
        self.ledger.close();self.ledger = Ledger(self.path,self.admin_uid)
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER-NEW',1003),[])

    def test_wrong_worker_cannot_acknowledge_claim(self):
        self.review();event=self.ledger.claim_outbox('SYNTH-WORKER-A',1002)[0]
        self.assertFalse(self.ledger.mark_outbox_delivered(event[0],'SYNTH-WORKER-B',1003))
        self.assertFalse(self.ledger.mark_outbox_delivered(event[0],'SYNTH-WORKER-A',1003))
        self.assertTrue(self.ledger.prepare_outbox(event[0],'SYNTH-WORKER-A',1003))
        self.assertTrue(self.ledger.mark_outbox_delivered(event[0],'SYNTH-WORKER-A',1003))


if __name__=='__main__':unittest.main()
