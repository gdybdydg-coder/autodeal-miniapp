import json
import unittest
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.test_pipeline import NOW


def sample(currency='UAH',amount=258685):
    v={'@type':'Vehicle','sku':'123','url':'https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html','name':'Example',
       'brand':'Skoda','model':'Fabia','productionDate':'2012','offers':{'price':amount,'priceCurrency':currency},
       'vehicleIdentificationNumber':'PRIVATE_VIN_SENTINEL','description':'PRIVATE_DESCRIPTION_SENTINEL',
       'image':['https://ireland.apollo.olxcdn.com/example.jpg']}
    return '<html><script type="application/ld+json">'+json.dumps(v)+'</script><div data-testid="ad-price-container">5 750 $ Договірна</div><p>Пробіг: 192 тис.км.</p><p>Об\'єм двигуна: 1.40 л.</p><p>Вид палива: Бензин</p><p>Коробка передач: Механічна</p><p>Тип кузова: Хетчбек</p><div data-testid="ad-posted-at">Опубліковано сьогодні о 17:34</div><p>Продавець: PRIVATE_SELLER_SENTINEL</p></html>'

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
