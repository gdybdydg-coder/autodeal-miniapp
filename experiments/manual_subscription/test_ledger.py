import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ledger import CLOCK_SKEW, DAY, MAX_RECEIPT_AGE, Ledger


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'ledger.db')
        self.ledger = Ledger(self.path, 777292211)
        self.admin = 777292211

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def reviewed(self, uid=111, now=1001):
        oid = self.ledger.create_order(uid, 249, 1000)
        self.ledger.submit_receipt(
            uid, oid, f'SYNTH-RECEIPT-{uid}', now, paid_at=1001
        )
        return oid

    def approve(self, oid, reference='SYNTH-BANK-TXN-1', now=1002):
        return self.ledger.approve(
            self.admin, oid, payment_ref=reference,
            actual_amount=249, bank_verified=True, now=now,
        )

    def test_receipt_does_not_grant_access(self):
        self.reviewed()
        self.assertFalse(self.ledger.active(111, 1002))

    def test_duplicate_create_reuses_all_open_states_with_snapshot_price(self):
        first = self.ledger.create_order(111, 249, 1000)
        self.assertEqual(self.ledger.create_order(111, 299, 1001), first)
        self.ledger.submit_receipt(111, first, 'SYNTH-RECEIPT-111', 1002, paid_at=1001)
        self.ledger.request_clarification(self.admin, first, 'Synthetic detail required', 1003)
        self.assertEqual(self.ledger.create_order(111, 399, 1004), first)
        self.assertEqual(self.ledger.db.execute('SELECT amount FROM orders').fetchone()[0], 249)

    def test_non_admin_and_other_clients_cannot_change_review(self):
        oid = self.reviewed()
        with self.assertRaises(PermissionError):
            self.ledger.approve(111, oid, payment_ref='SYNTH-REF', actual_amount=249,
                                bank_verified=True, now=1002)
        with self.assertRaises(PermissionError):
            self.ledger.submit_receipt(222, oid, 'SYNTH-REF', 1002, paid_at=1001)
        with self.assertRaises(PermissionError):
            self.ledger.reject(111, oid, 1002)
        with self.assertRaises(PermissionError):
            self.ledger.request_clarification(111, oid, 'Not authorized', 1002)

    def test_missing_bank_verification_and_wrong_amount_rejected(self):
        oid = self.reviewed()
        for amount, verified in [(249, False), (248, True), (True, True)]:
            with self.assertRaises(ValueError):
                self.ledger.approve(
                    self.admin, oid, payment_ref='SYNTH-REF', actual_amount=amount,
                    bank_verified=verified, now=1002,
                )
        self.assertFalse(self.ledger.active(111, 1002))

    def test_approval_requires_receipt(self):
        oid = self.ledger.create_order(111, 249, 1000)
        with self.assertRaises(ValueError):
            self.approve(oid)

    def test_receipt_reference_is_bounded_and_path_free(self):
        oid = self.ledger.create_order(111, 249, 1000)
        for reference in ('', 'ab', '../receipt.pdf', 'receipt name', 'x' * 101, 'квитанція'):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.ledger.submit_receipt(111, oid, reference, 1001, paid_at=1001)
        self.ledger.submit_receipt(111, oid, 'SYNTH.RECEIPT_001-PDF', 1001, paid_at=1001)

    def test_receipt_time_rejects_before_order_old_and_future_values(self):
        base = 1_000_000
        for offset in (-1, MAX_RECEIPT_AGE + 1, -(CLOCK_SKEW + 1)):
            uid = base + offset + 2_000_000
            oid = self.ledger.create_order(uid, 249, base)
            if offset == -1:
                paid_at, now = base - 1, base
            elif offset > 0:
                paid_at, now = base, base + offset
            else:
                paid_at, now = base - offset, base
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.ledger.submit_receipt(uid, oid, f'SYNTH-{uid}', now, paid_at=paid_at)

    def test_clarification_resubmit_is_durable_and_audited(self):
        oid = self.reviewed()
        self.ledger.request_clarification(self.admin, oid, '  Add synthetic timestamp  ', 1002)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        state, note, revision = self.ledger.db.execute(
            'SELECT state,clarification_note,receipt_revision FROM orders WHERE id=?', (oid,)
        ).fetchone()
        self.assertEqual((state, note, revision), ('clarification', 'Add synthetic timestamp', 1))
        self.ledger.submit_receipt(111, oid, 'SYNTH-RESUBMIT-002', 1003, paid_at=1002)
        row = self.ledger.db.execute(
            'SELECT state,clarification_note,receipt_revision FROM orders WHERE id=?', (oid,)
        ).fetchone()
        self.assertEqual(row, ('review', None, 2))
        actions = [r[0] for r in self.ledger.db.execute(
            'SELECT action FROM audit WHERE order_id=? ORDER BY id', (oid,)
        )]
        self.assertEqual(actions, [
            'created', 'receipt_submitted', 'clarification_requested', 'receipt_resubmitted'
        ])

    def test_clarification_note_is_required_and_bounded(self):
        oid = self.reviewed()
        for note in ('', ' ', 'x' * 501):
            with self.subTest(note_len=len(note)), self.assertRaises(ValueError):
                self.ledger.request_clarification(self.admin, oid, note, 1002)

    def test_approval_and_expiry_boundary(self):
        expiry = self.approve(self.reviewed())
        self.assertEqual(expiry, 1002 + 30 * DAY)
        self.assertTrue(self.ledger.active(111, expiry - 1))
        self.assertFalse(self.ledger.active(111, expiry))

    def test_duplicate_click_and_restart_do_not_extend_or_enqueue_twice(self):
        oid = self.reviewed()
        expiry = self.approve(oid)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertEqual(self.approve(oid, now=2000), expiry)
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM audit WHERE action='approved'").fetchone()[0], 1
        )
        self.assertEqual(
            self.ledger.db.execute(
                "SELECT count(*) FROM outbox WHERE kind='membership_approved'"
            ).fetchone()[0], 1
        )

    def test_outbox_delivery_survives_restart(self):
        oid = self.reviewed()
        self.approve(oid)
        claimed = self.ledger.claim_outbox('WORKER-001', 1003)
        event = next(row for row in claimed if row[3] == 'membership_approved')
        self.assertTrue(self.ledger.mark_outbox_delivered(event[0], 'WORKER-001', 1004))
        self.assertFalse(self.ledger.mark_outbox_delivered(event[0], 'WORKER-001', 1005))
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertNotIn(event[0], [row[0] for row in self.ledger.pending_outbox()])
        self.assertEqual(
            self.ledger.db.execute('SELECT state,delivered_at FROM outbox WHERE id=?', (event[0],)).fetchone(),
            ('delivered', 1004),
        )

    def test_uncertain_outbox_is_not_retried_after_restart(self):
        self.reviewed()
        event = self.ledger.claim_outbox('WORKER-002', 1002, limit=1)[0]
        self.assertTrue(self.ledger.mark_outbox_uncertain(event[0], 'WORKER-002', 1003))
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertEqual(
            self.ledger.db.execute('SELECT state FROM outbox WHERE id=?', (event[0],)).fetchone()[0],
            'uncertain',
        )
        self.assertNotIn(event[0], [row[0] for row in self.ledger.pending_outbox()])
        self.assertEqual(self.ledger.claim_outbox('WORKER-003', 1004), [])

    def test_single_bank_payment_cannot_grant_two_clients(self):
        self.approve(self.reviewed(111))
        with self.assertRaises(ValueError):
            self.approve(self.reviewed(222))
        self.assertFalse(self.ledger.active(222, 1002))

    def test_renewal_adds_to_active_expiry(self):
        expiry = self.approve(self.reviewed())
        renewal = self.reviewed()
        self.assertEqual(self.approve(renewal, 'SYNTH-BANK-TXN-2', 2000), expiry + 30 * DAY)

    def test_expired_membership_starts_from_confirmation(self):
        expiry = self.approve(self.reviewed())
        self.assertEqual(
            self.approve(self.reviewed(), 'SYNTH-BANK-TXN-2', expiry + 100),
            expiry + 100 + 30 * DAY,
        )

    def test_rejected_order_cannot_be_approved(self):
        oid = self.reviewed()
        self.ledger.reject(self.admin, oid, 1002)
        with self.assertRaises(ValueError):
            self.approve(oid)

    def test_stop_epoch_and_claim_fixtures_survive_approval_and_renewal(self):
        self.ledger.seed_search_guard_fixture(
            111, enabled=True, epoch=7, sent_claims=13, uncertain_claims=2
        )
        self.assertEqual(self.ledger.stop_search(111, 1000), 8)
        self.assertEqual(self.ledger.stop_search(111, 1001), 8)
        self.approve(self.reviewed())
        self.approve(self.reviewed(), 'SYNTH-BANK-TXN-2', 2000)
        self.assertEqual(self.ledger.entitlement(111, 2001), {
            'membership_active': True,
            'search_enabled': False,
            'effective_access': False,
            'epoch': 8,
            'sent_claims': 13,
            'uncertain_claims': 2,
        })

    def test_expiry_notice_is_deduplicated_without_mutating_search_guard(self):
        self.ledger.seed_search_guard_fixture(
            111, enabled=False, epoch=9, sent_claims=21, uncertain_claims=3
        )
        expiry = self.approve(self.reviewed())
        self.assertFalse(self.ledger.record_expiry(111, expiry - 1))
        self.assertTrue(self.ledger.record_expiry(111, expiry))
        self.assertFalse(self.ledger.record_expiry(111, expiry + 1))
        snapshot = self.ledger.entitlement(111, expiry)
        self.assertEqual((snapshot['search_enabled'], snapshot['epoch']), (False, 9))
        self.assertEqual((snapshot['sent_claims'], snapshot['uncertain_claims']), (21, 3))
        self.assertEqual(
            self.ledger.db.execute(
                "SELECT count(*) FROM outbox WHERE kind='membership_expired'"
            ).fetchone()[0], 1
        )

    def test_concurrent_duplicate_approval_grants_once(self):
        oid = self.reviewed()

        def run(_):
            ledger = Ledger(self.path, self.admin)
            try:
                return ledger.approve(
                    self.admin, oid, payment_ref='SYNTH-BANK-TXN-1',
                    actual_amount=249, bank_verified=True, now=1002,
                )
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(
            self.ledger.db.execute("SELECT count(*) FROM audit WHERE action='approved'").fetchone()[0], 1
        )

    def test_stage1_database_is_migrated_without_losing_order(self):
        other = str(Path(self.tmp.name) / 'stage1.db')
        db = sqlite3.connect(other)
        db.executescript('''
            CREATE TABLE orders (id TEXT PRIMARY KEY, uid INTEGER NOT NULL, amount INTEGER NOT NULL,
              days INTEGER NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL, receipt TEXT,
              payment_ref TEXT UNIQUE, approved_by INTEGER, approved_at INTEGER, expires_at INTEGER);
            CREATE TABLE memberships (uid INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL);
            CREATE TABLE audit (id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
              action TEXT NOT NULL, at INTEGER NOT NULL);
            INSERT INTO orders (id,uid,amount,days,state,created) VALUES ('AD-OLD',111,249,30,'awaiting',1000);
        ''')
        db.close()
        migrated = Ledger(other, self.admin)
        try:
            columns = {r[1] for r in migrated.db.execute('PRAGMA table_info(orders)')}
            self.assertTrue({'receipt_at', 'receipt_revision', 'clarification_note', 'clarified_at'} <= columns)
            self.assertEqual(migrated.db.execute('SELECT id FROM orders').fetchone()[0], 'AD-OLD')
        finally:
            migrated.close()

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
        self.ledger.submit_receipt(111,oid,'SYNTH-RECEIPT-111',1002)
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


if __name__ == '__main__':
    unittest.main()
