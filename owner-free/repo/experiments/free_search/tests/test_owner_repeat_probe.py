import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.free_search.owner_repeat_probe import run
from experiments.free_search.public_cards import PublicCard


class RepeatTests(unittest.TestCase):
    def test_three_samples_classify_churn_without_publication_claim(self):
        first = PublicCard('1', None, 50, None, None, False, (), ())
        second = PublicCard('2', None, 140, None, None, False, (), ())
        with tempfile.TemporaryDirectory() as tmp, \
                patch('experiments.free_search.owner_repeat_probe.PublicFetch') as fetch, \
                patch('experiments.free_search.owner_repeat_probe.time.sleep'), \
                patch('experiments.free_search.owner_repeat_probe.time.time', return_value=1000), \
                patch('experiments.free_search.owner_repeat_probe.parse_public_cards',
                      side_effect=[[first], [first, second], [second]]):
            fetch.return_value.get.side_effect = [('<html/>', {'completed_at': t})
                                                 for t in (90, 100, 150, 200)]
            report = run(Path(tmp)/'ledger.sqlite3', Path(tmp)/'s.owner-observations.sqlite3')
        self.assertEqual(fetch.return_value.get.call_count, 4)
        self.assertEqual(report['status'], 'bounded_observation_complete')
        self.assertEqual(report['samples'][1]['entered_head'], 1)
        self.assertEqual(report['samples'][2]['left_head'], 1)
        self.assertEqual(report['samples'][2]['intake']['first_seen'], 0)
        self.assertIsNone(report['whole_market_recall'])
        self.assertIsNone(report['publication_latency'])

    def test_source_failure_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('experiments.free_search.owner_repeat_probe.PublicFetch') as fetch, \
                patch('experiments.free_search.owner_repeat_probe.time.sleep'):
            fetch.return_value.get.return_value = (None, {'status': 403})
            report = run(Path(tmp)/'ledger.sqlite3', Path(tmp)/'s.owner-observations.sqlite3')
        self.assertEqual(fetch.return_value.get.call_count, 1)
        self.assertEqual(report['status'], 'source_unavailable')
        self.assertEqual(report['samples'], [])
