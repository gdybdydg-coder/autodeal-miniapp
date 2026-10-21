from dataclasses import replace
from decimal import Decimal
import unittest
from experiments.free_search.public_cards import PublicCard
from experiments.free_search.owner_control import freeze, compare, summary


class ControlTests(unittest.TestCase):
    def test_cohort_is_source_order_not_cheapest_or_feed_selected(self):
        cards = [PublicCard(str(i),f'https://auto.ria.com/uk/auto_fixture_{i}.html',
                            None,None,Decimal(20000-i*1000),False,(),()) for i in range(1,13)]
        cards[0] = replace(cards[0], promoted=True)
        result = freeze(cards)
        self.assertEqual([r['id'] for r in result['cohort']], [str(i) for i in range(2,12)])
        comparison = compare(result['cohort'], ['2'], ['3'])
        self.assertEqual(comparison['n'],10)
        self.assertEqual(comparison['overlap_n'],1)
        self.assertEqual(len(comparison['not_observed_in_feed_ids']),9)
        self.assertIsNone(comparison['recall'])
        self.assertIsNone(comparison['proven_misses'])
        self.assertTrue(all(r['valuation_status']=='unknown' for r in comparison['rows']))
        self.assertTrue(any(r['owner_price_preview_gate']=='outside_budget' for r in comparison['rows']))
        self.assertFalse(any(r['all_owner_filters_verified'] for r in comparison['rows']))

    def test_small_cohort_remains_small_and_empty_latency_unknown(self):
        self.assertEqual(freeze([])['selected_n'],0)
        self.assertIsNone(summary([])['p95'])
        self.assertEqual(summary([1,2,3])['p50'],2)
        self.assertEqual(summary([1,2,3])['p95'],3)
        with self.assertRaises(ValueError):summary([-1])
