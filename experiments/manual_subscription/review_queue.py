"""Stage 10: durable, owner-reviewed queue. Offline SQLite ONLY; no network.

Reuses stage-9 Ledger/outbox. This is NOT mounted into the production bot.
ReviewAPI takes server-issued sessions, never a client-supplied actor/uid.
"""
import hashlib
import json
import secrets
import unicodedata

from ledger import Ledger, DAY, clock, reference, clarification_note
from adapter import Adapter

STATES = ('awaiting', 'review', 'clarification', 'approved', 'rejected')


def bank_key(account, transaction):
    # Operator enters the actual credited bank transaction, NOT receipt filename.
    def canonical(value):
        if not isinstance(value, str):
            raise ValueError('Bank account and transaction references required')
        value = unicodedata.normalize('NFKC', value).strip().upper()
        if not 3 <= len(value) <= 120 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid bank reference')
        return value
    raw = json.dumps([canonical(account), canonical(transaction)], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class ReviewLedger(Ledger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ensure_columns('orders', {'reported_amount': 'INTEGER', 'transfer_note': 'TEXT',
                                      'rejection_reason': 'TEXT'})
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS review_people (
            uid INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT);
          CREATE TABLE IF NOT EXISTS review_evidence (
            id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE,
            receipt TEXT, amount INTEGER, transfer_at INTEGER, note TEXT, at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS review_confirmations (
            token TEXT PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
            revision INTEGER NOT NULL, before_expiry INTEGER NOT NULL,
            bank_key TEXT NOT NULL, amount INTEGER NOT NULL, deadline INTEGER NOT NULL,
            proposed_expiry INTEGER NOT NULL, result INTEGER);
          CREATE TABLE IF NOT EXISTS review_grants (
            order_id TEXT PRIMARY KEY, bank_key TEXT UNIQUE NOT NULL, actor INTEGER NOT NULL,
            at INTEGER NOT NULL, before_expiry INTEGER NOT NULL, after_expiry INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS review_summaries (
            bucket INTEGER PRIMARY KEY, created INTEGER NOT NULL, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS review_queue_order ON orders(state,created,id);
        ''')

    accepts_late_receipt = True

    def new_code(self):
        # 48 random bits; collision retries happen before any insert.
        for _ in range(10):
            code = 'AD-' + secrets.token_hex(6).upper()
            if not self.db.execute('SELECT 1 FROM orders WHERE id=?',(code,)).fetchone():
                return code
        raise RuntimeError('Could not allocate request code')

    def person(self, uid, name, username=None):
        """Trusted authentication adapter supplies display fields; never bank proof."""
        if type(uid) is not int or uid <= 0 or not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError('Invalid display profile')
        if username is not None and (not isinstance(username, str) or len(username) > 64):
            raise ValueError('Invalid username')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO review_people VALUES (?,?,?)', (uid, name, username))

    def paid(self, uid, oid, now, *, receipt=None, amount=None, transfer_at=None, note=None):
        clock(now)
        if receipt is not None:
            reference(receipt, 'receipt reference')
        if amount is not None and (type(amount) is not int or not 0 < amount < 10**9):
            raise ValueError('Invalid reported amount')
        if transfer_at is not None:
            clock(transfer_at)
            if transfer_at > now + 300:
                raise ValueError('Transfer is in the future')
        if note is not None:
            note = clarification_note(note)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._paid_locked(uid, oid, now, receipt=receipt, amount=amount, transfer_at=transfer_at, note=note)
        return self.order_status(uid, oid)

    def _submit_receipt_locked(self, uid, order_id, fixture_reference, now, *, paid_at=None):
        clock(now)
        reference(fixture_reference, 'receipt reference')
        self._paid_locked(uid, order_id, now, receipt=fixture_reference, transfer_at=paid_at)

    def _paid_locked(self, uid, oid, now, *, receipt=None, amount=None, transfer_at=None, note=None):
        if not self.db.in_transaction:
            raise RuntimeError('Evidence transaction required')
        row = self.db.execute('SELECT uid,state,receipt,reported_amount,receipt_at,transfer_note,receipt_revision FROM orders WHERE id=?', (oid,)).fetchone()
        if not row or type(uid) is not int or row[0] != uid:
            raise PermissionError('Order owner only')
        if row[1] in ('approved', 'rejected'):
            return self.order_status(uid, oid)
        # Merge late evidence; an empty repeat never erases prior evidence.
        values = [new if new is not None else old for new, old in zip(
            (receipt, amount, transfer_at, note), row[2:6])]
        if row[1] == 'review' and tuple(values) == row[2:6]:
            return self.order_status(uid, oid)
        rev = row[6] + 1
        key = oid + ':' + str(rev)
        self.db.execute('INSERT INTO review_evidence(order_id,event_key,receipt,amount,transfer_at,note,at) VALUES (?,?,?,?,?,?,?)', (oid,key,*values,now))
        self.db.execute("UPDATE orders SET state='review',receipt=?,reported_amount=?,receipt_at=?,transfer_note=?,receipt_revision=?,clarification_note=NULL,clarified_at=NULL WHERE id=?", (*values,rev,oid))
        self.db.execute("INSERT INTO audit(order_id,actor,action,at) VALUES (?,?,'paid_reported',?)", (oid,uid,now))
        self._queue_order_notice(oid, now)

    def order_status(self, uid, order_id):
        result = super().order_status(uid, order_id)
        row = self.db.execute('SELECT rejection_reason,amount,days FROM orders WHERE id=?',(order_id,)).fetchone()
        result.update(code=order_id, rejection_reason=row[0], amount=row[1], days=row[2])
        return result

    def current_expiry(self, uid):
        row = self.db.execute('SELECT expires_at FROM memberships WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else 0

    def queue(self, actor, now, *, state='review', search='', page=1, size=20):
        self._admin(actor)
        clock(now)
        if state not in (*STATES, 'all') or type(page) is not int or page < 1 or type(size) is not int or not 1 <= size <= 100:
            raise ValueError('Invalid queue page')
        if not isinstance(search, str) or len(search) > 100:
            raise ValueError('Invalid search')
        counts = dict(self.db.execute('SELECT state,count(*) FROM orders GROUP BY state'))
        oldest = self.db.execute("SELECT min(created) FROM orders WHERE state='review'").fetchone()[0]
        clauses, args = [], []
        if state != 'all':
            clauses.append('o.state=?'); args.append(state)
        if search:
            # instr treats % and _ as literal text, avoiding wildcard surprises.
            clauses.append('(instr(lower(o.id),lower(?))>0 OR cast(o.uid as text)=? OR instr(lower(coalesce(p.name,\'\')),lower(?))>0 OR instr(lower(coalesce(p.username,\'\')),lower(?))>0)')
            args.extend([search] * 4)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        join = ' FROM orders o LEFT JOIN review_people p ON p.uid=o.uid'
        total = self.db.execute('SELECT count(*)'+join+where, args).fetchone()[0]
        rows = self.db.execute('SELECT o.id,o.uid,o.amount,o.days,o.state,o.created,p.name,p.username'+join+where+' ORDER BY o.created,o.id LIMIT ? OFFSET ?', (*args,size,(page-1)*size)).fetchall()
        keys = ('code','user_id','amount','days','state','created','name','username')
        return {'items':[dict(zip(keys,row)) for row in rows], 'counts':{s:counts.get(s,0) for s in STATES},
                'waiting':counts.get('review',0), 'oldest_age':max(0,now-oldest) if oldest else 0,
                'total':total,'page':page,'pages':max(1,(total+size-1)//size),'size':size}

    def card(self, actor, oid):
        self._admin(actor)
        cursor = self.db.execute('SELECT * FROM orders WHERE id=?',(oid,))
        row = cursor.fetchone()
        if not row:
            raise ValueError('Unknown request')
        card = dict(zip([col[0] for col in cursor.description], row))
        person = self.db.execute('SELECT name,username FROM review_people WHERE uid=?',(card['uid'],)).fetchone()
        card.update(name=person[0] if person else 'Клієнт', username=person[1] if person else None,
                    current_expiry=self.current_expiry(card['uid']),
                    history=self.db.execute('SELECT actor,action,at FROM audit WHERE order_id=? ORDER BY id',(oid,)).fetchall(),
                    evidence=self.db.execute('SELECT receipt,amount,transfer_at,note,at FROM review_evidence WHERE order_id=? ORDER BY id',(oid,)).fetchall())
        return card

    def preview(self, actor, oid, now, *, account, transaction, actual_amount, bank_verified):
        self._admin(actor)
        clock(now)
        if bank_verified is not True or type(actual_amount) is not int:
            raise ValueError('Owner bank verification required')
        key = bank_key(account, transaction)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self.card(actor, oid)
            if card['state'] != 'review' or actual_amount != card['amount']:
                raise ValueError('Review and exact credited amount required')
            if self.db.execute('SELECT 1 FROM review_grants WHERE bank_key=?',(key,)).fetchone():
                raise ValueError('Bank credit already used')
            token = secrets.token_urlsafe(24)
            until = max(now,card['current_expiry']) + card['days'] * DAY
            self.db.execute('INSERT INTO review_confirmations VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                (token,oid,actor,card['receipt_revision'],card['current_expiry'],key,actual_amount,now+300,until))
        return {'confirmation':token,'code':oid,'name':card['name'],'user_id':card['uid'],
                'days':card['days'],'before':card['current_expiry'],'expires_at':until,'valid_until':now+300}

    def approve(self, *args, **kwargs):
        raise ValueError('Use preview and confirm; direct approval is disabled in stage 10')

    def confirm(self, actor, token, now):
        self._admin(actor)
        clock(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT order_id,actor,revision,before_expiry,bank_key,amount,deadline,proposed_expiry,result FROM review_confirmations WHERE token=?',(token,)).fetchone()
            if not row or row[1] != actor:
                raise PermissionError('Invalid confirmation')
            oid,_,revision,before,key,amount,deadline,proposed,result = row
            if result is not None:
                return result
            card = self.card(actor,oid)
            if now > deadline or card['state'] != 'review' or card['receipt_revision'] != revision or card['current_expiry'] != before:
                raise ValueError('Request or access changed; preview again')
            # Activation starts at the actual confirmation, not the preview. The
            # displayed date is a minimum; drift is bounded by preview's 5min TTL.
            until = max(now,before) + card['days'] * DAY
            if self.db.execute('SELECT 1 FROM review_grants WHERE bank_key=?',(key,)).fetchone():
                raise ValueError('Bank credit already used')
            self.db.execute('INSERT INTO review_grants VALUES (?,?,?,?,?,?)',(oid,key,actor,now,before,until))
            self.db.execute('INSERT INTO memberships(uid,expires_at) VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET expires_at=excluded.expires_at,expiry_notice_at=NULL,notice_pending=0',(card['uid'],until))
            self.db.execute("UPDATE orders SET state='approved',payment_ref=?,approved_by=?,approved_at=?,expires_at=? WHERE id=?",(key,actor,now,until,oid))
            self.db.execute("INSERT INTO audit(order_id,actor,action,at) VALUES (?,?,'approved',?)",(oid,actor,now))
            self.db.execute('UPDATE review_confirmations SET result=? WHERE token=?',(until,token))
            self._queue_order_notice(oid,now)
        return until

    def reject_with_reason(self, actor, oid, reason, now):
        self._admin(actor)
        clock(now)
        reason = clarification_note(reason)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            card = self.card(actor,oid)
            if card['state'] not in ('awaiting','review','clarification'):
                raise ValueError('Closed request')
            self.db.execute("UPDATE orders SET state='rejected',rejection_reason=? WHERE id=?",(reason,oid))
            self.db.execute("INSERT INTO audit(order_id,actor,action,at) VALUES (?,?,'rejected',?)",(oid,actor,now))
            self._queue_order_notice(oid,now)

    def retry_known_failure(self, actor, event_id, now):
        """Only explicitly known non-delivery; uncertain acceptance is never guessed."""
        self._admin(actor)
        clock(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("UPDATE outbox SET state='pending',claim_token=NULL,claimed_at=NULL,prepared_at=NULL WHERE id=? AND state='failed'",(event_id,))

    def mark_failed(self, event_id, worker_token, now):
        return self._finish_outbox(event_id, worker_token, 'failed', now)

    def summary(self, actor, now, *, enabled=False, interval=3600):
        self._admin(actor)
        clock(now)
        if not enabled:
            return None
        if type(interval) is not int or interval < 3600:
            raise ValueError('Summary interval must be at least one hour')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            snapshot = self.queue(actor,now)
            if not snapshot['waiting']:
                return None
            payload = {'waiting':snapshot['waiting'],'oldest_age':snapshot['oldest_age'],'command':'/payments'}
            inserted = self.db.execute('INSERT OR IGNORE INTO review_summaries VALUES (?,?,?)',(now//interval,now,json.dumps(payload))).rowcount
        return payload if inserted else None


class ReviewAPI(Adapter):
    """Server-side command adapter; sessions are resolved on EVERY call."""
    def command(self, token, command, data, now):
        principal = self.sessions.resolve(token)
        if command == 'create':
            return self.create(token,data,now)
        if command == 'paid':
            if type(data) is not dict or not {'code'} <= set(data) or set(data)-{'code','receipt','amount','transfer_at','note'}:
                raise ValueError('Invalid evidence fields')
            return self.ledger.paid(principal.uid,data['code'],now,**{k:v for k,v in data.items() if k!='code'})
        if command == 'status':
            self._body(data,('code',))
            return self.status(token,data['code'])
        self._admin(token)
        if command == '/payments':
            if set(data)-{'state','search','page','size'}:
                raise ValueError('Invalid queue fields')
            return self.ledger.queue(principal.uid,now,**data)
        if command == 'card':
            self._body(data,('code',))
            return self.ledger.card(principal.uid,data['code'])
        if command == 'preview':
            self._body(data,('code','account','transaction','actual_amount','bank_verified'))
            return self.ledger.preview(principal.uid,data['code'],now,**{k:v for k,v in data.items() if k!='code'})
        if command == 'confirm':
            self._body(data,('confirmation',))
            return self.ledger.confirm(principal.uid,data['confirmation'],now)
        if command == 'clarify':
            self._body(data,('code','note'))
            return self.ledger.clarify(principal.uid,data['code'],data['note'],now)
        if command == 'reject':
            self._body(data,('code','reason'))
            return self.ledger.reject_with_reason(principal.uid,data['code'],data['reason'],now)
        raise ValueError('Unknown command')
