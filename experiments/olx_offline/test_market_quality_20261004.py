"""Offline regressions; synthetic car identities and prices are not live evidence."""
from decimal import Decimal
import unittest
from unittest.mock import patch

from experiments.olx_offline.market import estimate_usd
from experiments.olx_offline.test_market import NOW, analogs, car, uah


class CurrentVerifiedVehicleTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('Offline only'))
        self.network.start()

    def tearDown(self):
        self.network.stop()

    def test_latest_unusable_crosspost_cannot_restore_old_comparable(self):
        invalid_updates = [
            {'price_kind': 'deposit'},
            {'price_review': {'status': 'needs_review', 'reasons': ['monthly_payment']}},
            {'field_conflicts': ['price']},
            {'transmission': None},
            {'usd_price': {'status': 'pending'}},
        ]
        for update in invalid_updates:
            with self.subTest(update=update):
                old = car('old', '10000', checked_at=NOW - 200, vehicle_key='verified-v')
                latest = car('latest', '10000', vehicle_key='verified-v', **update)
                result = estimate_usd(car('target'), analogs() + [old, latest], NOW)
                self.assertEqual(result['sample'], 12)
                self.assertNotIn('old', [r['id'] for r in result['used_comparables']])

    def test_latest_verified_vehicle_attributes_supersede_old_matching_specs(self):
        old = car('old', checked_at=NOW - 200, vehicle_key='verified-v')
        latest = car('latest', vehicle_key='verified-v', transmission='automatic')
        result = estimate_usd(car('target'), analogs() + [old, latest], NOW)
        self.assertEqual(result['sample'], 12)
        self.assertNotIn('old', [r['id'] for r in result['used_comparables']])

    def test_latest_current_crosspost_valid_price_is_used_once(self):
        old = car('old', '9500', checked_at=NOW - 200, vehicle_key='verified-v')
        latest = car('latest', '10000', vehicle_key='verified-v')
        result = estimate_usd(car('target'), analogs() + [old, latest], NOW)
        self.assertEqual(result['sample'], 13)
        used = {r['id']: r for r in result['used_comparables']}
        self.assertNotIn('old', used)
        self.assertEqual(used['latest']['usd_amount'], '10000')

    def test_latest_invalid_fx_crosspost_does_not_keep_old_usd_card(self):
        old = car('old', checked_at=NOW - 200, vehicle_key='verified-v')
        latest = uah('latest')
        latest['vehicle_key'] = 'verified-v'
        latest['usd_price']['fx']['fetched_at'] = '2026-10-01T00:00:00+00:00'
        result = estimate_usd(car('target'), analogs() + [old, latest], NOW)
        self.assertEqual(result['sample'], 12)

    def test_equal_time_crosspost_conflicts_do_not_pick_an_arbitrary_price_or_spec(self):
        for update in ({'price': '9500', 'usd_price': car('temporary', '9500')['usd_price']},
                       {'transmission': 'automatic'}):
            with self.subTest(update=update):
                rows = analogs() + [car('a', vehicle_key='verified-v'),
                                    car('b', vehicle_key='verified-v', **update)]
                result = estimate_usd(car('target'), rows, NOW)
                self.assertEqual(result['sample'], 12)
                self.assertEqual(result['exclusions']['conflicting_verified_vehicle_current_observations'], 2)

    def test_estimates_do_not_claim_independent_vehicles_without_identity(self):
        result = estimate_usd(car('target', '8000'), analogs(), NOW)
        self.assertEqual(result['sample_unit'], 'distinct_ads_after_known_vehicle_dedup')
        self.assertIn('crossposts_without_verified_vehicle_identity_may_remain', result['reasons'])
        self.assertFalse(result['real_world_accuracy_verified'])
        self.assertIsNone(result['recommended'])
        self.assertEqual(Decimal(result['discount_percent']), Decimal(20))

    def test_explicit_olx_pool_excludes_ria_peers_without_breaking_namespaces(self):
        rows = analogs()[:7] + [car('ria-' + str(i), source='auto_ria') for i in range(12)]
        result = estimate_usd(car('target'), rows, NOW, comparable_sources=('olx',))
        self.assertEqual(result['status'], 'profitability_unconfirmed')
        self.assertEqual(result['sample'], 7)
        self.assertEqual(result['exclusions']['source_outside_comparable_pool'], 12)
        self.assertEqual(estimate_usd(car('target'), rows, NOW)['sample'], 19)

    def test_invalid_pool_is_an_explicit_error(self):
        for sources in ('olx', (), ('hidden',)):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                estimate_usd(car('target'), analogs(), NOW, comparable_sources=sources)


if __name__ == '__main__':
    unittest.main()
