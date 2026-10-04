"""Offline mutations of the DOM/state shapes observed on three real OLX pages.

These fixtures test provenance boundaries; they are not live ads or a market
accuracy sample. Seller/contact/VIN fields must never enter returned evidence.
"""
import copy
import json
import unittest
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot


NOW = 1791106400
URL = 'https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html'
AD = {
    'id': 123, 'url': URL, 'status': 'active', 'isActive': True,
    'price': {'free': False, 'exchange': False, 'budget': False,
              'displayValue': '25 500 грн.',
              'regularPrice': {'value': 25500, 'currencyCode': 'UAH'}},
    'params': [{'key': 'sale_terms', 'value': 'Звичайний продаж',
                'normalizedValue': ['regular_sale']}],
    'contact': 'PRIVATE_CONTACT_SENTINEL',
}


def page(*, ad=None, visible='25 500 грн.', description='Розмитнений, продаю цілим, не на розбір.',
         body_complete=True, description_complete=True, terms='Звичайний продаж',
         offer_amount=25500, offer_currency='UAH', state=True, vehicle_extra=None):
    vehicle = {'@type': 'Vehicle', 'sku': '123', 'url': URL, 'name': 'Example',
               'brand': 'Mazda', 'model': '626', 'productionDate': '1994',
               'category': 'https://www.olx.ua/uk/transport/legkovye-avtomobili/mazda/',
               'offers': {'price': offer_amount, 'priceCurrency': offer_currency}}
    vehicle.update(vehicle_extra or {})
    html = '<html><script type="application/ld+json">' + json.dumps(vehicle) + '</script>'
    if state:
        payload = json.dumps({'ad': {'ad': copy.deepcopy(AD if ad is None else ad)}})
        html += '<script id="olx-init-config">window.__PRERENDERED_STATE__ = ' + json.dumps(payload) + ';</script>'
    html += '<div data-testid="ad-price-container">' + visible + '</div>'
    html += '<p>Умови продажу: ' + terms + '</p><p>Розмитнена: Так</p>'
    html += '<div data-testid="ad_description">' + description
    if description_complete:
        html += '</div>'
    if body_complete:
        html += '</html>'
    return html.encode()


class ObservedPriceProvenanceTests(unittest.TestCase):
    def parse(self, **kwargs):
        truncated = kwargs.pop('truncated', False)
        return parse_detail_snapshot(page(**kwargs), fetched_at=NOW, truncated=truncated)

    def test_complete_ordinary_sale_corroborates_display_only(self):
        result = self.parse()
        car = result['listing']
        proof = car['observed_asking_display']
        self.assertEqual(proof['status'], 'corroborated_display')
        self.assertEqual((proof['amount'], proof['currency']), ('25500', 'UAH'))
        self.assertEqual(proof['state_regular_price'], {'amount': '25500', 'currency': 'UAH'})
        self.assertTrue(proof['description_reviewed_in_full'])
        self.assertEqual(car['eligibility_review']['status'], 'allowed')
        self.assertFalse(proof['original_seller_currency_verified'])
        self.assertIsNone(car['price_kind'])
        self.assertEqual(car['price_review']['status'], 'needs_review')
        self.assertFalse(result['summary']['full_price_verified'])
        self.assertFalse(result['summary']['publication_verified'])
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_different_display_currencies_are_not_original_currency_proof(self):
        ad = copy.deepcopy(AD)
        ad['price']['displayValue'] = '2 350 $'
        ad['price']['regularPrice']['value'] = 105508
        car = self.parse(ad=ad, visible='2 350 $', offer_amount=105508)['listing']
        proof = car['observed_asking_display']
        self.assertEqual(proof['status'], 'corroborated_display')
        self.assertEqual(proof['currency'], 'USD')
        self.assertEqual(proof['state_regular_price']['currency'], 'UAH')
        self.assertFalse(proof['original_seller_currency_verified'])
        self.assertIsNone(car['price_observations']['fx_rate'])

    def test_missing_state_cannot_be_supplied_by_arbitrary_full_price_flag(self):
        car = self.parse(state=False, vehicle_extra={'price_kind': 'full', 'originalCurrencyVerified': True})['listing']
        self.assertEqual(car['observed_asking_display']['status'], 'needs_review')
        self.assertIsNone(car['price_kind'])

    def test_wrong_source_identity_url_or_type_stops_corroboration(self):
        for key, value in [('id', 456), ('id', True), ('url', 'https://example.com/fake'), ('url', 123)]:
            with self.subTest(key=key, value=value):
                ad = copy.deepcopy(AD)
                ad[key] = value
                proof = self.parse(ad=ad)['listing']['observed_asking_display']
                self.assertIn('public_state_identity_mismatch', proof['reasons'])

    def test_partial_download_or_description_cannot_corroborate(self):
        for kwargs in [{'body_complete': False}, {'truncated': True}, {'description_complete': False}]:
            with self.subTest(kwargs=kwargs):
                proof = self.parse(**kwargs)['listing']['observed_asking_display']
                self.assertEqual(proof['status'], 'needs_review')
                self.assertFalse(proof['description_reviewed_in_full'])

    def test_source_display_disagreement_and_nonpositive_price_are_held(self):
        for text in ['26 500 грн.', '0 грн.', 'Ціну не вказано']:
            with self.subTest(text=text):
                proof = self.parse(visible=text)['listing']['observed_asking_display']
                self.assertEqual(proof['status'], 'needs_review')

    def test_same_currency_state_and_jsonld_conflicts_are_held(self):
        ad = copy.deepcopy(AD)
        ad['price']['regularPrice']['value'] = 26000
        self.assertIn('same_currency_state_price_conflict', self.parse(ad=ad)['listing']['observed_asking_display']['reasons'])
        self.assertIn('same_currency_price_conflict', self.parse(offer_amount=26000)['listing']['observed_asking_display']['reasons'])

    def test_nonordinary_sale_or_missing_source_flags_are_held(self):
        for flag in ['free', 'exchange', 'budget']:
            for value in [True, None, 'false']:
                with self.subTest(flag=flag, value=value):
                    ad = copy.deepcopy(AD)
                    ad['price'][flag] = value
                    self.assertEqual(self.parse(ad=ad)['listing']['observed_asking_display']['status'], 'needs_review')
        self.assertIn('visible_sale_terms_missing_or_conflicting', self.parse(terms='Обмін')['listing']['observed_asking_display']['reasons'])
        ad = copy.deepcopy(AD)
        ad['params'][0]['normalizedValue'] = ['regular_sale', 'exchange']
        self.assertIn('ordinary_sale_not_corroborated', self.parse(ad=ad)['listing']['observed_asking_display']['reasons'])

    def test_inactive_ad_and_financing_context_are_held(self):
        ad = copy.deepcopy(AD)
        ad['isActive'] = False
        self.assertIn('source_ad_not_active', self.parse(ad=ad)['listing']['observed_asking_display']['reasons'])
        for description in ['Продаю цілим. Ціна — перший внесок.', 'Щомісячний платіж 500 грн.']:
            with self.subTest(description=description):
                self.assertEqual(self.parse(description=description)['listing']['observed_asking_display']['status'], 'needs_review')

    def test_forbidden_vehicle_is_not_rescued_by_asking_display(self):
        car = self.parse(description='Продаю під розбір, по запчастинах.')['listing']
        self.assertEqual(car['eligibility_review']['status'], 'excluded')
        self.assertFalse(car['observed_asking_display']['full_price_verified'])
        self.assertIsNone(car['published_at'])


if __name__ == '__main__':
    unittest.main()
