"""Synthetic fixtures only. No source HTML, credentials or personal data."""
import ast
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from experiments.free_search.public_details import ParseError, parse_public_details

ID = "12345678"
URL = "https://auto.ria.com/uk/auto_example_model_12345678.html"
BASE = {"@type": "Vehicle", "@id": URL, "url": URL,
        "brand": {"@type": "Brand", "name": "Example"}, "model": "Model",
        "productionDate": "2010", "fuelType": "Дизель", "bodyType": "Універсал",
        "vehicleTransmission": "Механічна",
        "mileageFromOdometer": {"value": 170000, "unitCode": "KMT"},
        "offers": {"@type": "Offer", "price": "5300.50", "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock"}}


def html(*nodes):
    return "".join('<script type="application/ld+json">' + json.dumps(n, ensure_ascii=False) + '</script>' for n in nodes)


class DetailsTests(unittest.TestCase):
    def parse(self, v=None):
        return parse_public_details(html(BASE if v is None else v), ID)

    def rejects(self, v, reason):
        with self.assertRaisesRegex(ParseError, '^' + reason + '$'):
            self.parse(v)

    def test_valid_exact_decimal_and_provenance(self):
        d = self.parse()
        self.assertEqual((d.price, d.currency, d.mileage_km), (Decimal('5300.50'), 'USD', Decimal(170000)))
        self.assertEqual((d.brand, d.model, d.year), ('Example', 'Model', 2010))
        self.assertEqual(dict(d.provenance)['price'], 'public_jsonld')
        self.assertFalse(d.ready_for_delivery)

    def test_multiple_blocks_and_image_only_vehicle(self):
        image = {'@type': 'Vehicle', '@id': URL + '#images', 'image': [{'contentUrl': 'https://example.invalid/img.jpg'}]}
        d = parse_public_details(html({'@type': 'BreadcrumbList'}, BASE, image), ID)
        self.assertEqual(d.price, Decimal('5300.50'))

    def test_graph_and_list_type(self):
        v = deepcopy(BASE); v['@type'] = ['Thing', 'Vehicle']
        d = self.parse({'@graph': [v]})
        self.assertEqual(d.model, 'Model')

    def test_missing_optional_values_not_rejected(self):
        v = deepcopy(BASE)
        for k in ('fuelType', 'vehicleTransmission', 'mileageFromOdometer'): v.pop(k)
        d = self.parse(v)
        self.assertIsNone(d.fuel); self.assertIsNone(d.transmission)
        self.assertIn('fuel', d.missing)

    def test_damage_is_recorded_not_blocked(self):
        v = deepcopy(BASE); v['itemCondition'] = 'https://schema.org/DamagedCondition'
        d = self.parse(v)
        self.assertEqual(d.condition, v['itemCondition'])
        self.assertNotIn('damage', d.issues)

    def test_private_data_is_never_in_output(self):
        v = deepcopy(BASE)
        v.update(description='PRIVATE_DESCRIPTION', vehicleIdentificationNumber='PRIVATE_VIN', seller={'telephone': 'PRIVATE_PHONE'})
        self.assertNotIn('PRIVATE_', repr(self.parse(v)))

    def test_foreign_host_identity(self):
        v = deepcopy(BASE); v['@id'] = URL.replace('auto.ria.com', 'evil.invalid')
        self.rejects(v, 'invalid_identity')

    def test_mixed_listing_id(self):
        v = deepcopy(BASE); v['url'] = URL.replace(ID, '98765432')
        self.rejects(v, 'identity_mismatch')

    def test_recommended_other_vehicle_not_merged(self):
        other = deepcopy(BASE)
        other['url'] = other['@id'] = URL.replace(ID, '98765432')
        self.rejects([BASE, other], 'identity_mismatch')

    def test_identity_is_required(self):
        v = deepcopy(BASE); v.pop('@id'); v.pop('url')
        self.rejects(v, 'identity_mismatch')

    def test_malformed_identity(self):
        for url in ('https://[bad', 'http://auto.ria.com/uk/auto_example_12345678.html', URL+'?x=1', URL.replace('auto.ria.com', 'auto.ria.com:443')):
            with self.subTest(url=url):
                v = deepcopy(BASE); v['@id'] = url
                self.rejects(v, 'invalid_identity')

    def test_conflicting_prices(self):
        v = deepcopy(BASE); v['offers'] = [deepcopy(BASE['offers']), deepcopy(BASE['offers'])]
        v['offers'][1]['price'] = '6000'
        self.rejects(v, 'conflicting_price')

    def test_conflicting_fuel_sources(self):
        v = deepcopy(BASE); v['vehicleEngine'] = {'fuelType': 'Бензин'}
        self.rejects(v, 'conflicting_fuel')

    def test_conflicting_vehicle_models(self):
        v = deepcopy(BASE); v['model'] = 'Other'
        self.rejects([BASE, v], 'conflicting_model')

    def test_duplicate_same_offer(self):
        v = deepcopy(BASE); v['offers'] = [deepcopy(BASE['offers']), deepcopy(BASE['offers'])]
        self.assertEqual(self.parse(v).price, Decimal('5300.50'))

    def test_invalid_prices(self):
        for price in (0, -10, True, 'NaN', 'Infinity', '1e99', '', '5,300', {}, []):
            with self.subTest(price=price):
                v = deepcopy(BASE); v['offers']['price'] = price
                self.rejects(v, 'invalid_price')

    def test_non_usd_preserved_without_conversion(self):
        v = deepcopy(BASE); v['offers']['priceCurrency'] = 'UAH'
        d = self.parse(v)
        self.assertEqual(d.price, Decimal('5300.50'))
        self.assertIn('non_usd_price', d.issues)
        self.assertFalse(d.ready_for_delivery)

    def test_invalid_currency_shapes(self):
        for currency in (None, [], {}, 'usd', 'GBP'):
            with self.subTest(currency=currency):
                v = deepcopy(BASE); v['offers']['priceCurrency'] = currency
                self.rejects(v, 'invalid_currency')

    def test_availability_is_explicit(self):
        for source, expected in [('https://schema.org/SoldOut', 'unavailable'), ('https://schema.org/OutOfStock', 'unavailable'), ('https://schema.org/Discontinued', 'unavailable'), ('https://schema.org/PreOrder', 'unknown'), (None, 'unknown'), ({}, 'unknown')]:
            with self.subTest(source=source):
                v = deepcopy(BASE); v['offers']['availability'] = source
                d = self.parse(v)
                self.assertEqual(d.availability, expected)
                self.assertIn('availability_not_active', d.issues)

    def test_missing_offer_does_not_authorize_delivery(self):
        v = deepcopy(BASE); v.pop('offers')
        d = self.parse(v)
        self.assertIn('price', d.missing); self.assertEqual(d.availability, 'unknown')
        self.assertFalse(d.ready_for_delivery)

    def test_invalid_mileage_units_and_values(self):
        for odo in ({'value': 100, 'unitCode': 'SMI'}, {'value': -1, 'unitCode': 'KMT'}, {'value': True, 'unitCode': 'KMT'}, {'value': 100, 'unitCode': []}, '170000'):
            with self.subTest(odo=odo):
                v = deepcopy(BASE); v['mileageFromOdometer'] = odo
                d = self.parse(v)
                self.assertIsNone(d.mileage_km); self.assertIn('invalid_mileage', d.issues)

    def test_invalid_optional_distinguished_from_absent(self):
        v = deepcopy(BASE); v['fuelType'] = {}; v['productionDate'] = 'not-a-year'
        d = self.parse(v)
        self.assertIn('invalid_fuel', d.issues); self.assertIn('invalid_year', d.issues)
        self.assertIn('year', d.missing)

    def test_bad_json_and_duplicate_keys(self):
        for payload, code in [('not-json', 'invalid_jsonld'), ('{"@type":"Vehicle","@type":"Thing"}', 'duplicate_json_key'), ('{"x":NaN}', 'invalid_json_number')]:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ParseError, '^'+code+'$'):
                    parse_public_details('<script type="application/ld+json">'+payload+'</script>', ID)

    def test_resource_limits(self):
        for body, code in [('x'*2500001, 'html_oversize'), ('<script type="application/ld+json">'+'x'*256001+'</script>', 'script_oversize'), (html(*([{}]*65)), 'too_many_scripts'), (html([{}]*501), 'too_many_nodes')]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ParseError, '^'+code+'$'): parse_public_details(body, ID)

    def test_unterminated_and_empty(self):
        for body, code in [('<script type="application/ld+json">{}', 'unterminated_jsonld'), ('<script type="application/json">{}</script>', 'no_vehicle'), ('<script type></script>', 'no_vehicle')]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ParseError, '^'+code+'$'): parse_public_details(body, ID)

    def test_no_network_or_production_dependencies(self):
        path = Path(__file__).parents[1] / 'public_details.py'
        tree = ast.parse(path.read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(modules, {'dataclasses', 'decimal', 'html.parser', 'json', 're', 'urllib.parse'})

    def test_canonical_page_identity(self):
        with self.assertRaisesRegex(ParseError, '^canonical_mismatch$'):
            parse_public_details('<link rel="canonical" href="'+URL.replace(ID, '98765432')+'">'+html(BASE), ID)
        self.assertEqual(parse_public_details('<link rel="canonical" href="'+URL+'">'+html(BASE), ID).listing_id, ID)

    def test_offer_cannot_belong_to_another_listing(self):
        v = deepcopy(BASE); v['offers']['url'] = URL.replace(ID, '98765432')
        self.rejects(v, 'offer_identity_mismatch')

    def test_optional_invalid_shape_does_not_crash(self):
        v = deepcopy(BASE)
        v.update(bodyType=[], brand=True, model={}, vehicleTransmission=12)
        d = self.parse(v)
        self.assertIsNone(d.body)
        self.assertIn('invalid_brand', d.issues)

    def test_missing_expected_id_and_bad_offer_shapes(self):
        for expected in (None, 12345678, '0', '123x'):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ParseError, '^invalid_expected_id$'):
                    parse_public_details(html(BASE), expected)
        for offers, code in [('bad', 'invalid_offers'), ([None], 'invalid_offer'), ({'@type': 'AggregateOffer'}, 'invalid_offer')]:
            with self.subTest(offers=offers):
                v = deepcopy(BASE); v['offers'] = offers
                self.rejects(v, code)


if __name__ == '__main__':
    unittest.main()
