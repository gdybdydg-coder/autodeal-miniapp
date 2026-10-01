import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from queue_harness import QueueHarness, page
from owner_harness import server


class QueueHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=1000
        self.harness=QueueHarness(Path(self.tmp.name)/'queue.sqlite',lambda:self.now)
        self.app=server(self.harness,page_factory=page)
        self.thread=threading.Thread(target=self.app.serve_forever,daemon=True);self.thread.start()
        self.tokens={role:self.request('/api/login',{'invitation':self.harness.sessions.issue_invitation(role)})[1]['token'] for role in ('client','admin')}

    def tearDown(self):
        self.app.shutdown();self.app.server_close();self.thread.join();self.tmp.cleanup()

    def request(self,path,data=None,role=None,raw=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.app.server_port,timeout=3)
        headers={'X-AutoDeal-Local':'1'}
        if role:headers['X-Local-Session']=self.tokens[role]
        if data is not None:headers['Content-Type']='application/json'
        payload=json.dumps(data).encode() if data is not None else None
        if raw:
            payload=raw[1];headers.update({'Content-Type':'application/pdf','X-Order-ID':raw[0]})
        conn.request('GET' if payload is None else 'POST',path,payload,headers)
        r=conn.getresponse();body=r.read();status=r.status;conn.close()
        return status,json.loads(body)

    def action(self,command,data,role='client'):
        return self.request('/api/action',{'command':command,'data':data},role)

    def test_http_paid_late_upload_review_confirm_and_private_views(self):
        status,data=self.action('create',{});self.assertEqual(status,200);code=data['result']
        self.assertEqual(self.action('paid',{'code':code})[0],200)
        self.assertEqual(self.request('/api/receipt',role='client',raw=(code,b'%PDF-SYNTHETIC'))[0],200)
        self.assertEqual(self.action('/payments',{},'client')[0],403)
        queue=self.action('/payments',{'state':'all'},'admin')[1]['result']
        self.assertEqual(queue['total'],1)
        card=self.action('card',{'code':code},'admin')[1]['result']
        self.assertTrue(card['receipt'].startswith('RCPT-'));self.assertIsNone(card['username'])
        args={'code':code,'account':'SYNTH-ACCOUNT','transaction':'SYNTH-TXN','actual_amount':250,'bank_verified':True}
        preview=self.action('preview',args,'admin')[1]['result']
        self.assertEqual(self.action('confirm',{'confirmation':preview['confirmation']},'client')[0],403)
        result=self.action('confirm',{'confirmation':preview['confirmation']},'admin')[1]['result']
        self.assertEqual(result,1000+30*86400)
        status=self.request('/api/state',role='client')[1]
        self.assertNotIn('uid',status['order']);self.assertEqual(status['order']['state'],'approved')
        self.assertFalse(status['payments_enabled'])

    def test_revoked_old_button_and_forged_identity_are_rejected(self):
        code=self.action('create',{})[1]['result']
        self.assertEqual(self.action('paid',{'code':code,'actor':777})[0],400)
        self.harness.sessions.revoke(self.tokens['admin'])
        self.assertEqual(self.action('/payments',{},'admin')[0],401)
        self.assertEqual(self.request('/api/state')[0],401)

    def test_verified_fixture_profile_accepts_short_code_without_invented_purpose(self):
        from bank_profile import RecipientProfile
        from test_bank_profile import fixture_data, TEST_IBAN
        self.harness.recipient=RecipientProfile.parse(fixture_data())
        self.assertIsNone(self.request('/api/state',role='client')[1]['payment_instruction'])
        code=self.action('create',{})[1]['result']
        self.assertEqual(len(code),15)
        instruction=self.request('/api/state',role='client')[1]['payment_instruction']
        self.assertEqual(instruction['iban'],TEST_IBAN)
        self.assertNotIn('purpose',instruction)
        self.assertFalse(instruction['payments_enabled'])


if __name__=='__main__':unittest.main()
