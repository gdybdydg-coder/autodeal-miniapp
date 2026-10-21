import tempfile
from pathlib import Path
from unittest import TestCase
from experiments.free_search.owner_night_runtime import Coordinator, RENEWED_START, RENEWED_DEADLINE


class RenewedNightTests(TestCase):
    def test_new_window_preserves_old_deadline_and_shared_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            lock=Coordinator(Path(folder)/'lock.sqlite3')
            try:
                now=RENEWED_START+1
                with self.assertRaisesRegex(ValueError,'outside_night'): lock.acquire('old_window',now)
                lock.acquire('new_window',now,renewed=True)
                with self.assertRaisesRegex(ValueError,'executor_busy'): lock.acquire('competitor',now,renewed=True)
                with self.assertRaisesRegex(ValueError,'not_lock_owner'): lock.release('competitor')
                lock.release('new_window')
                for boundary in (RENEWED_START,RENEWED_DEADLINE,RENEWED_DEADLINE+1):
                    with self.assertRaisesRegex(ValueError,'outside_night'): lock.acquire('new_window',boundary,renewed=True)
            finally: lock.close()
