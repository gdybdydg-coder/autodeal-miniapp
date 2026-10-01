import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ledger import Ledger, DAY


class Outbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'outbox.sqlite')
        self.admin = 777292211
        self.ledger = Ledger(self.path, self.admin)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def review(self, uid=111, now=1000):
        oid = self.ledger.create_order(uid, 249, now)
        self.ledger.submit_receipt(uid, oid, f'SYNTH-RECEIPT-{uid}', now + 1)
        return oid

    def approve(self, oid, now=1004, payment_ref='SYNTH-PAY-001'):
        return self.ledger.approve(self.admin, oid, payment_ref=payment_ref,
            actual_amount=249, bank_verified=True, now=now)

    def capacity(self, limit):
        self.ledger.close()
        self.path = str(Path(self.tmp.name) / f'capacity-{limit}.sqlite')
        self.ledger = Ledger(self.path, self.admin, outbox_capacity=limit)

    def delivered(self, event, token='SYNTH-WORKER', now=1005):
        self.assertTrue(self.ledger.prepare_outbox(event[0], token, now))
        self.assertTrue(self.ledger.mark_outbox_delivered(event[0], token, now + 1))

    def test_approval_does_not_dispatch_old_review_or_clarification(self):
        oid = self.review()
        self.ledger.clarify(self.admin, oid, 'Уточніть квитанцію', 1002)
        self.ledger.submit_receipt(111, oid, 'SYNTH-CORRECTED', 1003)
        self.approve(oid)
        events = self.ledger.claim_outbox('SYNTH-WORKER', 1005)
        self.assertEqual([event[3] for event in events], ['membership_approved'])

    def test_active_outbox_does_not_grow_past_default_capacity(self):
        for uid in range(1000, 1105):
            self.review(uid)
        count = self.ledger.db.execute(
            "SELECT count(*) FROM outbox WHERE state IN ('pending','claimed','uncertain')"
        ).fetchone()[0]
        self.assertLessEqual(count, 100)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 5)

    def test_capacity_does_not_rollback_approval_or_forget_deferred_notice_on_restart(self):
        self.capacity(1)
        oid = self.review()
        held = self.ledger.claim_outbox('SYNTH-OLD-WORKER', 1002)[0]
        self.ledger.seed_search_guard_fixture(111, enabled=False, epoch=9, sent_claims=13, uncertain_claims=2)
        expiry = self.approve(oid)
        self.assertTrue(self.ledger.active(111, 1005))
        self.assertEqual(self.ledger.outbox_status()['active'], 1)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertEqual(self.ledger.outbox_capacity, 1)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        # The old claim has not been sent. Its preflight cancels it and frees
        # a slot; this does not retry an uncertain transmission.
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-OLD-WORKER', 1005))
        events = self.ledger.claim_outbox('SYNTH-WORKER', 1006)
        self.assertEqual([(e[3], e[4]) for e in events], [('membership_approved', str(expiry))])
        self.delivered(events[0], now=1007)
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER-NEW', 1009), [])
        guard = self.ledger.entitlement(111, 1009)
        self.assertEqual((guard['search_enabled'], guard['epoch'], guard['sent_claims'], guard['uncertain_claims']),
                         (False, 9, 13, 2))

    def test_deferred_source_coalesces_to_current_revision(self):
        self.capacity(1)
        self.review(222)
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        oid = self.review()
        self.ledger.clarify(self.admin, oid, 'SYNTH-NOTE', 1002)
        self.ledger.submit_receipt(111, oid, 'SYNTH-CORRECTED', 1003)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        self.delivered(held)
        events = self.ledger.claim_outbox('SYNTH-NEXT', 1007)
        self.assertEqual([(e[1], e[3]) for e in events], [(f'{oid}:review:2', 'review_requested')])
        self.assertEqual(self.ledger.outbox_status()['deferred'], 0)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM clarifications').fetchone()[0], 1)

    def test_old_revision_claim_is_cancelled_before_hypothetical_send(self):
        oid = self.review()
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        self.ledger.clarify(self.admin, oid, 'SYNTH-NOTE', 1002)
        self.ledger.submit_receipt(111, oid, 'SYNTH-CORRECTED', 1003)
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1004))
        events = self.ledger.claim_outbox('SYNTH-NEXT', 1005)
        self.assertEqual([e[1] for e in events], [f'{oid}:review:2'])

    def test_pending_expiry_and_old_activation_are_superseded_by_renewal(self):
        first = self.review()
        expiry = self.approve(first)
        self.assertTrue(self.ledger.record_expiry(111, expiry))
        second = self.review(now=expiry)
        renewed = self.approve(second, now=expiry + 2, payment_ref='SYNTH-PAY-002')
        events = self.ledger.claim_outbox('SYNTH-WORKER', expiry + 3)
        self.assertEqual([(e[3], e[4]) for e in events], [('membership_approved', str(renewed))])
        self.assertFalse(self.ledger.record_expiry(111, expiry + 3))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM orders WHERE state=\'approved\'').fetchone()[0], 2)

    def test_claimed_expiry_is_cancelled_after_renewal(self):
        expiry = self.approve(self.review())
        self.ledger.record_expiry(111, expiry)
        held = self.ledger.claim_outbox('SYNTH-WORKER', expiry + 1)[0]
        self.assertEqual(held[3], 'membership_expired')
        second = self.review(now=expiry + 1)
        self.approve(second, now=expiry + 3, payment_ref='SYNTH-PAY-002')
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', expiry + 4))
        self.assertFalse(self.ledger.mark_outbox_delivered(held[0], 'SYNTH-WORKER', expiry + 5))

    def test_uncertain_claim_consumes_capacity_and_is_never_retried_or_cancelled(self):
        self.capacity(1)
        oid = self.review()
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        self.assertTrue(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1002))
        self.assertTrue(self.ledger.mark_outbox_uncertain(held[0], 'SYNTH-WORKER', 1003))
        self.approve(oid)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertEqual(self.ledger.claim_outbox('SYNTH-NEXT', 1005), [])
        self.assertEqual(self.ledger.outbox_status()['states'], {'uncertain': 1})
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        self.assertTrue(self.ledger.active(111, 1005))
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1006))

    def test_preflight_is_owned_and_can_be_used_only_once(self):
        self.review()
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-OTHER', 1003))
        self.assertTrue(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1003))
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1004))
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertFalse(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1005))

    def test_expired_activation_is_not_dispatched_as_active_access(self):
        expiry = self.approve(self.review())
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER', expiry), [])
        self.assertFalse(self.ledger.active(111, expiry))
        self.assertTrue(self.ledger.record_expiry(111, expiry))
        self.assertEqual(self.ledger.claim_outbox('SYNTH-NEXT', expiry + 1)[0][3], 'membership_expired')

    def test_rejection_suppresses_pending_clarification_but_preserves_audit(self):
        oid = self.review()
        self.ledger.clarify(self.admin, oid, 'SYNTH-NOTE', 1002)
        self.ledger.reject(self.admin, oid, 1003)
        self.assertEqual([e[3] for e in self.ledger.claim_outbox('SYNTH-WORKER', 1004)], ['payment_rejected'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM audit').fetchone()[0], 4)

    def test_capacity_is_shared_by_concurrent_writers(self):
        self.capacity(2)
        def run(uid):
            ledger = Ledger(self.path, self.admin)
            try:
                oid = ledger.create_order(uid, 249, 1000)
                ledger.submit_receipt(uid, oid, f'SYNTH-RECEIPT-{uid}', 1001)
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run, range(1000, 1004)))
        self.assertEqual(self.ledger.outbox_status()['active'], 2)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 2)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM orders WHERE state=\'review\'').fetchone()[0], 4)

    def test_renewals_coalesce_deferred_activations_without_deleting_payments(self):
        self.capacity(1)
        self.review(222)
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        self.assertTrue(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1002))
        self.ledger.mark_outbox_uncertain(held[0], 'SYNTH-WORKER', 1003)
        first = self.review()
        first_expiry = self.approve(first)
        second = self.review(now=2000)
        expiry = self.approve(second, now=2002, payment_ref='SYNTH-PAY-002')
        self.assertEqual(expiry, first_expiry + 30 * DAY)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        self.assertEqual(self.ledger.db.execute('SELECT id FROM orders WHERE notice_pending=1').fetchall(), [(second,)])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM orders WHERE payment_ref IS NOT NULL").fetchone()[0], 2)

    def test_deferred_expiry_is_cleared_by_renewal_under_backpressure(self):
        self.capacity(1)
        self.review(222)
        held = self.ledger.claim_outbox('SYNTH-WORKER', 1002)[0]
        self.assertTrue(self.ledger.prepare_outbox(held[0], 'SYNTH-WORKER', 1002))
        self.ledger.mark_outbox_uncertain(held[0], 'SYNTH-WORKER', 1003)
        expiry = self.approve(self.review())
        self.assertTrue(self.ledger.record_expiry(111, expiry))
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertFalse(self.ledger.record_expiry(111, expiry + 1))
        self.assertEqual(self.ledger.db.execute('SELECT notice_pending FROM memberships WHERE uid=111').fetchone()[0], 1)
        second = self.review(now=expiry + 1)
        self.approve(second, now=expiry + 3, payment_ref='SYNTH-PAY-002')
        self.assertEqual(self.ledger.db.execute('SELECT notice_pending,expiry_notice_at FROM memberships WHERE uid=111').fetchone(), (0, None))
        self.assertEqual(self.ledger.outbox_status()['deferred'], 1)
        self.assertTrue(self.ledger.active(111, expiry + 4))

    def test_existing_stage3_expiry_key_is_not_recorded_twice_after_upgrade(self):
        expiry = self.approve(self.review())
        self.assertTrue(self.ledger.record_expiry(111, expiry))
        with self.ledger.db:
            # This is the exact additive-migration value of the new marker
            # when an old expiry notice already exists.
            self.ledger.db.execute('UPDATE memberships SET expiry_notice_at=NULL')
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertFalse(self.ledger.record_expiry(111, expiry + 1))
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM outbox WHERE kind='membership_expired'").fetchone()[0], 1)

    def test_legacy_oversized_spool_stops_admission_and_drains_without_data_loss(self):
        for uid in range(1000, 1105):
            self.review(uid)
        with self.ledger.db:
            # An imported legacy spool may already be above its configured cap.
            # This fixture is never a production configuration mutation.
            self.ledger.db.execute("UPDATE fixture_settings SET value=1 WHERE name='outbox_capacity'")
        self.ledger.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertEqual(self.ledger.outbox_status()['active'], 100)
        seen = []
        for i in range(105):
            events = self.ledger.claim_outbox('SYNTH-WORKER', 1005 + i * 3, limit=1)
            self.assertEqual(len(events), 1)
            seen.append(events[0][0])
            self.delivered(events[0], now=1006 + i * 3)
            if i < 99:
                self.assertEqual(self.ledger.outbox_status()['active'], 99 - i)
        self.assertEqual(len(set(seen)), 105)
        self.assertEqual(self.ledger.outbox_status()['deferred'], 0)
        self.assertEqual(self.ledger.outbox_status()['states'], {'delivered': 105})
        self.assertEqual(self.ledger.claim_outbox('SYNTH-NEXT', 2000), [])

    def test_invalid_capacity_and_worker_override_are_rejected(self):
        for value in [0, -1, True, 1.5, '1', 1001]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Ledger(str(Path(self.tmp.name) / 'invalid.sqlite'), self.admin, outbox_capacity=value)
        with self.assertRaises(ValueError):
            Ledger(self.path, self.admin, outbox_capacity=99)
        self.assertEqual(self.ledger.outbox_status()['capacity'], 100)

    def test_unknown_pending_notice_fails_closed(self):
        with self.ledger.db:
            self.ledger.db.execute(
                "INSERT INTO outbox(dedupe_key,uid,kind,payload,created) VALUES ('SYNTH-UNKNOWN',111,'unknown','x',1000)"
            )
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER', 1001), [])
        self.assertEqual(self.ledger.db.execute('SELECT state,reason FROM outbox').fetchone(),
                         ('cancelled', 'unknown_notice'))

    def test_old_schema_migration_preserves_terminal_history_and_membership(self):
        self.ledger.close()
        self.path = str(Path(self.tmp.name) / 'legacy.sqlite')
        db = sqlite3.connect(self.path)
        db.executescript('''
            CREATE TABLE orders(id TEXT PRIMARY KEY,uid INTEGER,amount INTEGER,days INTEGER,
                state TEXT,created INTEGER,receipt TEXT,payment_ref TEXT UNIQUE,
                approved_by INTEGER,approved_at INTEGER,expires_at INTEGER);
            CREATE TABLE memberships(uid INTEGER PRIMARY KEY,expires_at INTEGER NOT NULL);
            CREATE TABLE outbox(id INTEGER PRIMARY KEY,dedupe_key TEXT UNIQUE,uid INTEGER,
                kind TEXT,payload TEXT,state TEXT DEFAULT 'pending',created INTEGER);
            INSERT INTO orders VALUES('AD-LEGACY',111,249,30,'approved',1000,'SYNTH-RECEIPT',
                'SYNTH-PAY-LEGACY',777292211,1002,2593002);
            INSERT INTO memberships VALUES(111,2593002);
            INSERT INTO outbox VALUES(1,'AD-LEGACY:approved',111,'membership_approved','2593002','delivered',1002);
            INSERT INTO outbox VALUES(2,'SYNTH-OLD-UNCERTAIN',111,'unknown','x','uncertain',1001);
        ''')
        db.close()
        self.ledger = Ledger(self.path, self.admin)
        self.assertTrue(self.ledger.active(111, 1003))
        self.assertEqual(self.ledger.claim_outbox('SYNTH-WORKER', 1003), [])
        self.assertEqual(self.ledger.outbox_status()['states'], {'delivered': 1, 'uncertain': 1})
        self.assertEqual(self.ledger.db.execute('SELECT payment_ref FROM orders').fetchone()[0], 'SYNTH-PAY-LEGACY')


if __name__ == '__main__':
    unittest.main()
