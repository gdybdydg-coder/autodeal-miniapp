import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from harness import Harness, server, ADMIN
from ledger import Ledger


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fixture.sqlite'
        self.now = 1000
        self.harness = Harness(self.path, lambda: self.now)
        self.app = server(self.harness)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, role='client', body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.app.server_port, timeout=3)
        token = self.harness.tokens[role] if role else 'SYNTH-INVALID'
        request_headers = {'X-Fixture-Session': token}
        if body is not None:
            request_headers.update({'Content-Type':'application/json', 'X-AutoDeal-Fixture':'1'})
        request_headers.update(headers or {})
        client.request('GET' if body is None else 'POST', path,
                       None if body is None else json.dumps(body), request_headers)
        response = client.getresponse()
        data, response_headers = response.read(), dict(response.getheaders())
        client.close()
        return response.status, data, response_headers

    def action(self, action, oid=None, data=None, role='client'):
        code, body, _ = self.request('/api/action', role,
            {'action':action, 'order_id':oid, 'data':data or {}})
        return code, json.loads(body)

    def review(self):
        code, state = self.action('create')
        self.assertEqual(code,200)
        oid = state['order']['id']
        self.now += 1
        code, state = self.action('receipt',oid,{'receipt_ref':'SYNTH-RECEIPT-001','paid_at':self.now})
        self.assertEqual(code,200)
        return oid, state

    def approval(self):
        return {'payment_ref':'SYNTH-PAY-001','actual_amount':249,'bank_verified':True}

    def test_connected_http_flow_persists_through_harness_restart(self):
        oid, state = self.review()
        self.assertEqual(state['order']['state'],'review')
        self.assertFalse(state['membership']['active'])
        code, state = self.action('approve',oid,self.approval(),role='admin')
        self.assertEqual(code,200)
        expiry = state['membership']['expires_at']
        restarted = Harness(self.path,lambda:self.now)
        view = restarted.state(restarted.tokens['client'])
        self.assertEqual(view['order']['state'],'approved')
        self.assertEqual(view['membership']['expires_at'],expiry)
        self.assertTrue(view['membership']['active'])
        self.assertFalse(view['search_enabled'])
        with self.assertRaises(PermissionError):
            restarted.state(self.harness.tokens['client'])

    def test_missing_verification_wrong_amount_and_client_admin_action_do_not_grant_access(self):
        oid, _ = self.review()
        for data in [{**self.approval(),'bank_verified':False},{**self.approval(),'actual_amount':1}]:
            code, _ = self.action('approve',oid,data,role='admin')
            self.assertEqual(code,400)
        self.assertEqual(self.action('approve',oid,self.approval())[0],403)
        self.assertFalse(self.harness.state(self.harness.tokens['client'])['membership']['active'])

    def test_clarification_and_resubmit_are_visible_to_both_roles(self):
        oid, _ = self.review()
        self.assertEqual(self.action('clarify',oid,{'note':'Уточни час'},role='admin')[0],200)
        state = self.harness.state(self.harness.tokens['client'])
        self.assertEqual(state['order']['clarification'],'Уточни час')
        code, state = self.action('receipt',oid,{'receipt_ref':'SYNTH-CORRECTED','paid_at':self.now})
        self.assertEqual(code,200)
        self.assertIsNone(state['order']['clarification'])
        self.assertEqual(self.harness.state(self.harness.tokens['admin'])['order']['receipt_revision'],2)

    def test_duplicate_approval_http_does_not_extend_membership(self):
        oid, _ = self.review()
        first = self.action('approve',oid,self.approval(),role='admin')[1]['membership']['expires_at']
        self.now += 10
        second = self.action('approve',oid,self.approval(),role='admin')[1]['membership']['expires_at']
        self.assertEqual(first,second)
        ledger = Ledger(self.path,ADMIN)
        try:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM audit WHERE action='approved'").fetchone()[0],1)
        finally:
            ledger.close()

    def test_payload_cannot_choose_uid_tariff_or_server_clock(self):
        for key in ['uid','amount','now','expires_at','actor']:
            self.assertEqual(self.action('create',data={key:777})[0],400)
        self.assertIsNone(self.harness.state(self.harness.tokens['client'])['order'])

    def test_host_origin_and_action_header_guards_are_enforced(self):
        self.assertEqual(self.app.server_address[0],'127.0.0.1')
        for headers in [{'Host':'example.invalid'},{'Origin':'https://example.invalid'},
                        {'X-AutoDeal-Fixture':'0'},{'Content-Type':'text/plain'}]:
            code,_,_ = self.request('/api/action',body={'action':'create','order_id':None,'data':{}},headers=headers)
            self.assertEqual(code,403)
        self.assertEqual(self.request('/api/state',role=None)[0],403)
        self.assertEqual(self.request('/api/state',headers={'Host':'example.invalid'})[0],403)
        self.assertEqual(self.request('/fixture-config.js',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/fixture-config.js',headers={'Sec-Fetch-Site':'same-site'})[0],403)

    def test_page_assets_are_local_and_production_paths_are_unavailable(self):
        for path in ['/','/payment-ui.js','/connected.js','/payment-ui.css','/fixture-config.js']:
            code,data,headers = self.request(path)
            self.assertEqual(code,200)
            self.assertTrue(data)
            self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])
        for path in ['/backend/app.py','/api/source-status','/api/subscriptions','/../ledger.py']:
            self.assertEqual(self.request(path)[0],404)


if __name__ == '__main__':
    unittest.main()
