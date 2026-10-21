from dataclasses import replace
import unittest
from experiments.free_search.owner_budget import public_url, Denied
from experiments.free_search.owner_pagination_probe import next_page, compare
from experiments.free_search.public_pagination import PageMetadata
from experiments.free_search.tests.test_owner_observations import card

FEED = 'https://auto.ria.com/uk/last/hour/'


class PaginationProbeTests(unittest.TestCase):
    def test_only_exact_observed_second_page_is_authorized(self):
        p = PageMetadata(FEED, 0, 20, 'scope', FEED+'?page=2', False, ())
        self.assertEqual(next_page(p), FEED+'?page=2')
        for suffix in ('?page=3', '?page=02', '?page=2&key=x', '?page=2#x'):
            with self.assertRaises(Denied):
                public_url(FEED+suffix)
        with self.assertRaises(Denied):
            next_page(replace(p, issues=('scope_unknown',)))
        with self.assertRaises(Denied):
            public_url('https://developers.ria.com/auto/search?page=2')

    def test_churn_overlap_and_size_change_never_prove_recall(self):
        p = PageMetadata(FEED, 0, 20, 'scope', FEED+'?page=2', False, ())
        q = PageMetadata(FEED+'?page=2', 1, 50, 'scope', None, False, ())
        result = compare([p, q, p], [[card('1'), card('2')],
                                    [card('2'), card('3')], [card('3'), card('4')]])
        self.assertEqual(result['first_head_page_two_overlap'], 1)
        self.assertEqual(result['left_head_seen_on_page_two'], 1)
        self.assertIn('page_size_changed', result['scan']['reasons'])
        self.assertIsNone(result['missed_listing_count'])
        self.assertFalse(result['nominal_windows']['actual_rank_coverage_proven'])
