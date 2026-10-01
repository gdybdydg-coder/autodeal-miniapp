"""Offline manual membership model; no bot, bank or production connections."""
import re
import secrets
import sqlite3

DAY = 86400
MAX_RECEIPT_AGE = 7 * DAY
CLOCK_SKEW = 300
REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,99}\Z")


def clock(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Positive integer timestamp required")


def reference(value, label="reference"):
    if not isinstance(value, str):
        raise ValueError(f"{label} required")
    if value != value.strip() or not REFERENCE_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def clarification_note(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 500
            or any((ord(c) < 32 and c != '\n') or ord(c) == 127 for c in value)):
        raise ValueError("Clarification text required (max 500 characters)")
    return value.strip()


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
                receipt TEXT, receipt_at INTEGER, receipt_revision INTEGER NOT NULL DEFAULT 0,
                clarification_note TEXT, clarified_at INTEGER,
                payment_ref TEXT UNIQUE, approved_by INTEGER,
                approved_at INTEGER, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS memberships (
                uid INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
                action TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS clarifications (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, actor INTEGER NOT NULL,
                note TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                uid INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL,
                claim_token TEXT, claimed_at INTEGER, delivered_at INTEGER);
            CREATE TABLE IF NOT EXISTS search_guard_fixtures (
                uid INTEGER PRIMARY KEY, enabled INTEGER NOT NULL, epoch INTEGER NOT NULL,
                sent_claims INTEGER NOT NULL, uncertain_claims INTEGER NOT NULL);
        ''')
        # Additive migration for stage-1 and partially-created stage-3 databases.
        self._ensure_columns("orders", {
            "receipt_at": "INTEGER",
            "receipt_revision": "INTEGER NOT NULL DEFAULT 0",
            "clarification_note": "TEXT",
            "clarified_at": "INTEGER",
        })
        self._ensure_columns("outbox", {
            "claim_token": "TEXT",
            "claimed_at": "INTEGER",
            "delivered_at": "INTEGER",
        })

    def _ensure_columns(self, table, migrations):
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        with self.db:
            for name, definition in migrations.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _admin(self, actor):
        if type(actor) is not int or actor != self.admin_id:
            raise PermissionError("Admin only")

    def _enqueue(self, dedupe_key, uid, kind, payload, now):
        self.db.execute(
            "INSERT OR IGNORE INTO outbox (dedupe_key,uid,kind,payload,created) VALUES (?,?,?,?,?)",
            (dedupe_key, uid, kind, payload, now),
        )

    def create_order(self, uid, amount, now, days=30):
        clock(now)
        if any(type(v) is not int or not 0 < v < 2**63 for v in (uid, amount, days)) or days > 365:
            raise ValueError("Invalid order")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT id FROM orders WHERE uid=? AND state IN ('awaiting','review','clarification')",
                (uid,),
            ).fetchone()
            if existing:
                return existing[0]
            oid = "AD-" + secrets.token_hex(8).upper()
            self.db.execute(
                "INSERT INTO orders (id,uid,amount,days,state,created) VALUES (?,?,?,?,'awaiting',?)",
                (oid, uid, amount, days, now),
            )
            self.db.execute(
                "INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'created',?)",
                (oid, uid, now),
            )
        return oid

    def submit_receipt(self, uid, order_id, fixture_reference, now, *, paid_at=None):
        clock(now)
        fixture_reference = reference(fixture_reference, "receipt reference")
        paid_at = now if paid_at is None else paid_at
        clock(paid_at)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT uid,state,created,receipt,receipt_revision FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not row or type(uid) is not int or row[0] != uid:
                raise PermissionError("Order owner only")
            owner, state, created, current_receipt, revision = row
            if state == 'review' and current_receipt == fixture_reference:
                return  # Exact retry is idempotent; no audit or outbox duplicate.
            if state not in ('awaiting', 'clarification'):
                raise ValueError("Order already under review or closed")
            if paid_at < created or paid_at < now - MAX_RECEIPT_AGE or paid_at > now + CLOCK_SKEW:
                raise ValueError("Receipt time outside allowed window")
            revision += 1
            action = 'receipt_resubmitted' if state == 'clarification' else 'receipt_submitted'
            self.db.execute(
                """UPDATE orders SET state='review',receipt=?,receipt_at=?,receipt_revision=?,
                   clarification_note=NULL,clarified_at=NULL WHERE id=?""",
                (fixture_reference, paid_at, revision, order_id),
            )
            self.db.execute(
                "INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,?,?)",
                (order_id, owner, action, now),
            )
            self._enqueue(
                f"{order_id}:review:{revision}", self.admin_id, "review_requested", order_id, now
            )

    def clarify(self, actor, order_id, note, now):
        self._admin(actor)
        clock(now)
        note = clarification_note(note)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT uid,state,receipt_revision FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not row or row[1] != 'review':
                raise ValueError("Receipt review required")
            uid, _, revision = row
            self.db.execute(
                "UPDATE orders SET state='clarification',clarification_note=?,clarified_at=? WHERE id=?",
                (note, now, order_id),
            )
            self.db.execute(
                "INSERT INTO clarifications (order_id,actor,note,at) VALUES (?,?,?,?)",
                (order_id, actor, note, now),
            )
            self.db.execute(
                "INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'clarification_requested',?)",
                (order_id, actor, now),
            )
            self._enqueue(
                f"{order_id}:clarification:{revision}", uid, "clarification_requested", note, now
            )

    def request_clarification(self, actor, order_id, note, now):
        return self.clarify(actor, order_id, note, now)

    def approve(self, actor, order_id, *, payment_ref, actual_amount, bank_verified, now):
        self._admin(actor)
        clock(now)
        payment_ref = reference(payment_ref, "payment reference")
        if bank_verified is not True or type(actual_amount) is not int or actual_amount <= 0:
            raise ValueError("Actual bank verification required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT uid,amount,days,state,payment_ref,expires_at FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
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
            current = self.db.execute(
                "SELECT expires_at FROM memberships WHERE uid=?", (uid,)
            ).fetchone()
            expires = max(now, current[0] if current else 0) + days * DAY
            self.db.execute(
                "INSERT INTO memberships VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET expires_at=excluded.expires_at",
                (uid, expires),
            )
            self.db.execute(
                """UPDATE orders SET state='approved',payment_ref=?,approved_by=?,approved_at=?,expires_at=?
                   WHERE id=?""",
                (payment_ref, actor, now, expires, order_id),
            )
            self.db.execute(
                "INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'approved',?)",
                (order_id, actor, now),
            )
            self._enqueue(f"{order_id}:approved", uid, "membership_approved", str(expires), now)
        return expires

    def reject(self, actor, order_id, now):
        self._admin(actor)
        clock(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT uid,state FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or row[1] not in ('awaiting', 'review', 'clarification'):
                raise ValueError("Order closed or unknown")
            uid = row[0]
            self.db.execute("UPDATE orders SET state='rejected' WHERE id=?", (order_id,))
            self.db.execute(
                "INSERT INTO audit (order_id,actor,action,at) VALUES (?,?,'rejected',?)",
                (order_id, actor, now),
            )
            self._enqueue(f"{order_id}:rejected", uid, "payment_rejected", order_id, now)

    def order_status(self, uid, order_id):
        row = self.db.execute(
            """SELECT uid,state,receipt,receipt_at,receipt_revision,expires_at,
               clarification_note FROM orders WHERE id=?""",
            (order_id,),
        ).fetchone()
        if not row or type(uid) is not int or row[0] != uid:
            raise PermissionError("Order owner only")
        note = self.db.execute(
            "SELECT note FROM clarifications WHERE order_id=? ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        return {
            "state": row[1], "receipt": row[2], "receipt_at": row[3],
            "receipt_revision": row[4], "expires_at": row[5],
            "clarification": note[0] if note and row[1] == 'clarification' else None,
        }

    def active(self, uid, now):
        clock(now)
        row = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
        return bool(row and now < row[0])

    def record_expiry(self, uid, now):
        """Record one offline notice; never changes search state or membership history."""
        clock(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
            if not row or now < row[0]:
                return False
            before = self.db.total_changes
            self._enqueue(
                f"membership:{uid}:expired:{row[0]}", uid, "membership_expired", str(row[0]), now
            )
            return self.db.total_changes > before

    def seed_search_guard_fixture(self, uid, *, enabled, epoch, sent_claims, uncertain_claims):
        """Test setup only: synthetic state used to prove entitlement cannot mutate searches."""
        if (type(uid) is not int or uid <= 0 or type(enabled) is not bool
                or any(type(v) is not int or v < 0 for v in (epoch, sent_claims, uncertain_claims))):
            raise ValueError("Invalid search fixture")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO search_guard_fixtures VALUES (?,?,?,?,?)",
                (uid, int(enabled), epoch, sent_claims, uncertain_claims),
            )

    def stop_search(self, uid, now):
        """Offline /stop simulation; repeated stops are idempotent until explicitly re-seeded."""
        clock(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT enabled,epoch FROM search_guard_fixtures WHERE uid=?", (uid,)
            ).fetchone()
            if not row:
                self.db.execute(
                    "INSERT INTO search_guard_fixtures VALUES (?,?,?,?,?)", (uid, 0, 1, 0, 0)
                )
                return 1
            if not row[0]:
                return row[1]
            epoch = row[1] + 1
            self.db.execute(
                "UPDATE search_guard_fixtures SET enabled=0,epoch=? WHERE uid=?", (epoch, uid)
            )
            return epoch

    def entitlement(self, uid, now):
        """Read-only effective access; membership never enables or rewrites a search fixture."""
        clock(now)
        member = self.db.execute("SELECT expires_at FROM memberships WHERE uid=?", (uid,)).fetchone()
        guard = self.db.execute(
            "SELECT enabled,epoch,sent_claims,uncertain_claims FROM search_guard_fixtures WHERE uid=?",
            (uid,),
        ).fetchone()
        active = bool(member and now < member[0])
        enabled, epoch, sent, uncertain = guard if guard else (0, 0, 0, 0)
        return {
            "membership_active": active,
            "search_enabled": bool(enabled),
            "effective_access": active and bool(enabled),
            "epoch": epoch,
            "sent_claims": sent,
            "uncertain_claims": uncertain,
        }

    def pending_outbox(self):
        return self.db.execute(
            "SELECT id,dedupe_key,uid,kind,payload FROM outbox WHERE state='pending' ORDER BY id"
        ).fetchall()

    def claim_outbox(self, worker_token, now, limit=10):
        """Claim-before-send: a restart never silently retries claimed/uncertain events."""
        worker_token = reference(worker_token, "worker token")
        clock(now)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid outbox limit")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in self.db.execute(
                "SELECT id FROM outbox WHERE state='pending' ORDER BY id LIMIT ?", (limit,)
            )]
            if not ids:
                return []
            marks = ','.join('?' for _ in ids)
            self.db.execute(
                f"UPDATE outbox SET state='claimed',claim_token=?,claimed_at=? WHERE id IN ({marks})",
                (worker_token, now, *ids),
            )
            return self.db.execute(
                f"SELECT id,dedupe_key,uid,kind,payload FROM outbox WHERE id IN ({marks}) ORDER BY id",
                ids,
            ).fetchall()

    def _finish_outbox(self, event_id, worker_token, state, now):
        clock(now)
        worker_token = reference(worker_token, "worker token")
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("Invalid event")
        with self.db:
            cursor = self.db.execute(
                """UPDATE outbox SET state=?,delivered_at=?
                   WHERE id=? AND state='claimed' AND claim_token=?""",
                (state, now, event_id, worker_token),
            )
        return cursor.rowcount == 1

    def mark_outbox_delivered(self, event_id, worker_token, now):
        return self._finish_outbox(event_id, worker_token, 'delivered', now)

    def mark_outbox_uncertain(self, event_id, worker_token, now):
        return self._finish_outbox(event_id, worker_token, 'uncertain', now)

    def close(self):
        self.db.close()
