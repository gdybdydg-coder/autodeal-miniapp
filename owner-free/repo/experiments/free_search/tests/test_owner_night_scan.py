from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.free_search.owner_budget import Budget
from experiments.free_search.owner_night_runtime import Coordinator
from experiments.free_search.owner_night_scan import run
from experiments.free_search.tests.test_owner_scan import page, cards


class NightScanRunnerTests(unittest.TestCase):
    def test_two_invocations_resume_then_persist_scope_block_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / 'lock.sqlite3'
            lock = Coordinator(lock_path)
            lock.acquire('fixture_worker', 100)
            lock.close()
            def budget():
                return Budget(root / 'ledger.sqlite3', 40, 100000000)
            with patch('experiments.free_search.owner_night_scan.WORKSPACE', root), \
                 patch('experiments.free_search.owner_night_scan.LOCK', lock_path), \
                 patch('experiments.free_search.owner_night_scan.night_budget', budget), \
                 patch('experiments.free_search.owner_night_scan.PublicFetch') as fetch, \
                 patch('experiments.free_search.owner_night_scan.inspect_page') as metadata, \
                 patch('experiments.free_search.owner_night_scan.parse_public_cards', return_value=cards(1)), \
                 patch('experiments.free_search.owner_night_scan.time.time', return_value=100):
                fetch.return_value.get.return_value = ('fixture', {'completed_at':101})
                metadata.return_value = page()
                first = run('fixture_worker', 1)
                self.assertEqual(first['status'], 'bounded_pause_cursor_retained')
                self.assertEqual(first['scan']['page_index'], 1)
                metadata.return_value = page(1, True, scope='lang_id=4&top=1&custom=1')
                second = run('fixture_worker', 1)
                self.assertEqual(second['scan']['reason'], 'scope_metadata_changed')
                calls = fetch.return_value.get.call_count
                third = run('fixture_worker', 1)
                self.assertEqual(third['status'], 'persisted_scan_block_no_retry')
                self.assertEqual(fetch.return_value.get.call_count, calls)
                self.assertEqual(third['telegram_calls'], 0)

    def test_missing_executor_lock_denies_before_fetch(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('experiments.free_search.owner_night_scan.LOCK', Path(directory) / 'lock.sqlite3'), \
             patch('experiments.free_search.owner_night_scan.PublicFetch') as fetch:
            with self.assertRaisesRegex(ValueError, 'executor_lock_required'):
                run('fixture_worker')
            fetch.assert_not_called()
