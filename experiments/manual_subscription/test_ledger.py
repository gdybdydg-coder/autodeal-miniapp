import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ledger import DAY, Ledger


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'ledger.db')
        self.ledger = Ledger(self.path, 777292211)
        self.admin = 777292211

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def reviewed(self, uid=111):
        oid = self.ledger.create_order(uid, 249, 1000)
        self.ledger.submit_receipt(uid, oid, 'synthetic-receipt', 1001)
        return oid

    def approve(self, oid, reference='synthetic-bank-txn-1', now=1002):
        return self.ledger.approve(self.admin, oid, payment_ref=reference,
                                  actual_amount=249, bank_verified=True, now=now)

    def test_receipt_does_not_grant_access(self):
        self.reviewed()
        self.assertFalse(self.ledger.active(111, 1002))

    def test_duplicate_create_reuses_pending_order_with_snapshot_price(self):
        first = self.ledger.create_order(111, 249, 1000)
        self.assertEqual(self.ledger.create_order(111, 299, 1001), first)
        self.assertEqual(self.ledger.db.execute('SELECT amount FROM orders').fetchone()[0], 249)

    def test_non_admin_and_other_clients_cannot_approve_or_submit(self):
        oid = self.reviewed()
        with self.assertRaises(PermissionError):
            self.ledger.approve(111, oid, payment_ref='fake', actual_amount=249, bank_verified=True, now=1002)
        with self.assertRaises(PermissionError):
            self.ledger.submit_receipt(222, oid, 'fake', 1002)
        with self.assertRaises(PermissionError):
            self.ledger.reject(111, oid, 1002)

    def test_missing_bank_verification_and_wrong_amount_rejected(self):
        oid = self.reviewed()
        for amount, verified in [(249,False),(248,True),(True,True)]:
            with self.assertRaises(ValueError):
                self.ledger.approve(self.admin, oid, payment_ref='fake', actual_amount=amount, bank_verified=verified, now=1002)
        self.assertFalse(self.ledger.active(111, 1002))

    def test_approval_requires_receipt(self):
        oid = self.ledger.create_order(111,249,1000)
        with self.assertRaises(ValueError): self.approve(oid)

    def test_approval_and_expiry_boundary(self):
        expiry = self.approve(self.reviewed())
        self.assertEqual(expiry,1002+30*DAY)
        self.assertTrue(self.ledger.active(111,expiry-1))
        self.assertFalse(self.ledger.active(111,expiry))

    def test_duplicate_click_and_restart_do_not_extend_twice(self):
        oid = self.reviewed()
        expiry = self.approve(oid)
        self.ledger.close(); self.ledger = Ledger(self.path,self.admin)
        self.assertEqual(self.approve(oid,now=2000),expiry)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM audit WHERE action='approved'").fetchone()[0],1)

    def test_single_bank_payment_cannot_grant_two_clients(self):
        self.approve(self.reviewed(111))
        with self.assertRaises(ValueError): self.approve(self.reviewed(222))
        self.assertFalse(self.ledger.active(222,1002))

    def test_renewal_adds_to_active_expiry(self):
        expiry = self.approve(self.reviewed())
        renewal = self.reviewed()
        self.assertEqual(self.approve(renewal,'synthetic-bank-txn-2',2000),expiry+30*DAY)

    def test_expired_membership_starts_from_confirmation(self):
        expiry = self.approve(self.reviewed())
        self.assertEqual(self.approve(self.reviewed(),'synthetic-bank-txn-2',expiry+100),expiry+100+30*DAY)

    def test_rejected_order_cannot_be_approved(self):
        oid = self.reviewed(); self.ledger.reject(self.admin,oid,1002)
        with self.assertRaises(ValueError): self.approve(oid)

    def test_concurrent_duplicate_approval_grants_once(self):
        oid = self.reviewed()
        def run(_):
            ledger = Ledger(self.path,self.admin)
            try:
                return ledger.approve(self.admin,oid,payment_ref='synthetic-bank-txn-1',actual_amount=249,bank_verified=True,now=1002)
            finally: ledger.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run,range(4)))
        self.assertEqual(len(set(results)),1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM audit WHERE action='approved'").fetchone()[0],1)

    def test_clarification_survives_restart_and_resubmit(self):
        oid = self.reviewed()
        self.ledger.clarify(self.admin,oid,'Вкажіть час тестового переказу',1002)
        self.ledger.close(); self.ledger = Ledger(self.path,self.admin)
        status = self.ledger.order_status(111,oid)
        self.assertEqual(status['state'],'clarification')
        self.assertEqual(status['clarification'],'Вкажіть час тестового переказу')
        self.assertFalse(self.ledger.active(111,1003))
        self.assertEqual(self.ledger.create_order(111,249,1003),oid)
        with self.assertRaises(ValueError): self.approve(oid,now=1003)
        self.ledger.submit_receipt(111,oid,'corrected-fixture.pdf',1004)
        self.assertEqual(self.ledger.order_status(111,oid)['state'],'review')
        self.assertIsNone(self.ledger.order_status(111,oid)['clarification'])
        self.assertEqual(self.approve(oid,now=1005),1005+30*DAY)

    def test_clarification_and_status_authorization(self):
        oid = self.reviewed()
        with self.assertRaises(PermissionError): self.ledger.clarify(111,oid,'note',1002)
        with self.assertRaises(PermissionError): self.ledger.order_status(222,oid)
        with self.assertRaises(ValueError): self.ledger.clarify(self.admin,oid,' ',1002)
        with self.assertRaises(ValueError): self.ledger.clarify(self.admin,oid,'x'*501,1002)

    def test_duplicate_receipt_does_not_duplicate_audit(self):
        oid = self.reviewed()
        self.ledger.submit_receipt(111,oid,'synthetic-receipt',1002)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM audit WHERE action='receipt_submitted'").fetchone()[0],1)

    def test_invalid_reference_or_clock_leaves_order_unmodified(self):
        oid = self.reviewed()
        for bad in ['', ' trailing ', 'line\nbreak','x'*201,'null\0byte']:
            with self.assertRaises(ValueError): self.ledger.submit_receipt(111,oid,bad,1002)
        for bad in [0,-1,True,1.5,'1002',2**80]:
            with self.assertRaises(ValueError): self.ledger.clarify(self.admin,oid,'note',bad)
            with self.assertRaises(ValueError): self.ledger.active(111,bad)
        for bad in [' spaced ','line\nbreak','x'*101]:
            with self.assertRaises(ValueError): self.approve(oid,bad)
        self.assertEqual(self.ledger.order_status(111,oid)['state'],'review')
        self.assertFalse(self.ledger.active(111,1002))

    def test_reject_clarification_order_and_preserve_history(self):
        oid = self.reviewed();self.ledger.clarify(self.admin,oid,'note',1002)
        self.ledger.reject(self.admin,oid,1003)
        with self.assertRaises(ValueError): self.ledger.submit_receipt(111,oid,'new-fixture',1004)
        self.assertEqual(self.ledger.db.execute('SELECT note FROM clarifications').fetchone()[0],'note')

    def test_stage_one_database_additive_upgrade(self):
        oid = self.reviewed();self.approve(oid)
        self.ledger.db.execute('DROP TABLE clarifications'); self.ledger.db.commit()
        self.ledger.close();self.ledger = Ledger(self.path,self.admin)
        self.assertTrue(self.ledger.active(111,1003))
        self.assertEqual(self.ledger.order_status(111,oid)['state'],'approved')


if __name__ == '__main__': unittest.main()
