import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from experiments.free_search.owner_night_runtime import Coordinator, DEADLINE, NightBudget


class NightCoordinationTests(unittest.TestCase):
    def test_open_budget_cannot_reserve_after_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = NightBudget(Path(folder) / 'budget.sqlite3', 40, 100000000)
            try:
                url = 'https://auto.ria.com/robots.txt'
                budget.observe(url)
                with patch('experiments.free_search.owner_night_runtime.time.time', return_value=DEADLINE):
                    with self.assertRaisesRegex(ValueError, 'night_ended'):
                        budget.reserve(url, 20000, DEADLINE - 1)
                self.assertEqual(budget.summary()['reserved_gets'], 0)
            finally:
                budget.close()

    def test_two_connections_only_one_executor_and_wrong_release_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock.sqlite3'
            init = Coordinator(path)
            init.close()
            barrier = threading.Barrier(2)
            results = []

            def worker(token):
                lock = Coordinator(path)
                try:
                    barrier.wait(timeout=5)
                    try:
                        lock.acquire(token, DEADLINE - 100)
                        results.append(('won', token))
                    except ValueError as exc:
                        results.append((str(exc), token))
                finally:
                    lock.close()

            workers = [threading.Thread(target=worker, args=(token,))
                       for token in ('worker_one', 'worker_two')]
            for worker_thread in workers:
                worker_thread.start()
            for worker_thread in workers:
                worker_thread.join(timeout=10)
                self.assertFalse(worker_thread.is_alive())
            self.assertEqual(sorted(x[0] for x in results), ['executor_busy', 'won'])
            lock = Coordinator(path)
            try:
                with self.assertRaisesRegex(ValueError, 'not_lock_owner'):
                    lock.release('wrong_owner')
                self.assertTrue(lock.status()['busy'])
                winner = next(token for outcome, token in results if outcome == 'won')
                lock.release(winner)
                self.assertFalse(lock.status()['busy'])
            finally:
                lock.close()

    def test_restart_retains_claim_without_expiring_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock.sqlite3'
            lock = Coordinator(path)
            lock.acquire('first_worker', DEADLINE - 10000)
            lock.close()
            lock = Coordinator(path)
            try:
                with self.assertRaisesRegex(ValueError, 'executor_busy'):
                    lock.acquire('next_worker', DEADLINE - 1)
                lock.release('first_worker')
                with self.assertRaisesRegex(ValueError, 'outside_night'):
                    lock.acquire('next_worker', DEADLINE)
            finally:
                lock.close()
