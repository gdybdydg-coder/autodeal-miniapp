from datetime import datetime, timezone
import json
import unittest

from experiments.free_search.valuation_benchmark import benchmark, canonical

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp()


def fixture(identity, price, at=NOW-1):
    return dict(source_id=identity, features={
        'brand': 'VW', 'model': 'Passat', 'generation_id': 5, 'year': 2003,
        'engine_cc': 1800, 'transmission': 'Ручна / Механіка',
        'fuel': 'Бензин, 1.8 л.', 'region': 'Львівська', 'mileage': 250000,
        'body': 'Седан',
    }, asking_price_usd=price, candidate_observed_at=at,
       historical_label={'lower_usd': 6000, 'quote_observed_at': at + .1})


class BenchmarkTest(unittest.TestCase):
    def test_same_target_is_not_its_own_peer_and_labels_not_forwarded(self):
        target = fixture('1', 3900)
        value = canonical(target)
        self.assertNotIn('historical_label', value)
        self.assertNotIn('lower_usd', value)
        self.assertEqual(value['fuel'], 'Бензин')
        result = benchmark({'items': [target]})
        self.assertEqual(result['status_counts'], {'unknown': 1})
        self.assertEqual(result['metrics_at_discount_threshold_percent']['10']['unknown_positive'], 1)
        self.assertEqual(result['external_calls'], 0)

    def test_normalized_peer_projection_and_future_time_guard(self):
        target = fixture('target-sensitive', 3900)
        past = [canonical(fixture(str(i), 5000, NOW - 10 - i)) for i in range(6)]
        future = [canonical(fixture('f'+str(i), 50000, NOW + 10 + i)) for i in range(6)]
        for row in past:
            row['price_usd'] = row.pop('price')
            row['historical_label'] = 'must not reach estimator'
        result = benchmark({'items': [target]}, {'items': past + future})
        self.assertEqual(result['status_counts'], {'estimated': 1})
        self.assertEqual(result['comparable_sample_count_histogram'], {6: 1})
        self.assertEqual(result['metrics_at_discount_threshold_percent']['10']['true_positive'], 1)
        self.assertNotIn('target-sensitive', json.dumps(result))
        self.assertNotIn('must not reach estimator', json.dumps(result))

    def test_empty_benchmark_has_no_fabricated_period_or_accuracy(self):
        result = benchmark({'items': []})
        self.assertIsNone(result['period_candidate_observed_utc'])
        self.assertIsNone(result['metrics_at_discount_threshold_percent']['10']['precision_on_labeled_predictions'])


if __name__ == '__main__':
    unittest.main()
