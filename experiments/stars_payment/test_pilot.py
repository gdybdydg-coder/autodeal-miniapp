import tempfile
import unittest
from pathlib import Path
from pilot import Pilot


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/"pilot.sqlite")
        self.pilot = Pilot(self.path, 777292211, enabled=True)
        self.uid = 777292211

    def tearDown(self):
        self.pilot.close()
        self.tmp.cleanup()

    def payment(self):
        invoice = self.pilot.invoice(self.uid, self.uid, 1000)
        return {"invoice_payload": invoice["payload"], "currency": "XTR",
                "total_amount": 1, "telegram_payment_charge_id": "test-charge"}

    def test_hidden_and_server_rejected_for_other_users_and_groups(self):
        for uid, chat in [(123, 123), (self.uid, -123), (str(self.uid), self.uid)]:
            self.assertFalse(self.pilot.visible(uid, chat))
            self.assertIsNone(self.pilot.invoice(uid, chat, 1000))
        payment = self.payment()
        self.assertFalse(self.pilot.precheckout(123, payment, 1001))
        self.assertEqual(self.pilot.successful_payment(123, 123, payment, 1001), "rejected")

    def test_disabled_default(self):
        other = Pilot(self.path, self.uid)
        self.assertIsNone(other.invoice(self.uid, self.uid, 1000))
        other.close()

    def test_invoice_and_precheckout_do_not_activate(self):
        payment = self.payment()
        self.assertTrue(self.pilot.precheckout(self.uid, payment, 1001))
        self.assertEqual(self.pilot.db.execute("SELECT state FROM orders").fetchone()[0], "pending")

    def test_tampered_currency_amount_payload_and_expiry(self):
        payment = self.payment()
        for field, value in [("currency", "UAH"), ("total_amount", 2),
                             ("total_amount", True), ("invoice_payload", "forged")]:
            bad = dict(payment, **{field: value})
            self.assertFalse(self.pilot.precheckout(self.uid, bad, 1001))
            self.assertEqual(self.pilot.successful_payment(self.uid, self.uid, bad, 1001), "rejected")
        self.assertFalse(self.pilot.precheckout(self.uid, payment, 1901))
        self.assertFalse(self.pilot.precheckout(self.uid, payment, 999))

    def test_success_and_restart_deduplication(self):
        payment = self.payment()
        self.assertEqual(self.pilot.successful_payment(self.uid, self.uid, payment, 1001), "recorded_test_only")
        self.pilot.close()
        self.pilot = Pilot(self.path, self.uid, enabled=True)
        self.assertEqual(self.pilot.successful_payment(self.uid, self.uid, payment, 1002), "duplicate")
        self.assertEqual(self.pilot.db.execute("SELECT paid_until FROM orders").fetchone()[0], 1001+30*86400)

    def test_missing_or_reused_charge(self):
        payment = self.payment()
        self.assertEqual(self.pilot.successful_payment(self.uid, self.uid,
                         dict(payment, telegram_payment_charge_id=""), 1001), "rejected")
        self.pilot.successful_payment(self.uid, self.uid, payment, 1001)
        second = self.payment()
        self.assertEqual(self.pilot.successful_payment(self.uid, self.uid, second, 1002), "rejected")


if __name__ == "__main__":
    unittest.main()
