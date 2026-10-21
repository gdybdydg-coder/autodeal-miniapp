import tempfile,unittest
from pathlib import Path
from experiments.free_search.owner_session_runtime import session
from experiments.free_search.owner_night_runtime import Coordinator

class SessionRuntimeTests(unittest.TestCase):
    def test_new_session_keeps_expired_night_and_busy_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lock.sqlite3';now=1800000000
            c=Coordinator(path)
            with self.assertRaisesRegex(ValueError,'outside_night'):c.acquire('expired_token',now,renewed=True)
            with session(path,120,clock=lambda:now) as deadline:
                self.assertEqual(deadline,now+120)
                with self.assertRaisesRegex(ValueError,'executor_busy'):
                    with session(path,120,clock=lambda:now):pass
                self.assertTrue(c.status()['busy'])
            self.assertFalse(c.status()['busy']);c.close()

    def test_exception_releases_own_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lock.sqlite3'
            with self.assertRaises(KeyboardInterrupt):
                with session(path,60):raise KeyboardInterrupt
            c=Coordinator(path);self.assertFalse(c.status()['busy']);c.close()
