import base64
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ledger import Ledger
from receipt_store import ReceiptStore, MAX_FILE_BYTES, MAX_ORDER_FILES

# A non-sensitive one-pixel PNG, not a bank document or a customer upload.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6dZkAAAAASUVORK5CYII=')
PDF = b'%PDF-1.4\n% AUTODeal synthetic receipt - no transfer\n%%EOF\n'
ADMIN = 777292211


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fixture.sqlite'
        self.ledger = Ledger(self.path,ADMIN)
        self.store = ReceiptStore(self.ledger)
        self.oid = self.ledger.create_order(111,249,1000)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def upload(self, data=PNG, mime='image/png', now=1001):
        return self.store.submit(111,self.oid,mime,data,now)

    def count(self):
        return self.ledger.db.execute('SELECT count(*) FROM local_receipt_files').fetchone()[0]

    def test_file_is_saved_with_order_but_never_grants_access(self):
        rid = self.upload()
        self.assertEqual(self.ledger.order_status(111,self.oid)['state'],'review')
        self.assertFalse(self.ledger.active(111,1001))
        metadata = self.store.metadata(111,self.oid)
        self.assertEqual(metadata['id'],rid)
        self.assertEqual(metadata['size'],len(PNG))
        self.assertNotIn('filename',metadata)
        self.assertEqual(self.store.download(ADMIN,rid),('image/png',PNG))

    def test_owner_and_admin_boundaries_apply_even_for_known_receipt_id(self):
        rid = self.upload()
        with self.assertRaises(PermissionError):
            self.store.submit(222,self.oid,'image/png',PNG,1002)
        with self.assertRaises(PermissionError):
            self.store.metadata(222,self.oid)
        for uid in [111,222]:
            with self.assertRaises(PermissionError):
                self.store.download(uid,rid)
        self.assertEqual(self.count(),1)

    def test_bad_format_zero_and_oversized_files_leave_order_untouched(self):
        for mime,data in [('text/html',b'<script>alert(1)</script>'),('image/png',b'not PNG'),
                          ('image/jpeg',PNG),('image/png',b''),('image/png',PNG+b'x'*MAX_FILE_BYTES)]:
            with self.assertRaises(ValueError):
                self.store.submit(111,self.oid,mime,data,1001)
        self.assertEqual(self.count(),0)
        self.assertEqual(self.ledger.order_status(111,self.oid)['state'],'awaiting')

    def test_file_and_order_audit_outbox_write_rollback_together(self):
        with patch.object(self.ledger,'_queue_order_notice',side_effect=RuntimeError('synthetic interrupted write')):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertEqual(self.count(),0)
        self.assertEqual(self.ledger.order_status(111,self.oid)['receipt_revision'],0)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM audit').fetchone()[0],1)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_duplicate_upload_is_idempotent_and_clarification_can_reuse_file(self):
        rid = self.upload()
        self.assertEqual(self.upload(now=1002),rid)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.ledger.order_status(111,self.oid)['receipt_revision'],1)
        self.ledger.clarify(ADMIN,self.oid,'Уточни час',1003)
        self.assertEqual(self.upload(now=1004),rid)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.ledger.order_status(111,self.oid)['receipt_revision'],2)

    def test_restart_preserves_bytes_review_and_approval_does_not_delete_file(self):
        rid = self.upload(PDF,'application/pdf')
        self.ledger.close()
        self.ledger = Ledger(self.path,ADMIN)
        self.store = ReceiptStore(self.ledger)
        self.assertEqual(self.store.download(ADMIN,rid),('application/pdf',PDF))
        self.ledger.approve(ADMIN,self.oid,payment_ref='SYNTH-PAID-1',actual_amount=249,bank_verified=True,now=1002)
        self.assertEqual(self.store.download(ADMIN,rid)[1],PDF)
        with self.assertRaises(ValueError):
            self.upload(PNG,now=1003)

    def test_file_count_and_bytes_budget_reject_without_losing_evidence(self):
        for i in range(MAX_ORDER_FILES):
            self.upload(PNG+bytes([i]),now=1001+i*2)
            self.ledger.clarify(ADMIN,self.oid,'Уточнення',1002+i*2)
        with self.assertRaises(ValueError):
            self.upload(PDF,'application/pdf',now=1020)
        self.assertEqual(self.count(),MAX_ORDER_FILES)
        self.assertEqual(self.ledger.order_status(111,self.oid)['state'],'clarification')
        # Independent global byte-cap failure in a new order, no history removal.
        second = self.ledger.create_order(222,249,1021)
        with patch('receipt_store.MAX_TOTAL_BYTES',len(PNG)):
            with self.assertRaises(ValueError):
                self.store.submit(222,second,'image/png',PNG,1022)
        self.assertEqual(self.ledger.order_status(222,second)['state'],'awaiting')
        with patch('receipt_store.MAX_FILES',MAX_ORDER_FILES):
            with self.assertRaises(ValueError):
                self.store.submit(222,second,'image/png',PNG,1022)

    def test_concurrent_retries_leave_one_file_and_one_revision(self):
        def upload(_):
            ledger = Ledger(self.path,ADMIN)
            try:
                return ReceiptStore(ledger).submit(111,self.oid,'image/png',PNG,1001)
            finally:
                ledger.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(upload,range(4)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.ledger.order_status(111,self.oid)['receipt_revision'],1)


if __name__ == '__main__':
    unittest.main()
