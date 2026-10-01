import json
import tempfile
import threading
import unittest
from pathlib import Path

import test_owner_harness as helpers
from ledger import Ledger
from owner_harness import OwnerHarness, server, ADMIN
from test_bank_profile import private_fixture, TEST_IBAN, fixture_data


class RecipientHttpTests(unittest.TestCase):
    request = helpers.OwnerHttpTests.request
    action = helpers.OwnerHttpTests.action
    create = helpers.OwnerHttpTests.create

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'fixture.sqlite'
        self.profile = private_fixture(Path(self.tmp.name)/'recipient.private.json')
        self.harness = OwnerHarness(self.path,lambda:1000,recipient_profile=self.profile)
        self.app = server(self.harness)
        self.thread = threading.Thread(target=self.app.serve_forever,daemon=True)
        self.thread.start()
        self.tokens = {role:self.harness.sessions.login(self.harness.sessions.issue_invitation(role))['token']
                       for role in ('client','admin')}

    def tearDown(self):
        self.app.shutdown();self.app.server_close();self.thread.join();self.tmp.cleanup()

    def test_public_assets_and_unauthenticated_routes_never_expose_profile(self):
        for path in ['/client','/admin','/owner-login.js','/owner-transport.js','/payment-ui.js',
                     '/recipient.private.json','/../recipient.private.json','/bank_profile.py']:
            code,body,_ = self.request(path)
            self.assertIn(code,(200,404))
            for key in ['recipient_name','recipient_code','iban','bank_name']:
                self.assertNotIn(fixture_data()[key].encode(),body)
        self.assertEqual(self.request('/api/state')[0],401)

    def test_instructions_are_issued_only_after_own_order_with_server_tariff(self):
        code,body,_ = self.request('/api/state',token=self.tokens['client'])
        self.assertEqual(code,200)
        self.assertIsNone(json.loads(body)['payment_instruction'])
        oid = self.create()
        code,body,headers = self.request('/api/state',token=self.tokens['client'])
        view = json.loads(body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        payment = view['payment_instruction']
        self.assertEqual(payment['iban'],TEST_IBAN)
        self.assertIn(oid,payment['purpose'])
        self.assertEqual(payment['amount'],249)
        self.assertFalse(payment['payments_enabled'])
        self.assertFalse(view['membership']['active'])

    def test_web_payload_cannot_replace_bank_destination_or_price(self):
        for key in ['iban','recipient_name','recipient_code','bank_name','amount','tariff_confirmed']:
            self.assertEqual(self.action('create',data={key:'SYNTH-OVERRIDE'})[0],400)
        self.assertIsNone(self.harness.state(self.tokens['client'])['order'])
        self.create()
        payment = self.harness.state(self.tokens['client'])['payment_instruction']
        self.assertEqual(payment['iban'],TEST_IBAN)

    def test_new_order_changes_purpose_without_resetting_guards_or_storing_bank_values(self):
        first = self.create()
        self.assertEqual(self.action('reject',first,role='admin')[0],200)
        second = self.create()
        self.assertNotEqual(first,second)
        view = self.harness.state(self.tokens['client'])
        self.assertIn(second,view['payment_instruction']['purpose'])
        self.assertNotIn(first,view['payment_instruction']['purpose'])
        ledger = Ledger(self.path,ADMIN)
        try:
            self.assertEqual(ledger.db.execute('SELECT * FROM search_guard_fixtures').fetchone(),(111,0,9,13,2))
        finally:
            ledger.close()
        raw = self.path.read_bytes()
        for key in ['recipient_name','recipient_code','iban','bank_name']:
            self.assertNotIn(fixture_data()[key].encode(),raw)


if __name__ == '__main__':
    unittest.main()
