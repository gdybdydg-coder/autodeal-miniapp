"""Tariff change across durable orders; synthetic amounts/receipts only."""
import tempfile
import unittest
from pathlib import Path

from ledger import Ledger, DAY
from owner_harness import OwnerHarness, ADMIN, CLIENT
from test_bank_profile import private_fixture
from test_receipt_store import PNG


class TariffSnapshotTests(unittest.TestCase):
    def test_prior_quote_is_preserved_after_restart_and_renewal_uses_250(self):
        for previous_state in ('awaiting', 'review'):
            with self.subTest(previous_state=previous_state), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'fixture.sqlite'
                profile = private_fixture(Path(tmp)/'recipient.private.json')
                old = Ledger(path, ADMIN)
                oid = old.create_order(CLIENT, 249, 1000)
                old.seed_search_guard_fixture(CLIENT, enabled=False, epoch=9,
                                              sent_claims=13, uncertain_claims=2)
                if previous_state == 'review':
                    old.submit_receipt(CLIENT, oid, 'SYNTH-OLD-RECEIPT', 1001)
                old.close()
                now = 1002
                app = OwnerHarness(path, lambda:now, recipient_profile=profile)
                tokens = {role:app.sessions.login(app.sessions.issue_invitation(role))['token']
                          for role in ('client','admin')}

                def act(action, order=None, data=None, role='client'):
                    return app.action(tokens[role], {'action':action, 'order_id':order, 'data':data or {}})

                view = act('create')  # An open order must not be silently repriced.
                self.assertEqual((view['amount'],view['days']),(250,30))
                self.assertEqual((view['order']['id'],view['order']['amount']),(oid,249))
                self.assertEqual(view['payment_instruction']['amount'],249)
                self.assertFalse(view['payment_instruction']['tariff_confirmed'])
                self.assertFalse(view['payment_instruction']['payments_enabled'])
                if previous_state == 'awaiting':
                    app.upload(tokens['client'],oid,'image/png',PNG)
                approval = {'payment_ref':'SYNTH-OLD-PAID','actual_amount':249,'bank_verified':True}
                with self.assertRaisesRegex(ValueError,'Payment amount mismatch'):
                    act('approve',oid,{**approval,'actual_amount':250},role='admin')
                self.assertFalse(app.state(tokens['client'])['membership']['active'])
                # Local fixture confirmation of the OLD quote, never real collection.
                expiry = act('approve',oid,approval,role='admin')['membership']['expires_at']
                self.assertEqual(expiry,now+30*DAY)
                now += 10
                app = OwnerHarness(path, lambda:now, recipient_profile=profile)
                self.assertEqual(act('approve',oid,approval,role='admin')['membership']['expires_at'],expiry)
                renewal = act('create')
                new_oid = renewal['order']['id']
                self.assertNotEqual(new_oid,oid)
                self.assertEqual((renewal['order']['amount'],renewal['order']['days']),(250,30))
                self.assertTrue(renewal['payment_instruction']['tariff_confirmed'])
                self.assertFalse(renewal['payment_instruction']['payments_enabled'])
                app.upload(tokens['client'],new_oid,'image/png',PNG)
                paid = {'payment_ref':'SYNTH-NEW-PAID','actual_amount':250,'bank_verified':True}
                renewed = act('approve',new_oid,paid,role='admin')
                self.assertEqual(renewed['membership']['expires_at'],expiry+30*DAY)
                self.assertFalse(renewed['search_enabled'])
                ledger = Ledger(path, ADMIN)
                try:
                    self.assertEqual(ledger.db.execute('SELECT amount FROM orders ORDER BY rowid').fetchall(),[(249,),(250,)])
                    self.assertEqual(ledger.db.execute('SELECT * FROM search_guard_fixtures').fetchone(),(CLIENT,0,9,13,2))
                    self.assertFalse(ledger.active(CLIENT,expiry+30*DAY))
                finally:
                    ledger.close()

    def test_new_quote_rejects_underpayment_overpayment_and_noninteger_amounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'fixture.sqlite'
            app = OwnerHarness(path, lambda:1000)
            tokens = {role:app.sessions.login(app.sessions.issue_invitation(role))['token']
                      for role in ('client','admin')}
            created = app.action(tokens['client'],{'action':'create','order_id':None,'data':{}})
            oid = created['order']['id']
            app.upload(tokens['client'],oid,'image/png',PNG)
            for amount in (249,251,250.0,'250',True):
                with self.subTest(amount=amount), self.assertRaises(ValueError):
                    app.action(tokens['admin'],{'action':'approve','order_id':oid,
                        'data':{'payment_ref':'SYNTH-WRONG-AMOUNT','actual_amount':amount,'bank_verified':True}})
            view = app.state(tokens['client'])
            self.assertEqual(view['order']['state'],'review')
            self.assertFalse(view['membership']['active'])
            self.assertFalse(view['search_enabled'])
            self.assertFalse(view['payments_enabled'])


if __name__ == '__main__':
    unittest.main()
