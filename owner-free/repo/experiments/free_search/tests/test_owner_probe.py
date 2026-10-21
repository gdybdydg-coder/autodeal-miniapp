import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.free_search.owner_probe import run
from experiments.free_search.public_cards import PublicCard


class ProbeIntegrationTests(unittest.TestCase):
    def test_intake_survives_two_bounded_runs_without_claiming_publication(self):
        card = PublicCard('1', 'https://auto.ria.com/uk/auto_fixture_1.html',
                          90, None, None, False, (), ())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observations = root / 'test.owner-observations.sqlite3'
            with patch('experiments.free_search.owner_probe.PublicFetch') as fetch, \
                    patch('experiments.free_search.owner_probe.parse_public_cards', return_value=[card]):
                fetch.return_value.get.return_value = ('<html></html>', {'completed_at': 100})
                first = run(root / 'first.sqlite3', observations)
                fetch.return_value.get.return_value = ('<html></html>', {'completed_at': 110})
                second = run(root / 'second.sqlite3', observations)
            self.assertEqual(first['status'], 'parsed')
            self.assertTrue(first['intake']['cold_start'])
            self.assertFalse(second['intake']['cold_start'])
            self.assertEqual(second['intake']['first_seen'], 0)
            self.assertEqual(second['intake']['ready_for_delivery'], 0)
            self.assertEqual(second['pagination']['state'], 'incomplete')
            self.assertEqual(second['telegram_calls'], 0)
