import json
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.pipeline import Pipeline
from experiments.olx_offline.test_pipeline import NOW


def sample(currency='UAH',amount=258685):
    v={'@type':'Vehicle','sku':'123','url':'https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html','name':'Example',
       'brand':'Skoda','model':'Fabia','productionDate':'2012','offers':{'price':amount,'priceCurrency':currency},
       'vehicleIdentificationNumber':'PRIVATE_VIN_SENTINEL','description':'PRIVATE_DESCRIPTION_SENTINEL',
       'image':['https://ireland.apollo.olxcdn.com/example.jpg']}
    return '<html><script type="application/ld+json">'+json.dumps(v)+'</script><div data-testid="ad-price-container">5 750 $ Договірна</div><p>Пробіг: 192 тис.км.</p><p>Об\'єм двигуна: 1.40 л.</p><p>Вид палива: Бензин</p><p>Коробка передач: Механічна</p><p>Тип кузова: Хетчбек</p><div data-testid="ad-posted-at">Опубліковано сьогодні о 17:34</div><p>Продавець: PRIVATE_SELLER_SENTINEL</p></html>'


def geography_sample(links=(), *, area=None, close=True, scoped=True):
    html = sample()
    match = re.search(r'(<script type="application/ld\+json">)(.*?)(</script>)', html)
    vehicle = json.loads(match[2])
    vehicle['category'] = 'https://www.olx.ua/transport/legkovye-avtomobili/skoda/'
    if area is not None:
        vehicle['offers']['areaServed'] = area
    html = html[:match.start(2)] + json.dumps(vehicle) + html[match.end(2):]
    anchors = ''.join('<a href="' + href + '">Skoda - ' + value + '</a>' for value, href in links)
    breadcrumbs = '<nav' + (' data-testid="breadcrumbs"' if scoped else '') + '>' + anchors + ('</nav>' if close else '')
    return html.replace('</html>', breadcrumbs + '</html>')


REGION = ('Тернопільська область', '/uk/transport/legkovye-avtomobili/skoda/ter/')
CITY = ('Тернопіль', '/uk/transport/legkovye-avtomobili/skoda/ternopol/')
DISTRICT = ('Центральний', '/uk/transport/legkovye-avtomobili/skoda/ternopol/?search%5Bdistrict_id%5D=125')


