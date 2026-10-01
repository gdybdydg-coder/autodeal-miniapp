import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from review_queue import ReviewLedger, ReviewAPI, bank_key
from adapter import FixtureSessions
from receipt_store import ReceiptStore
from ledger import DAY


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'review.sqlite')
        self.db = ReviewLedger(self.path, 900)
        self.sessions = FixtureSessions()
        self.owner = self.sessions.issue(900)
        self.client = self.sessions.issue(111)
        self.api = ReviewAPI(self.db,self.sessions)

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def paid(self, uid=111, now=1000):
        code = self.db.create_order(uid,250,now)
        self.db.paid(uid,code,now+1)
        return code

    def preview(self, code, transaction='BANK-001', now=1002):
        return self.db.preview(900,code,now,account='ACCOUNT-A',transaction=transaction,actual_amount=250,bank_verified=True)

    def test_no_receipt_first_activation_atomic_history(self):
        code = self.paid()
        self.assertFalse(self.db.active(111,1001))
        preview = self.preview(code)
        self.assertEqual(preview['days'],30)
        expiry = self.db.confirm(900,preview['confirmation'],1003)
        self.assertEqual(expiry,1003+30*DAY)
        grant = self.db.db.execute('SELECT actor,at,before_expiry,after_expiry FROM review_grants').fetchone()
        self.assertEqual(grant,(900,1003,0,expiry))

    def test_repeat_paid_and_late_evidence_keep_one_request(self):
        code = self.paid()
        for _ in range(3):self.db.paid(111,code,1002)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM review_evidence').fetchone()[0],1)
        self.db.paid(111,code,1003,amount=250,transfer_at=1000,note='Тестовий переказ')
        store = ReceiptStore(self.db)
        rid = store.submit(111,code,'application/pdf',b'%PDF-SYNTHETIC ONLY',1004)
        self.assertEqual(store.submit(111,code,'application/pdf',b'%PDF-SYNTHETIC ONLY',1005),rid)
        self.assertEqual(self.db.order_status(111,code)['receipt'],rid)
        self.assertEqual(self.db.queue(900,1006)['total'],1)

    def test_late_receipt_is_not_bank_uniqueness_key(self):
        a,b = self.paid(111),self.paid(222)
        for uid,code in [(111,a),(222,b)]:
            self.db.paid(uid,code,1002,receipt='SAME-SCREENSHOT-FILENAME')
        self.db.confirm(900,self.preview(a)['confirmation'],1003)
        with self.assertRaises(ValueError):self.preview(b,' bank-001 ')
        self.db.confirm(900,self.preview(b,'BANK-002')['confirmation'],1003)
        self.assertTrue(self.db.active(222,1004))

    def test_multiple_orders_same_client_and_preserve_existing_gift(self):
        with self.db.db:self.db.db.execute('INSERT INTO memberships(uid,expires_at) VALUES (111,?)',(1000+10*DAY,))
        a=self.paid(); self.assertEqual(self.db.create_order(111,999,1001),a)
        first=self.db.confirm(900,self.preview(a)['confirmation'],1003)
        self.assertEqual(first,1000+40*DAY)
        b=self.paid(now=2000); self.assertNotEqual(a,b)
        second=self.db.confirm(900,self.preview(b,'BANK-002',2002)['confirmation'],2003)
        self.assertEqual(second,first+30*DAY)
        self.assertEqual(self.db.queue(900,2004,state='all')['total'],2)

    def test_wrong_amount_clarification_and_rejection_remain_visible(self):
        a=self.paid()
        with self.assertRaises(ValueError):
            self.db.preview(900,a,1002,account='ACCOUNT-A',transaction='BANK-001',actual_amount=249,bank_verified=True)
        self.db.clarify(900,a,'Уточніть суму та час переказу.',1003)
        self.assertEqual(self.db.queue(900,1004,state='clarification')['total'],1)
        self.db.paid(111,a,1005,amount=250)
        self.db.reject_with_reason(900,a,'Зарахування не знайдене. Напишіть у підтримку.',1006)
        self.assertEqual(self.db.queue(900,1007,state='rejected')['total'],1)
        self.assertIn('Зарахування',self.db.order_status(111,a)['rejection_reason'])
        self.assertFalse(self.db.active(111,1007))

    def test_queue_all_pages_search_missing_username_and_view_no_mutation(self):
        ids=[]
        for uid in range(1,126):
            code=self.paid(uid,1000+uid); ids.append(code)
            self.db.person(uid,'Synthetic '+str(uid),None if uid==111 else 'fixture'+str(uid))
        found=[]
        for page in range(1,8):
            found.extend(item['code'] for item in self.db.queue(900,2000,page=page)['items'])
        self.assertEqual(set(ids),set(found)); self.assertEqual(len(found),125)
        self.assertEqual(self.db.queue(900,2000,state='all',search='111')['total'],1)
        item=self.db.queue(900,2000,search=ids[0])['items'][0]
        self.assertEqual(item['code'],ids[0])
        self.db.card(900,ids[0]);self.assertEqual(self.db.queue(900,2000)['waiting'],125)

    def test_new_evidence_stales_confirmation(self):
        code=self.paid();preview=self.preview(code)
        self.db.paid(111,code,1003,note='Додаткові дані')
        with self.assertRaises(ValueError):self.db.confirm(900,preview['confirmation'],1004)
        self.assertFalse(self.db.active(111,1004))

    def test_expiry_change_and_expired_confirmation_require_new_preview(self):
        code=self.paid();preview=self.preview(code)
        with self.assertRaises(ValueError):self.db.confirm(900,preview['confirmation'],1400)
        with self.db.db:self.db.db.execute('INSERT INTO memberships(uid,expires_at) VALUES(111,10000)')
        with self.assertRaises(ValueError):self.db.confirm(900,preview['confirmation'],1003)

    def test_duplicate_concurrent_confirmation_and_restart_once(self):
        code=self.paid();token=self.preview(code)['confirmation']
        def confirm(_):
            db=ReviewLedger(self.path,900)
            try:return db.confirm(900,token,1003)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(confirm,range(4)))
        self.assertEqual(len(set(results)),1)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM review_grants').fetchone()[0],1)
        self.db.close();self.db=ReviewLedger(self.path,900)
        self.assertEqual(self.db.confirm(900,token,5000),results[0])

    def test_database_failure_rolls_back_request_grant_and_outbox(self):
        code=self.paid();token=self.preview(code)['confirmation']
        with patch.object(self.db,'_queue_order_notice',side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):self.db.confirm(900,token,1003)
        self.db.close();self.db=ReviewLedger(self.path,900)
        self.assertEqual(self.db.order_status(111,code)['state'],'review')
        self.assertFalse(self.db.active(111,1004))
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM review_grants').fetchone()[0],0)
        self.db.confirm(900,token,1004)

    def test_owner_notice_failure_queue_intact_client_failure_access_intact(self):
        code=self.paid()
        event=self.db.claim_outbox('WORKER-A',1002)[0]
        self.db.prepare_outbox(event[0],'WORKER-A',1002)
        self.db.mark_failed(event[0],'WORKER-A',1003)
        self.assertEqual(self.db.queue(900,1003)['waiting'],1)
        self.db.retry_known_failure(900,event[0],1004)
        self.assertEqual(len(self.db.pending_outbox()),1)
        self.db.confirm(900,self.preview(code,now=1005)['confirmation'],1006)
        event=self.db.claim_outbox('WORKER-B',1007)[0]
        self.assertEqual(event[3],'membership_approved')
        self.db.prepare_outbox(event[0],'WORKER-B',1007)
        self.db.mark_failed(event[0],'WORKER-B',1008)
        self.assertTrue(self.db.active(111,1009))
        self.db.retry_known_failure(900,event[0],1010)
        self.assertEqual(len(self.db.pending_outbox()),1)

    def test_uncertain_notice_not_silently_retried(self):
        self.paid();event=self.db.claim_outbox('WORKER-A',1002)[0]
        self.db.prepare_outbox(event[0],'WORKER-A',1002)
        self.db.mark_outbox_uncertain(event[0],'WORKER-A',1003)
        self.db.retry_known_failure(900,event[0],1004)
        self.assertEqual(self.db.pending_outbox(),[])

    def test_every_admin_command_and_forged_actor_denied(self):
        code=self.paid();token=self.preview(code)['confirmation']
        for command,data in [('/payments',{}),('card',{'code':code}),('confirm',{'confirmation':token}),
                             ('clarify',{'code':code,'note':'No'}),('reject',{'code':code,'reason':'No'})]:
            with self.subTest(command=command),self.assertRaises(PermissionError):
                self.api.command(self.client,command,data,1003)
        with self.assertRaises(ValueError):self.api.command(self.client,'create',{'actor':900},1003)
        self.sessions.revoke(self.owner)
        with self.assertRaises(PermissionError):self.api.command(self.owner,'confirm',{'confirmation':token},1003)

    def test_summary_disabled_and_bucket_durable(self):
        self.paid()
        self.assertIsNone(self.db.summary(900,4000))
        self.assertEqual(self.db.summary(900,4000,enabled=True)['waiting'],1)
        self.db.close();self.db=ReviewLedger(self.path,900)
        self.assertIsNone(self.db.summary(900,4001,enabled=True))

    def test_quote_snapshot_and_stopped_search_remain_unchanged(self):
        code=self.paid()
        self.db.seed_search_guard_fixture(111,enabled=False,epoch=9,sent_claims=13,uncertain_claims=2)
        self.assertEqual(self.db.create_order(111,500,1002,60),code)
        self.db.confirm(900,self.preview(code)['confirmation'],1003)
        state=self.db.entitlement(111,1004)
        self.assertTrue(state['membership_active']); self.assertFalse(state['effective_access'])
        self.assertEqual((state['epoch'],state['sent_claims'],state['uncertain_claims']),(9,13,2))
        self.assertNotIn('uid',self.db.order_status(111,code))


if __name__=='__main__':unittest.main()
