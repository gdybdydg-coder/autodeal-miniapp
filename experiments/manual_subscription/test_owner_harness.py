import concurrent.futures
import http.client
import json
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from ledger import Ledger
from local_auth import SESSION_TTL
from owner_harness import OwnerHarness, server, ADMIN
from receipt_store import MAX_FILE_BYTES
from test_receipt_store import PNG, PDF


class OwnerHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fixture.sqlite'
        self.now = 1000
        self.harness = OwnerHarness(self.path,lambda:self.now)
        self.app = server(self.harness)
        self.thread = threading.Thread(target=self.app.serve_forever,daemon=True)
        self.thread.start()
        self.tokens = {role:self.login(role)['token'] for role in ('client','admin')}

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, *, token=None, data=None, raw=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.app.server_port,timeout=3)
        request_headers = {'X-AutoDeal-Local':'1'}
        if token:
            request_headers['X-Local-Session'] = token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        elif raw is not None:
            body = raw
        request_headers.update(headers or {})
        connection.request('GET' if body is None else 'POST',path,body,request_headers)
        response = connection.getresponse()
        content, meta = response.read(),dict(response.getheaders())
        connection.close()
        return response.status,content,meta

    def login(self, role):
        invitation = self.harness.sessions.issue_invitation(role)
        code,body,_ = self.request('/api/login',data={'invitation':invitation})
        self.assertEqual(code,200)
        return json.loads(body)

    def action(self, action, oid=None, data=None, role='client'):
        code,body,_ = self.request('/api/action',token=self.tokens[role],
            data={'action':action,'order_id':oid,'data':data or {}})
        return code,json.loads(body)

    def create(self):
        code,view = self.action('create')
        self.assertEqual(code,200)
        return view['order']['id']

    def upload(self, oid, data=PNG, mime='image/png', role='client', headers=None):
        return self.request('/api/receipt',token=self.tokens[role],raw=data,
                            headers={'Content-Type':mime,'X-Order-ID':oid,**(headers or {})})

    def file_count(self):
        ledger = Ledger(self.path,ADMIN)
        try:
            return ledger.db.execute('SELECT count(*) FROM local_receipt_files').fetchone()[0]
        finally:
            ledger.close()

    def test_public_pages_and_assets_disclose_no_session_codes_or_config(self):
        self.assertEqual(self.app.server_address[0],'127.0.0.1')
        for path in ['/client','/admin','/payment-ui.js','/owner-login.js','/owner-transport.js','/payment-ui.css']:
            code,body,headers = self.request(path)
            self.assertEqual(code,200)
            self.assertEqual(headers['Cache-Control'],'no-store')
            for token in self.tokens.values():
                self.assertNotIn(token.encode(),body)
        for path in ['/fixture-config.js','/connected.js','/local_auth.py','/fixture.sqlite',
                     '/api/source-status','/api/subscriptions','/../ledger.py']:
            self.assertEqual(self.request(path)[0],404)
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),0o600)

    def test_login_body_cannot_choose_uid_role_clock_or_price(self):
        invitation = self.harness.sessions.issue_invitation('client')
        for key in ['uid','role','amount','now','expires_at']:
            self.assertEqual(self.request('/api/login',data={'invitation':invitation,key:777})[0],400)
        code,body,_ = self.request('/api/login',data={'invitation':invitation})
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['role'],'client')
        self.assertEqual(self.request('/api/login',data={'invitation':invitation})[0],401)

    def test_file_receipt_then_manual_approval_restart_and_duplicate_confirmation(self):
        oid = self.create()
        code,body,_ = self.upload(oid)
        self.assertEqual(code,200)
        view = json.loads(body)
        self.assertEqual(view['order']['state'],'review')
        self.assertFalse(view['membership']['active'])
        rid = view['receipt_file']['id']
        approved = {'payment_ref':'SYNTH-HTTP-PAID','actual_amount':249,'bank_verified':True}
        code,view = self.action('approve',oid,approved,'admin')
        self.assertEqual(code,200)
        expires = view['membership']['expires_at']
        self.assertFalse(view['search_enabled'])
        self.now += 10
        self.assertEqual(self.action('approve',oid,approved,'admin')[1]['membership']['expires_at'],expires)
        restarted = OwnerHarness(self.path,lambda:self.now)
        self.assertEqual(restarted.state(self.tokens['client'])['membership']['expires_at'],expires)
        self.assertEqual(restarted.download(self.tokens['admin'],rid),('image/png',PNG))
        ledger = Ledger(self.path,ADMIN)
        try:
            self.assertEqual(ledger.db.execute('SELECT * FROM search_guard_fixtures').fetchone(),(111,0,9,13,2))
        finally:
            ledger.close()

    def test_client_cannot_approve_download_or_upload_to_another_order(self):
        oid = self.create()
        rid = json.loads(self.upload(oid)[1])['receipt_file']['id']
        self.assertEqual(self.action('approve',oid,{'payment_ref':'SYNTH-NO','actual_amount':249,'bank_verified':True})[0],403)
        self.assertEqual(self.request('/api/receipts/'+rid,token=self.tokens['client'])[0],403)
        self.assertEqual(self.upload(oid,role='admin')[0],403)
        ledger = Ledger(self.path,ADMIN)
        other = ledger.create_order(222,249,self.now)
        ledger.close()
        self.assertEqual(self.upload(other)[0],403)
        self.assertEqual(self.file_count(),1)
        self.assertFalse(self.harness.state(self.tokens['client'])['membership']['active'])

    def test_admin_download_is_attachment_with_exact_bytes_and_no_filename_input(self):
        oid = self.create()
        view = json.loads(self.upload(oid,PDF,'application/pdf')[1])
        rid = view['receipt_file']['id']
        code,body,headers = self.request('/api/receipts/'+rid,token=self.tokens['admin'])
        self.assertEqual((code,body),(200,PDF))
        self.assertEqual(headers['Content-Type'],'application/octet-stream')
        self.assertEqual(headers['Content-Disposition'],'attachment; filename="receipt.pdf"')
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertIn('sandbox',headers['Content-Security-Policy'])
        self.assertNotIn('filename',view['receipt_file'])

    def test_receipt_limits_formats_and_metadata_bypass_do_not_grant(self):
        oid = self.create()
        self.assertEqual(self.upload(oid,mime='text/html')[0],415)
        self.assertEqual(self.upload(oid,data=b'<script>bad</script>')[0],400)
        self.assertEqual(self.upload(oid,headers={'Content-Length':str(MAX_FILE_BYTES+1)})[0],413)
        self.assertEqual(self.action('receipt',oid,{'receipt_ref':'SYNTH-FAKE','paid_at':self.now})[0],400)
        self.assertEqual(self.file_count(),0)
        self.assertEqual(self.harness.state(self.tokens['client'])['order']['state'],'awaiting')

    def test_truncated_upload_does_not_save_partial_file(self):
        oid = self.create()
        connection = http.client.HTTPConnection('127.0.0.1',self.app.server_port,timeout=3)
        connection.request('POST','/api/receipt',PNG,{'X-Local-Session':self.tokens['client'],
            'X-AutoDeal-Local':'1','Content-Type':'image/png','X-Order-ID':oid,
            'Content-Length':str(len(PNG)+10)})
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        self.assertEqual(response.status,400)
        response.read();connection.close()
        self.assertEqual(self.file_count(),0)

    def test_expired_and_revoked_session_cannot_upload_or_read_after_restart(self):
        oid = self.create()
        self.harness.sessions.revoke(self.tokens['admin'])
        self.assertEqual(self.request('/api/state',token=self.tokens['admin'])[0],401)
        restarted = OwnerHarness(self.path,lambda:self.now)
        with self.assertRaises(PermissionError):
            restarted.state(self.tokens['admin'])
        self.now += SESSION_TTL
        self.assertEqual(self.upload(oid)[0],401)
        self.assertEqual(self.file_count(),0)

    def test_http_rotation_keeps_deadline_and_logout_revokes_new_token(self):
        self.now += 10
        code,body,_ = self.request('/api/rotate',token=self.tokens['client'],data={})
        self.assertEqual(code,200)
        rotated = json.loads(body)
        self.assertEqual(rotated['expires_at'],1000+SESSION_TTL)
        self.assertEqual(self.request('/api/state',token=self.tokens['client'])[0],401)
        self.assertEqual(self.request('/api/state',token=rotated['token'])[0],200)
        self.assertEqual(self.request('/api/logout',token=rotated['token'],data={})[0],200)
        self.assertEqual(self.request('/api/state',token=rotated['token'])[0],401)

    def test_host_origin_fetch_metadata_and_action_headers_are_required(self):
        for headers in [{'Host':'example.invalid'},{'Origin':'https://example.invalid'},
                        {'Sec-Fetch-Site':'same-site'},{'Sec-Fetch-Site':'cross-site'},
                        {'X-AutoDeal-Local':'0'},{'Transfer-Encoding':'chunked'}]:
            code,_,_ = self.request('/api/action',token=self.tokens['client'],
                data={'action':'create','order_id':None,'data':{}},headers=headers)
            self.assertEqual(code,403)
        self.assertIsNone(self.harness.state(self.tokens['client'])['order'])

    def test_concurrent_http_retries_keep_one_file_and_one_receipt_revision(self):
        oid = self.create()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.upload(oid),range(4)))
        self.assertTrue(all(r[0]==200 for r in results))
        self.assertEqual(len({json.loads(r[1])['receipt_file']['id'] for r in results}),1)
        self.assertEqual(self.file_count(),1)
        self.assertEqual(self.harness.state(self.tokens['client'])['order']['receipt_revision'],1)


if __name__ == '__main__':
    unittest.main()
