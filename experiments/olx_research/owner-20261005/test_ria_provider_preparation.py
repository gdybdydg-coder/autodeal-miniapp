"""Synthetic contracts plus saved-real missing-field test; no live API evidence.

Run directly with stdlib. Network fence is installed BEFORE project imports.
"""
import copy
import json
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError('network_forbidden_in_offline_tests')


socket.socket = denied
socket.create_connection = denied
socket.getaddrinfo = denied
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.olx_market import ria_provider as provider

NOW = 1791194000


def target():
    # Explicitly synthetic. Values below do not claim dictionary verification.
    return dict(source='olx', id='900000001',
        url='https://www.olx.ua/d/obyavlenie/example-ID11o4pQ.html',
        brand='Skoda', model='Octavia', generation='A5', body='liftback',
        fuel='diesel', transmission='manual', drive_type='front',
        engine_cc=1600, power_hp=105, year=2010, mileage_km=270000,
        category='whole_passenger_car', research_condition='seller_declared_running',
        price='6000', currency='USD', checked_at=NOW,
        eligibility_review={'status': 'allowed', 'description_complete': True})


def mapping(car):
    return {k: {'value': car[k], 'id': i+1,
        'dictionary_url': 'https://developers.ria.com/auto/categories/1/',
        'checked_at': NOW} for i,k in enumerate(provider.IDS)}


def response():
    return {'statisticData': [{'type': 'avgPrice', 'price': {'USD': 7000},
        'avgValueRange': .1, 'quantityAdv': 200}],
        'similarCars': [{'id': i, 'VIN': 'DO_NOT_RETAIN',
                        'plateNumber': 'DO_NOT_RETAIN'} for i in range(1, 10)]}


class ProviderPreparation(unittest.TestCase):
    def setUp(self):
        self.car = target()
        self.mapping = mapping(self.car)
        self.request = provider.prepare(self.car, self.mapping, NOW, period_parameter=168)

    def test_exact_parameters_convert_units_and_do_not_send_olx_id(self):
        p = self.request['body']['params']
        self.assertEqual(p['mileage'], {'gte': '270', 'lte': '270'})
        self.assertEqual(p['engineVolume'], {'gte': '1.6', 'lte': '1.6'})
        self.assertEqual(p['year'], {'gte': '2010', 'lte': '2010'})
        self.assertEqual(p['power'], 105)
        self.assertNotIn('omniId', p)
        for k in ('technicalConditionId', 'paintConditionId', 'damage'):
            self.assertNotIn(k, p)

    def test_wrong_mapping_is_rejected_before_transport(self):
        for key in provider.IDS:
            m = copy.deepcopy(self.mapping)
            m[key]['value'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'mapping_unverified'):
                provider.prepare(self.car, m, NOW, period_parameter=168)

    def test_missing_critical_fields_are_not_model_defaults(self):
        for key in (*provider.IDS, 'engine_cc', 'power_hp', 'mileage_km', 'year'):
            car = self.car | {key: None}
            with self.assertRaises(ValueError):
                provider.prepare(car, self.mapping, NOW, period_parameter=168)

    def test_official_mapping_must_not_contain_credentials(self):
        for url in ('https://example.org/auto/',
                    'https://developers.ria.com/auto/?api_key=secret',
                    'https://secret@developers.ria.com/auto/'):
            m = copy.deepcopy(self.mapping)
            m['brand']['dictionary_url'] = url
            with self.assertRaises(ValueError):
                provider.prepare(self.car, m, NOW, period_parameter=168)

    def test_changed_price_or_car_cannot_reuse_observation(self):
        for field, value in (('id','900000002'), ('price','6500'),
                             ('body','wagon'), ('research_condition','body_dents'),
                             ('checked_at', NOW-1)):
            with self.assertRaisesRegex(ValueError, 'binding_invalid'):
                provider.observe(response(), self.request, self.car | {field: value}, NOW)

    def test_stale_card_and_omni_request_are_rejected(self):
        with self.assertRaises(ValueError):
            provider.observe(response(), self.request, self.car, NOW+301)
        req = copy.deepcopy(self.request)
        req['body']['params']['omniId'] = self.car['id']
        req['request_sha256'] = provider.digest(req['body'])
        with self.assertRaises(ValueError):
            provider.observe(response(), req, self.car, NOW)

    def test_provider_count_is_not_independent_compatible_count(self):
        data = response()
        data['similarCars'] += data['similarCars']
        observed = provider.observe(data, self.request, self.car, NOW)
        self.assertEqual(observed['returned_distinct_ad_count'], 9)
        self.assertEqual(observed['provider_quantity'], 200)
        self.assertIsNone(observed['verified_independent_compatible_count'])
        self.assertFalse(observed['technical_ready'])
        self.assertEqual(observed['status'], 'provider_observation_not_admitted')

    def test_private_fields_do_not_survive_projection(self):
        self.mapping['brand']['VIN'] = 'DO_NOT_RETAIN'
        req = provider.prepare(self.car, self.mapping, NOW, period_parameter=168)
        obs = provider.observe(response(), req, self.car, NOW)
        self.assertNotIn('DO_NOT_RETAIN', json.dumps([req, obs]))
        self.assertNotIn('similarCars', obs)

    def test_missing_range_stays_missing(self):
        data = response()
        del data['statisticData'][0]['avgValueRange']
        observed = provider.observe(data, self.request, self.car, NOW)
        self.assertIsNone(observed['provider_range_usd'])
        self.assertIn('provider_range_missing', observed['blockers'])
        self.assertFalse(observed['technical_ready'])

    def test_invalid_averages_and_ranges_fail(self):
        for field, values in (('USD', (0, -1, True, 'NaN', 'Infinity')),
                              ('avgValueRange', (0, -1, True, 1, 'NaN'))):
            for value in values:
                data = response()
                block = data['statisticData'][0]
                (block['price'] if field=='USD' else block)[field] = value
                with self.assertRaises(ValueError):
                    provider.observe(data, self.request, self.car, NOW)

    def test_discount_uses_provider_average_without_ria_minus_five(self):
        benefit = provider.discount_percent('7000', '6000')
        self.assertAlmostEqual(float(benefit), 14.285714285714286)
        self.assertGreater(benefit, 10)
        self.assertLess(provider.discount_percent('6000', '7000'), 0)

    def test_saved_real_unknown_power_remains_unknown(self):
        data = json.loads((Path(__file__).parent/'enable-new-details-sanitized.json').read_text())
        car = next(c for c in data if c['id']=='936387379')
        self.assertIsNone(car['power_hp'])
        with self.assertRaises(ValueError):
            provider.prepare(car, mapping(car), car['checked_at'], period_parameter=168)


if __name__ == '__main__':
    unittest.main(verbosity=2)
