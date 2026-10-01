"""Offline manual membership model; no bot, bank or production connections."""
import secrets
import sqlite3

DAY = 86400


class Ledger:
    def __init__(self, path, admin_id):
        if type(admin_id) is not int or admin_id <= 0:
            raise ValueError("Explicit admin required")
        self.admin_id = admin_id
        self.db = sqlite3.connect(path, timeout=10)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, uid INTEGER NOT NULL, amount INTEGER NOT NULL,
                days INTEGER NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL,
                receipt TEXT, payment_ref TEXT UNIQUE, approved_by INTEGER,
                approved_at INTEGER, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS memberships (
                uid INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
                action TEXT NOT NULL, at INTEGER NOT NULL);
        ''')

    def _admin(self, actor):
        if type(actor) is not int or actor != self.admin_id:
            raise PermissionError("Admin only")

    def create_order(self, uid, amount, now, days=30):
        if any(type(v) is not int or v <= 0 for v in (uid, amount, now, days)) or days > 365:
            raise ValueError("Invalid order")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT id FROM orders WHERE uid=? AND state IN ('awaiting','review')",
                                       (uid,)).fetchone()
            if existing:
                return existing[0]
            oid = "AD-"+secrets.token_hex(8).upper()
            self.db.execute("INSERT INTO orders (id,uid,amount,days,state,created) VALUES (?,?,?,?,'awaiting',?)",
                            (oid, uid, amount, days, now))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'created',?)", (oid,uid,now))
        return oid

    def submit_receipt(self, uid, order_id, fixture_reference, now):
        if not isinstance(fixture_reference, str) or not 1 <= len(fixture_reference) <= 200:
            raise ValueError("Receipt reference required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT uid,state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or type(uid) is not int or row[0] != uid:
                raise PermissionError("Order owner only")
            if row[1] not in ('awaiting', 'review'):
                raise ValueError("Order closed")
            self.db.execute("UPDATE orders SET state='review',receipt=? WHERE id=?", (fixture_reference,order_id))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'receipt_submitted',?)",
                            (order_id,uid,now))

    def approve(self, actor, order_id, *, payment_ref, actual_amount, bank_verified, now):
        self._admin(actor)
        if (bank_verified is not True or not isinstance(payment_ref, str)
                or not 1 <= len(payment_ref) <= 100
                or type(actual_amount) is not int or actual_amount <= 0
                or type(now) is not int or now <= 0):
            raise ValueError("Actual bank verification required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT uid,amount,days,state,payment_ref,expires_at FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
            if not row:
                raise ValueError("Unknown order")
            uid, amount, days, state, reference, expires = row
            if amount != actual_amount:
                raise ValueError("Payment amount mismatch")
            if state == 'approved':
                if reference != payment_ref:
                    raise ValueError("Already approved against a different payment")
                return expires
            if state != 'review':
                raise ValueError("Receipt review required")
            if self.db.execute("SELECT 1 FROM orders WHERE payment_ref=?", (payment_ref,)).fetchone():
                raise ValueError("Payment already used")
            current = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
            expires = max(now, current[0] if current else 0)+days*DAY
            self.db.execute("INSERT INTO memberships VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET expires_at=excluded.expires_at",
                            (uid, expires))
            self.db.execute("UPDATE orders SET state='approved',payment_ref=?,approved_by=?,approved_at=?,expires_at=? WHERE id=?",
                            (payment_ref,actor,now,expires,order_id))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'approved',?)", (order_id,actor,now))
        return expires

    def reject(self, actor, order_id, now):
        self._admin(actor)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row[0] not in ('awaiting', 'review'):
                raise ValueError("Order closed or unknown")
            self.db.execute("UPDATE orders SET state='rejected' WHERE id=?", (order_id,))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'rejected',?)", (order_id,actor,now))

    def active(self, uid, now):
        row = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
        return bool(row and now < row[0])

    def close(self):
        self.db.close()
