"""Synthetic method and safety tests; these are not real appraisal accuracy."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

from experiments.olx_offline.fx import FXQuote, day, instant, nbu_url, normalize_price
from experiments.olx_offline.market import estimate_usd, compare_methods


NOW = 1790976600


def car(identity, price='10000', **changes):
    result = dict(source='olx', id=str(identity), price=price, currency='USD',
                  category='whole_passenger_car', price_kind='full',
                  price_review={'status': 'structured_full_price', 'reasons': []},
                  eligibility_review={'status': 'allowed'},
                  usd_price={'status': 'ready', 'original_amount': price,
                             'original_currency': 'USD', 'usd_amount': price, 'fx': None},
                  brand='Volkswagen', model='Golf', generation='VII', year=2016,
                  engine_cc=1400, transmission='manual', body='hatchback',
                  fuel='petrol', mileage_km=120000, region='Київська',
                  condition='undamaged', trim='test-trim', checked_at=NOW-100,
                  evidence='synthetic')
    result.update(changes)
    return result


def analogs(prices=None):
    return [car('comp'+str(i), str(value)) for i, value in enumerate(prices or [10000] * 12)]


def uah(identity, price='210000', rate='42', effective='2026-10-03'):
    result = car(identity, price, currency='UAH')
    result['usd_price'] = dict(status='ready', original_amount=price, original_currency='UAH',
                               usd_amount=str(Decimal(price)/Decimal(rate)),
                               fx=dict(uah_per_usd=rate, source=nbu_url(effective),
                                       effective_date=effective, fetched_at=instant(NOW-100).isoformat()))
    return result


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('No network allowed'))
        self.network.start()

    def tearDown(self):
        self.network.stop()

    def test_no_auto_ria_margin_exact_discount_and_no_recommendation(self):
        result = estimate_usd(car('target', '8000'), analogs(), NOW)
        self.assertEqual(result['status'], 'experimental_estimate')
        self.assertEqual(Decimal(result['reference_usd']), Decimal('10000'))
        self.assertEqual(Decimal(result['discount_percent']), Decimal('20'))
        self.assertEqual(result['extra_margin_percent'], '0')
        self.assertIsNone(result['recommended'])
        self.assertFalse(result['real_world_accuracy_verified'])

    def test_three_methods_differ_without_claiming_accuracy(self):
        prices = ['9000', '9200', '9400', '9600', '9800', '10000', '10200', '10400', '10600', '10800', '11000', '11200']
        report = compare_methods(car('target', '8000'), iter(analogs(prices)), NOW)
        methods = report['methods']
        self.assertEqual(Decimal(methods['lower_quartile']['reference_usd']), Decimal('9550'))
        self.assertEqual(Decimal(methods['robust_median']['reference_usd']), Decimal('10100'))
        self.assertIn('weighted_similar', methods)
        self.assertTrue(report['selection_is_provisional'])
        self.assertFalse(report['real_world_accuracy_verified'])

    def test_forbidden_target_or_unreviewed_never_estimated(self):
        for status in ('excluded', 'needs_review', None):
            with self.subTest(status=status):
                target = car('target', eligibility_review={'status': status})
                self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'eligibility_not_allowed')

    def test_forbidden_comparables_do_not_lower_reference(self):
        forbidden = [car('forbidden'+str(i), '100', eligibility_review={'status': 'excluded'}) for i in range(30)]
        result = estimate_usd(car('target'), analogs()+forbidden, NOW)
        self.assertEqual(Decimal(result['reference_usd']), Decimal('10000'))
        self.assertEqual(result['sample'], 12)
        self.assertEqual(result['exclusions']['eligibility_not_allowed'], 30)

    def test_deposit_and_conflicts_are_not_comparables(self):
        for changes in ({'price_kind': 'deposit'}, {'price_review': {'status': 'needs_review', 'reasons': ['monthly']}},
                        {'field_conflicts': ['price']}, {'category': 'parts'}):
            with self.subTest(changes=changes):
                rows = [car(i, **changes) for i in range(12)]
                self.assertEqual(estimate_usd(car('target'), rows, NOW)['sample'], 0)

    def test_same_id_namespaced_and_verified_vehicle_dedup(self):
        rows = [car('same-id')] * 20
        result = estimate_usd(car('target'), rows, NOW)
        self.assertEqual(result['sample'], 1)
        rows += [car('same-id', source='auto_ria')]
        self.assertEqual(estimate_usd(car('target'), rows, NOW)['sample'], 2)
        rows = [car(str(i), vehicle_key='verified-car') for i in range(20)]
        self.assertEqual(estimate_usd(car('target'), rows, NOW)['sample'], 1)

    def test_self_and_crosspost_are_not_independent(self):
        target = car('target', vehicle_key='verified-target')
        rows = [deepcopy(target), car('crosspost', vehicle_key='verified-target'), *analogs()]
        result = estimate_usd(target, rows, NOW)
        self.assertEqual(result['sample'], 12)
        self.assertEqual(result['exclusions']['self_or_verified_duplicate'], 2)

    def test_forbidden_verified_crosspost_cannot_launder_older_analogue(self):
        rows = analogs()
        rows += [car('old', '100', vehicle_key='proven-crosspost', checked_at=NOW-200),
                 car('new', '100', vehicle_key='proven-crosspost',
                     eligibility_review={'status': 'excluded'})]
        result = estimate_usd(car('target'), rows, NOW)
        self.assertEqual(result['sample'], 12)
        self.assertEqual(result['exclusions']['verified_vehicle_current_policy_not_allowed'], 2)

    def test_latest_version_wins_and_same_time_conflict_is_held(self):
        latest = analogs()
        old = [car('comp'+str(i), '9000', checked_at=NOW-200) for i in range(12)]
        result = estimate_usd(car('target'), old+latest, NOW)
        self.assertEqual(Decimal(result['reference_usd']), Decimal('10000'))
        conflict = car('comp0', '9500')
        result = estimate_usd(car('target'), latest+[conflict], NOW)
        self.assertEqual(result['sample'], 11)
        self.assertEqual(result['exclusions']['conflicting_duplicate_observation'], 2)

    def test_stale_future_and_incompatible_fields(self):
        changes_list = ({'checked_at': NOW-31*86400}, {'checked_at': NOW+1},
                        {'model': 'Polo'}, {'generation': 'VI'}, {'year': 2010},
                        {'engine_cc': 1600}, {'mileage_km': 200000}, {'body': 'sedan'},
                        {'transmission': 'automatic'}, {'trim': 'different'})
        for changes in changes_list:
            with self.subTest(changes=changes):
                self.assertEqual(estimate_usd(car('target'), [car(i, **changes) for i in range(12)], NOW)['sample'], 0)

    def test_missing_gear_is_unknown_valuation_not_claimed_filter_failure(self):
        result = estimate_usd(car('target', transmission=None), analogs(), NOW)
        self.assertEqual(result['reason'], 'missing_comparison_attributes')
        self.assertNotIn('filter', result)

    def test_damaged_car_needs_comparable_condition_not_automatic_exclusion(self):
        target = car('target', condition='damaged')
        self.assertEqual(estimate_usd(target, analogs(), NOW)['sample'], 0)
        rows = [car(i, condition='damaged') for i in range(12)]
        result = estimate_usd(target, rows, NOW)
        self.assertEqual(result['status'], 'experimental_estimate')
        self.assertIn('repair_severity_and_cost_not_verified', result['reasons'])
        unknown = [car(i, condition=None) for i in range(12)]
        result = estimate_usd(car('target', condition=None), unknown, NOW)
        self.assertIn('condition_unknown_for_target_and_comparables', result['reasons'])

    def test_low_sample_outlier_and_wide_dispersion(self):
        self.assertEqual(estimate_usd(car('target'), analogs()[:7], NOW)['reason'], 'insufficient_comparables')
        result = estimate_usd(car('target'), analogs()+[car('outlier', '999999')], NOW)
        self.assertEqual(result['sample'], 12)
        self.assertEqual(result['exclusions']['price_outlier'], 1)
        values = [5000, 6000, 7000, 8000, 10000, 12000, 14000, 16000]
        self.assertEqual(estimate_usd(car('target'), analogs(values), NOW)['reason'], 'wide_price_dispersion')

    def test_usd_normalization_and_full_price_must_be_proven(self):
        for proof in (None, {'status': 'pending'}, {'status': 'ready', 'usd_amount': 'NaN'}):
            with self.subTest(proof=proof):
                self.assertEqual(estimate_usd(car('target', usd_price=proof), analogs(), NOW)['status'], 'profitability_unconfirmed')
        target = car('target')
        target['usd_price']['usd_amount'] = '5000'
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'double_conversion_or_usd_conflict')

    def test_uah_usd_same_basis_and_exact_arithmetic(self):
        target = uah('target', '210000', '42')
        result = estimate_usd(target, analogs(['6000']*12), NOW)
        self.assertEqual(result['status'], 'experimental_estimate')
        self.assertTrue(result['discount_percent'].startswith('16.666666666666666'))
        self.assertEqual(result['currency'], 'USD')
        self.assertEqual(target['price'], '210000')
        target['usd_price']['usd_amount'] = '6000'
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'fx_arithmetic_conflict')

    def test_mixed_fx_basis_not_silently_compared(self):
        target = uah('target')
        rows = [uah(i, '252000') for i in range(8)]
        rows += [uah('other'+str(i), '252000', '41') for i in range(10)]
        result = estimate_usd(target, rows, NOW)
        self.assertEqual(result['sample'], 8)
        self.assertEqual(result['exclusions']['inconsistent_fx_basis'], 10)

    def test_future_fx_and_float_rejected(self):
        target = uah('target', effective='2026-10-05')
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'fx_future_date')
        target = car('target')
        target['usd_price']['usd_amount'] = 10000.0
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'invalid_usd_price')

    def test_real_fx_module_interface_and_date_cache_expiry(self):
        # 42 is an invented arithmetic fixture, not a claim about today's rate.
        quote = FXQuote(Decimal('42'), day('2026-10-03'), instant(NOW-100), nbu_url('2026-10-03'))
        target = car('target', '210000', currency='UAH')
        target['usd_price'] = normalize_price(target, quote, NOW)
        self.assertIsInstance(target['usd_price']['fx']['fetched_at'], str)
        result = estimate_usd(target, analogs(['6000'] * 12), NOW)
        self.assertEqual(result['status'], 'experimental_estimate')
        self.assertEqual(estimate_usd(target, analogs(), NOW+86400)['reason'], 'fx_wrong_effective_date')
        target['usd_price']['fx']['fetched_at'] = instant(NOW-86401).isoformat()
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'fx_stale_cache')
        target['usd_price']['fx']['source'] = 'https://example.invalid/rate'
        self.assertEqual(estimate_usd(target, analogs(), NOW)['reason'], 'invalid_fx_provenance')

    def test_method_parameters_and_inputs_unchanged(self):
        target, rows = car('target'), analogs()
        original = deepcopy((target, rows))
        compare_methods(target, rows, NOW)
        self.assertEqual((target, rows), original)
        with self.assertRaises(ValueError):
            estimate_usd(target, rows, NOW, method='ria_minus_5')
        with self.assertRaises(ValueError):
            estimate_usd(target, rows, NOW, minimum=1)

    def test_weighting_changes_reference_but_never_selects_biggest_discount(self):
        close = [car('close'+str(i), '10000') for i in range(4)]
        distant = [car('far'+str(i), '9000', year=2015, mileage_km=90000,
                        checked_at=NOW-20*86400, region='Одеська') for i in range(8)]
        report = compare_methods(car('target', '8000'), close+distant, NOW)
        self.assertEqual(Decimal(report['methods']['weighted_similar']['reference_usd']), Decimal('10000'))
        self.assertEqual(Decimal(report['methods']['lower_quartile']['reference_usd']), Decimal('9000'))
        self.assertEqual(report['selected_method'], 'lower_quartile')
        self.assertEqual(estimate_usd(car('target'), distant, NOW, max_age=86400)['sample'], 0)


if __name__ == '__main__':
    unittest.main()
