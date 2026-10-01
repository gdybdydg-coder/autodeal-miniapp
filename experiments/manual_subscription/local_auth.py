"""Durable one-use invitation/session model for LOCAL owner review only."""
from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3

from adapter import Principal
from ledger import clock

TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
SESSION_TTL = 3600
INVITATION_TTL = 900
AUTH_CAPACITY = 32


def token_hash(token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise PermissionError('Local session required')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


class LocalSessions:
    """Server creates invitations; web callers cannot choose their uid or role.

    No identity provider or Telegram authentication. Hashes, revocation and
    absolute expiry survive a LOCAL SQLite restart. No browser persistence.
    """
    def __init__(self, path, now, participants):
        self.path, self.now, self.participants = str(path), now, dict(participants)
        if set(participants) != {'client', 'admin'} or any(
                type(v) is not int or not 0 < v < 2**63 for v in participants.values()):
            raise ValueError('Explicit fixture participants required')
        if participants['client'] == participants['admin']:
            raise ValueError('Separate fixture principals required')
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS local_invitations (
                    digest TEXT PRIMARY KEY, uid INTEGER NOT NULL, role TEXT NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL, used INTEGER);
                CREATE TABLE IF NOT EXISTS local_sessions (
                    digest TEXT PRIMARY KEY, uid INTEGER NOT NULL, role TEXT NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL, revoked INTEGER);
                CREATE TABLE IF NOT EXISTS local_auth_clock (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), at INTEGER NOT NULL);
            ''')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            # Persist the clock even when the following authorization fails.
            # Otherwise a failed expiry check followed by clock rollback could
            # revive a session whose deadline was already observed.
            with db:
                db.execute('BEGIN IMMEDIATE')
                now = self.now()  # Sample AFTER acquiring the write lock.
                clock(now)
                previous = db.execute('SELECT at FROM local_auth_clock WHERE singleton=1').fetchone()
                if previous and now < previous[0]:
                    raise PermissionError('Local clock moved backwards')
                db.execute('INSERT INTO local_auth_clock VALUES (1,?) ON CONFLICT(singleton) '
                           'DO UPDATE SET at=excluded.at', (now,))
                db.execute('SAVEPOINT local_authority_operation')
                try:
                    yield db, now
                except BaseException:
                    db.execute('ROLLBACK TO local_authority_operation')
                    db.execute('RELEASE local_authority_operation')
                    db.commit()  # Clock stays durable; failed action is undone.
                    raise
                else:
                    db.execute('RELEASE local_authority_operation')
        finally:
            db.close()

    @staticmethod
    def _prune(db, now):
        # Removing terminal hashes does not restore authority; unknown tokens fail.
        db.execute('DELETE FROM local_invitations WHERE expires<=? OR used IS NOT NULL', (now,))
        db.execute('DELETE FROM local_sessions WHERE expires<=? OR revoked IS NOT NULL', (now,))

    def issue_invitation(self, role):
        if role not in self.participants:
            raise ValueError('Unknown fixture role')
        token = secrets.token_urlsafe(32)
        with self._transaction() as (db, now):
            self._prune(db, now)
            if db.execute('SELECT count(*) FROM local_invitations').fetchone()[0] >= AUTH_CAPACITY:
                raise ValueError('Local invitation capacity reached')
            db.execute('INSERT INTO local_invitations VALUES (?,?,?,?,?,NULL)',
                       (token_hash(token), self.participants[role], role, now, now+INVITATION_TTL))
        return token

    def login(self, invitation):
        digest = token_hash(invitation)
        token = secrets.token_urlsafe(32)
        with self._transaction() as (db, now):
            self._prune(db, now)
            row = db.execute('SELECT uid,role,created,expires,used FROM local_invitations '
                             'WHERE digest=?', (digest,)).fetchone()
            if not row or row[4] is not None or not row[2] <= now < row[3]:
                raise PermissionError('Invitation invalid or expired')
            if self.participants.get(row[1]) != row[0]:
                raise PermissionError('Fixture principal changed')
            if db.execute('SELECT count(*) FROM local_sessions').fetchone()[0] >= AUTH_CAPACITY:
                raise ValueError('Local session capacity reached')
            db.execute('UPDATE local_invitations SET used=? WHERE digest=?', (now, digest))
            db.execute('INSERT INTO local_sessions VALUES (?,?,?,?,?,NULL)',
                       (token_hash(token), row[0], row[1], now, now+SESSION_TTL))
            return {'token': token, 'role': row[1], 'expires_at': now+SESSION_TTL}

    def _session(self, db, digest, now):
        row = db.execute('SELECT uid,role,created,expires,revoked FROM local_sessions '
                         'WHERE digest=?', (digest,)).fetchone()
        if (not row or row[4] is not None or not row[2] <= now < row[3]
                or self.participants.get(row[1]) != row[0]):
            raise PermissionError('Local session invalid or expired')
        return row

    def resolve(self, token):
        digest = token_hash(token)
        with self._transaction() as (db, now):
            return Principal(self._session(db, digest, now)[0])

    def role(self, token):
        digest = token_hash(token)
        with self._transaction() as (db, now):
            return self._session(db, digest, now)[1]

    def rotate(self, token):
        digest, replacement = token_hash(token), secrets.token_urlsafe(32)
        with self._transaction() as (db, now):
            row = self._session(db, digest, now)
            self._prune(db, now)
            db.execute('UPDATE local_sessions SET revoked=? WHERE digest=?', (now, digest))
            self._prune(db, now)
            db.execute('INSERT INTO local_sessions VALUES (?,?,?,?,?,NULL)',
                       (token_hash(replacement), row[0], row[1], now, row[3]))
            # Rotation cannot push the absolute session deadline into the future.
            return {'token': replacement, 'role': row[1], 'expires_at': row[3]}

    def revoke(self, token):
        digest = token_hash(token)
        with self._transaction() as (db, now):
            self._session(db, digest, now)
            db.execute('UPDATE local_sessions SET revoked=? WHERE digest=?', (now, digest))
