import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_auth import LocalSessions, SESSION_TTL, INVITATION_TTL, AUTH_CAPACITY


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fixture.sqlite'
        self.now = 1000
        self.auth = LocalSessions(self.path, lambda: self.now, {'client':111, 'admin':777292211})

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, role='client'):
        return self.auth.login(self.auth.issue_invitation(role))

    def test_server_invitation_controls_principal_and_cannot_be_reused(self):
        invitation = self.auth.issue_invitation('client')
        session = self.auth.login(invitation)
        self.assertEqual(session['role'], 'client')
        self.assertEqual(self.auth.resolve(session['token']).uid,111)
        with self.assertRaises(PermissionError):
            self.auth.login(invitation)
        with self.assertRaises(ValueError):
            self.auth.issue_invitation('superuser')

    def test_invitation_expiry_boundary_is_exact(self):
        invitation = self.auth.issue_invitation('admin')
        self.now += INVITATION_TTL
        with self.assertRaises(PermissionError):
            self.auth.login(invitation)

    def test_expiry_and_failed_check_cannot_revive_after_clock_rollback(self):
        token = self.login()['token']
        self.now += SESSION_TTL-1
        self.assertEqual(self.auth.resolve(token).uid,111)
        self.now += 1
        with self.assertRaises(PermissionError):
            self.auth.resolve(token)
        self.now -= 1
        with self.assertRaises(PermissionError):
            self.auth.resolve(token)

    def test_rotation_revokes_old_token_and_keeps_absolute_deadline_across_restart(self):
        first = self.login('admin')
        self.now += 700
        second = self.auth.rotate(first['token'])
        self.assertNotEqual(first['token'],second['token'])
        self.assertEqual(first['expires_at'],second['expires_at'])
        restarted = LocalSessions(self.path,lambda:self.now,{'client':111,'admin':777292211})
        with self.assertRaises(PermissionError):
            restarted.resolve(first['token'])
        self.assertEqual(restarted.role(second['token']),'admin')
        restarted.revoke(second['token'])
        with self.assertRaises(PermissionError):
            self.auth.resolve(second['token'])

    def test_plaintext_tokens_are_not_written_to_sqlite(self):
        invitation = self.auth.issue_invitation('client')
        session = self.auth.login(invitation)
        raw = self.path.read_bytes()
        self.assertNotIn(invitation.encode(),raw)
        self.assertNotIn(session['token'].encode(),raw)
        for bad in [None,111,'x'*42,'x'*44,'../'+session['token']]:
            with self.assertRaises(PermissionError):
                self.auth.resolve(bad)

    def test_session_and_invitation_counts_remain_bounded_without_dropping_active_sessions(self):
        tokens = [self.login()['token'] for _ in range(AUTH_CAPACITY)]
        waiting = self.auth.issue_invitation('client')
        with self.assertRaises(ValueError):
            self.auth.login(waiting)
        self.auth.revoke(tokens[0])
        current = self.auth.login(waiting)
        for _ in range(AUTH_CAPACITY+2):
            current = self.auth.rotate(current['token'])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM local_sessions').fetchone()[0],AUTH_CAPACITY)
        self.assertEqual(self.auth.resolve(tokens[1]).uid,111)

    def test_unused_invitation_budget_and_pruning(self):
        for _ in range(AUTH_CAPACITY):
            self.auth.issue_invitation('client')
        with self.assertRaises(ValueError):
            self.auth.issue_invitation('admin')
        self.now += INVITATION_TTL
        self.auth.issue_invitation('admin')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM local_invitations').fetchone()[0],1)

    def test_parallel_exchange_has_exactly_one_winner(self):
        invitation = self.auth.issue_invitation('admin')
        def exchange(_):
            try:
                return self.auth.login(invitation)
            except PermissionError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            sessions = list(pool.map(exchange,range(4)))
        self.assertEqual(sum(s is not None for s in sessions),1)

    def test_changed_operator_mapping_does_not_transfer_existing_authority(self):
        token = self.login('admin')['token']
        restarted = LocalSessions(self.path,lambda:self.now,{'client':111,'admin':222})
        with self.assertRaises(PermissionError):
            restarted.resolve(token)


if __name__ == '__main__':
    unittest.main()