class DetailSnapshotTests(unittest.TestCase):
    def parse(self,s):return parse_detail_snapshot(s.encode(),fetched_at=NOW,truncated=False)
    def test_visible_currency_preserved(self):
        r=self.parse(sample());c=r['listing']
        self.assertEqual((c['price'],c['currency']),('5750','USD'))
        self.assertEqual(r['summary']['price_relationship'],'different_display_currencies')
        self.assertIsNone(c['price_observations']['fx_rate']);self.assertIsNone(c['published_at'])
    def test_characteristics_and_no_generation_guess(self):
        c=self.parse(sample())['listing']
        self.assertEqual((c['brand'],c['model'],c['year'],c['mileage_km'],c['engine_cc']),('Skoda','Fabia',2012,192000,1400))
        self.assertIsNone(c['generation']);self.assertEqual(c['fuel'],'petrol');self.assertEqual(c['transmission'],'manual')
    def test_private_fields_not_exported(self):self.assertNotIn('PRIVATE_',json.dumps(self.parse(sample())))
    def test_same_currency_disagreement_explicit(self):
        self.assertEqual(self.parse(sample('USD',6000))['summary']['price_relationship'],'same_currency_conflict')
        self.assertEqual(self.parse(sample('USD',5750))['summary']['price_relationship'],'agree')
    def test_truncation_not_hidden(self):
        self.assertTrue(self.parse(sample().replace('</html>',''))['summary']['download_truncated'])
    def test_missing_price_no_jsonld_fallback(self):
        r=self.parse(sample().replace('5 750 $ Договірна','Ціну не вказано'))
        self.assertIsNone(r['listing']['price']);self.assertEqual(r['summary']['price_relationship'],'unavailable')
    def test_conflicting_parameters_become_unknown(self):
        r=self.parse(sample().replace('</html>','<p>Пробіг: 250 тис.км.</p></html>'))
        self.assertIsNone(r['listing']['mileage_km']);self.assertEqual(r['summary']['field_conflicts'],['mileage'])
    def test_missing_vehicle_rejected(self):
        with self.assertRaises(ValueError):self.parse('<html>Loading</html>')

    def test_explicit_breadcrumb_geography_keeps_source_names(self):
        c = self.parse(geography_sample([REGION, CITY]))['listing']
        self.assertEqual((c['region'], c['locality'], c['district']), ('Тернопільська область', 'Тернопіль', None))
        self.assertEqual(c['field_provenance']['region']['status'], 'observed')
        self.assertEqual(c['field_provenance']['locality']['observations'][0]['source'], 'visible_breadcrumbs')
        self.assertIsNone(c['generation'])
        self.assertIsNone(c['price_kind'])

    def test_district_is_separate_and_administrative_area_never_becomes_city(self):
        area = {'@type': 'AdministrativeArea', 'name': 'Центральний'}
        c = self.parse(geography_sample([REGION, CITY, DISTRICT], area=area))['listing']
        self.assertEqual((c['region'], c['locality'], c['district']), ('Тернопільська область', 'Тернопіль', 'Центральний'))
        self.assertEqual(c['observed_area_served']['corroborates'], ['district'])
        c = self.parse(geography_sample(area=area))['listing']
        self.assertIsNone(c['region'])
        self.assertIsNone(c['locality'])
        self.assertIsNone(c['district'])

    def test_no_city_or_province_guessed_from_missing_breadcrumbs(self):
        c = self.parse(geography_sample([CITY]))['listing']
        self.assertEqual(c['locality'], 'Тернопіль')
        self.assertIsNone(c['region'])
        c = self.parse(geography_sample(area={'@type': 'City', 'name': 'Тернопіль'}))['listing']
        self.assertIsNone(c['locality'])
        self.assertEqual(c['observed_area_served']['name'], 'Тернопіль')

    def test_conflicting_cities_and_regions_become_unknown(self):
        links = [REGION, CITY, ('Львівська область', '/uk/transport/legkovye-avtomobili/skoda/lv/'), ('Львів', '/uk/transport/legkovye-avtomobili/skoda/lvov/')]
        c = self.parse(geography_sample(links))['listing']
        self.assertIsNone(c['region'])
        self.assertIsNone(c['locality'])
        self.assertEqual(c['field_conflicts'], ['locality', 'region'])
        self.assertEqual(c['field_provenance']['locality']['status'], 'conflicting')

    def test_city_jsonld_contradiction_is_retained_as_conflict(self):
        c = self.parse(geography_sample([REGION, CITY], area={'@type': 'City', 'name': 'Львів'}))['listing']
        self.assertEqual(c['region'], 'Тернопільська область')
        self.assertIsNone(c['locality'])
        self.assertEqual(c['field_conflicts'], ['locality'])
        self.assertEqual(len(c['field_provenance']['locality']['observations']), 2)

    def test_breadcrumb_and_jsonld_city_can_only_corroborate(self):
        c = self.parse(geography_sample([CITY], area={'@type': 'City', 'name': 'Тернопіль'}))['listing']
        self.assertEqual(c['locality'], 'Тернопіль')
        self.assertEqual(c['observed_area_served']['corroborates'], ['locality'])

    def test_unclosed_or_unscoped_breadcrumbs_cannot_supply_geography(self):
        for html in (geography_sample([REGION, CITY], close=False), geography_sample([REGION, CITY], scoped=False)):
            c = self.parse(html)['listing']
            self.assertIsNone(c['region'])
            self.assertIsNone(c['locality'])

    def test_other_brand_external_link_and_unrecognized_query_are_not_locations(self):
        links = [('Львів', '/uk/transport/legkovye-avtomobili/bmw/lvov/'), ('Одеса', 'https://example.com/transport/legkovye-avtomobili/skoda/odessa/'), ('Київ', '/uk/transport/legkovye-avtomobili/skoda/kiev/?unknown=1')]
        c = self.parse(geography_sample(links))['listing']
        self.assertIsNone(c['locality'])
        self.assertIsNone(c['district'])

    def test_conflicting_districts_do_not_erase_the_explicit_city(self):
        other = ('Північний', '/uk/transport/legkovye-avtomobili/skoda/ternopol/?search%5Bdistrict_id%5D=126')
        c = self.parse(geography_sample([CITY, DISTRICT, other]))['listing']
        self.assertEqual(c['locality'], 'Тернопіль')
        self.assertIsNone(c['district'])
        self.assertEqual(c['field_conflicts'], ['district'])

    def test_detail_apply_and_restart_preserve_geography_provenance(self):
        with tempfile.TemporaryDirectory() as directory, patch('socket.socket', side_effect=AssertionError('No network')):
            path = Path(directory) / 'isolated-geography.sqlite'
            pipeline = Pipeline(path)
            html = geography_sample([REGION, CITY, DISTRICT]).encode()
            pipeline.apply_detail_snapshot(html, expected_id='123', expected_url='https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html', fetched_at=NOW, truncated=False)
            pipeline.db.close()
            pipeline = Pipeline(path)
            try:
                c = pipeline.cars()[0]
                self.assertEqual((c['region'], c['locality'], c['district']), ('Тернопільська область', 'Тернопіль', 'Центральний'))
                self.assertEqual(c['field_provenance']['district']['status'], 'observed')
                self.assertFalse(c['publication_verified'])
                self.assertIsNone(c['price_kind'])
            finally:
                pipeline.db.close()

    def test_reported_source_dates_persist_without_becoming_verified_publication(self):
        state = {'ad': {'ad': {'id': 123,
                 'createdTime': datetime.fromtimestamp(NOW - 1000, timezone.utc).isoformat(),
                 'lastRefreshTime': datetime.fromtimestamp(NOW - 10, timezone.utc).isoformat(),
                 'pushupTime': None, 'privateContact': 'PRIVATE_DATE_CONTACT'}}}
        script = '<script id="olx-init-config">window.__PRERENDERED_STATE__ = ' + json.dumps(json.dumps(state)) + ';</script>'
        html = geography_sample([REGION, CITY]).replace('</html>', script + '</html>').encode()
        with tempfile.TemporaryDirectory() as directory, patch('socket.socket', side_effect=AssertionError('No network')):
            path = Path(directory) / 'isolated-dates.sqlite'
            pipeline = Pipeline(path)
            result = pipeline.apply_detail_snapshot(html, expected_id='123', expected_url='https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html', fetched_at=NOW, truncated=False)
            self.assertEqual(result['summary']['source_date_fields'], ['createdTime', 'lastRefreshTime'])
            pipeline.db.close()
            pipeline = Pipeline(path)
            try:
                c = pipeline.cars()[0]
                observations = c['source_date_observations']
                self.assertTrue(observations['identity_matches'])
                self.assertEqual(observations['values']['createdTime']['epoch'], NOW - 1000)
                self.assertFalse(observations['publication_semantics_verified'])
                self.assertFalse(c['publication_verified'])
                for key in ('published_at', 'updated_at', 'bumped_at'):
                    self.assertIsNone(c[key])
                self.assertNotIn('PRIVATE_DATE_CONTACT', json.dumps(c))
            finally:
                pipeline.db.close()
