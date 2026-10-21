from unittest import TestCase
from unittest.mock import patch
from experiments.free_search.owner_head_probe import run,wait_for_fetch,RENEWED_DEADLINE


class HeadDeadlineTests(TestCase):
    def test_expired_run_never_opens_lock_or_ledger(self):
        with patch('experiments.free_search.owner_head_probe.time.time',return_value=RENEWED_DEADLINE), \
             patch('experiments.free_search.owner_head_probe.Coordinator') as lock:
            with self.assertRaisesRegex(ValueError,'night_ended'):run('unused')
            lock.assert_not_called()

    def test_rate_limit_wait_cannot_spend_transport_headroom(self):
        with patch('experiments.free_search.owner_head_probe.time.time',return_value=100), \
             patch('experiments.free_search.owner_head_probe.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'batch_deadline'):wait_for_fetch(110,130)
            sleep.assert_not_called()

    def test_suspended_process_rechecks_before_fetch(self):
        with patch('experiments.free_search.owner_head_probe.time.time',side_effect=[100,200]), \
             patch('experiments.free_search.owner_head_probe.time.sleep'):
            with self.assertRaisesRegex(ValueError,'batch_deadline'):wait_for_fetch(100,150)
