"""Private loopback review UI with existing durable invitation auth. No sends."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from owner_harness import OwnerHarness, server
from harness import ROOT, CLIENT, ADMIN
from receipt_store import ReceiptStore
from review_queue import ReviewLedger, ReviewAPI


class QueueHarness(OwnerHarness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ledger = ReviewLedger(self.path,ADMIN)
        ledger.person(CLIENT,'Тестовий клієнт',None)
        ledger.close()

    def state(self, token):
        principal = self.sessions.resolve(token)
        role = self.sessions.role(token)
        db = ReviewLedger(self.path,ADMIN)
        try:
            if role == 'admin':
                return {'role':role,'queue':db.queue(principal.uid,self.clock()),'payments_enabled':False}
            row = db.db.execute('SELECT id FROM orders WHERE uid=? ORDER BY created DESC,id DESC LIMIT 1',(principal.uid,)).fetchone()
            order = db.order_status(principal.uid,row[0]) if row else None
            instruction = self.recipient.instruction(order['code'],order['amount'],order['days']) if order and self.recipient else None
            if instruction:
                # Legacy fixture-generated purpose is not a verified bank instruction.
                instruction.pop('purpose',None)
            return {'role':role,'order':order,'payment_instruction':instruction,'payments_enabled':False}
        finally:db.close()

    def action(self, token, body):
        if type(body) is not dict or set(body) != {'command','data'}:
            raise ValueError('Only documented fields are accepted')
        db = ReviewLedger(self.path,ADMIN)
        try:return {'result':ReviewAPI(db,self.sessions).command(token,body['command'],body['data'],self.clock())}
        finally:db.close()

    def upload(self, token, order_id, mime, data):
        principal = self.sessions.resolve(token)
        if principal.uid != CLIENT:
            raise PermissionError('Client session required')
        db = ReviewLedger(self.path,ADMIN)
        try:ReceiptStore(db).submit(principal.uid,order_id,mime,data,self.clock())
        finally:db.close()
        return self.state(token)


def page():
    return (ROOT/'review.html').read_text()


def main():
    parser=argparse.ArgumentParser(description='LOCAL synthetic manual queue review; NEVER publish')
    parser.add_argument('--db',type=Path)
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--recipient-profile',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='review-queue-') as tmp:
        path=args.db or Path(tmp)/'fixture.sqlite'
        harness=QueueHarness(path,recipient_profile=args.recipient_profile)
        keys=path.with_name(path.name+'.local-invitations.json')
        data={role:harness.sessions.issue_invitation(role) for role in ('client','admin')}
        fd=os.open(keys,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),0o600);json.dump(data,f)
        print('Local invitation file:',keys.resolve(),flush=True)
        app=server(harness,args.port,page_factory=page)
        print(f'Local only: http://127.0.0.1:{app.server_port}/client and /admin',flush=True)
        try:app.serve_forever()
        except KeyboardInterrupt:pass
        finally:app.server_close()


if __name__=='__main__':main()
