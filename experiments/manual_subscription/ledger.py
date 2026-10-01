"""Offline manual membership model; no bot, bank or production connections."""
import secrets
import sqlite3

DAY = 86400


def clock(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Positive integer timestamp required")


def reference(value, limit):
    if (not isinstance(value, str) or value != value.strip()
            or not 1 <= len(value) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Invalid reference")
    return value


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
            CREATE TABLE IF NOT EXISTS clarifications (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
                note TEXT NOT NULL, at INTEGER NOT NULL);
        ''')

    def _admin(self, actor):
        if type(actor) is not int or actor != self.admin_id:
            raise PermissionError("Admin only")

    def create_order(self, uid, amount, now, days=30):
        clock(now)
        if any(type(v) is not int or not 0 < v < 2**63 for v in (uid, amount, days)) or days > 365:
            raise ValueError("Invalid order")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT id FROM orders WHERE uid=? AND state IN ('awaiting','review','clarification')",
                                       (uid,)).fetchone()
            if existing:
                return existing[0]
            oid = "AD-"+secrets.token_hex(8).upper()
            self.db.execute("INSERT INTO orders (id,uid,amount,days,state,created) VALUES (?,?,?,?,'awaiting',?)",
                            (oid, uid, amount, days, now))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'created',?)", (oid,uid,now))
        return oid

    def submit_receipt(self, uid, order_id, fixture_reference, now):
        clock(now)
        reference(fixture_reference, 200)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT uid,state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or type(uid) is not int or row[0] != uid:
                raise PermissionError("Order owner only")
            if row[1] not in ('awaiting', 'review', 'clarification'):
                raise ValueError("Order closed")
            current = self.db.execute("SELECT receipt FROM orders WHERE id=?", (order_id,)).fetchone()[0]
            if row[1] == 'review' and current == fixture_reference:
                return  # A duplicate submission does not grow the audit log.
            self.db.execute("UPDATE orders SET state='review',receipt=? WHERE id=?", (fixture_reference,order_id))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'receipt_submitted',?)",
                            (order_id,uid,now))

    def approve(self, actor, order_id, *, payment_ref, actual_amount, bank_verified, now):
        self._admin(actor)
        clock(now)
        reference(payment_ref, 100)
        if (bank_verified is not True
                or type(actual_amount) is not int or actual_amount <= 0
                or type(now) is not int or now <= 0):
            raise ValueError("Actual bank verification required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT uid,amount,days,state,payment_ref,expires_at FROM orders WHERE id=?",
                                  (order_id,)).fetchone()
            if not row:
                raise ValueError("Unknown order")
            uid, amount, days, state, existing_reference, expires = row
            if amount != actual_amount:
                raise ValueError("Payment amount mismatch")
            if state == 'approved':
                if existing_reference != payment_ref:
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
        clock(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row[0] not in ('awaiting', 'review', 'clarification'):
                raise ValueError("Order closed or unknown")
            self.db.execute("UPDATE orders SET state='rejected' WHERE id=?", (order_id,))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'rejected',?)", (order_id,actor,now))

    def clarify(self, actor, order_id, note, now):
        self._admin(actor)
        clock(now)
        if (not isinstance(note, str) or not 1 <= len(note.strip()) <= 500
                or any((ord(c) < 32 and c != '\n') or ord(c) == 127 for c in note)):
            raise ValueError("Clarification text required (max 500 characters)")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row[0] != 'review':
                raise ValueError("Receipt review required")
            self.db.execute("UPDATE orders SET state='clarification' WHERE id=?", (order_id,))
            self.db.execute("INSERT INTO clarifications (order_id,actor,note,at) VALUES (?,?,?,?)",
                            (order_id,actor,note.strip(),now))
            self.db.execute("INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'clarification_requested',?)",
                            (order_id,actor,now))

    def order_status(self, uid, order_id):
        row = self.db.execute("SELECT uid,state,receipt,expires_at FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or type(uid) is not int or row[0] != uid:
            raise PermissionError("Order owner only")
        note = self.db.execute("SELECT note FROM clarifications WHERE order_id=? ORDER BY id DESC LIMIT 1",
                               (order_id,)).fetchone()
        return {"state": row[1], "receipt": row[2], "expires_at": row[3],
                "clarification": note[0] if note and row[1] == 'clarification' else None}

    def active(self, uid, now):
        clock(now)
        row = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
        return bool(row and now < row[0])

    def close(self):
        self.db.close()
