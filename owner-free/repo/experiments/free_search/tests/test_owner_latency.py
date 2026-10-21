from unittest import TestCase
from experiments.free_search.owner_latency import summary


class LatencyTests(TestCase):
    def test_empty_is_unknown_and_nearest_rank_is_explicit(self):
        self.assertIsNone(summary([])['p95'])
        r=summary(range(1,101))
        self.assertEqual((r['n'],r['p50'],r['p95'],r['max']),(100,50,95,100))
        self.assertEqual(summary([283.48])['p95'],283.48)

    def test_invalid_clocks_are_not_silently_filtered(self):
        for value in (-1,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):summary([value])
