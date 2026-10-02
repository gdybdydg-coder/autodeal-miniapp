import json
from pathlib import Path
import unittest
from experiments.olx_offline.html_snapshot import parse_search_snapshot,money
from experiments.olx_offline.pipeline import canonical,estimate
from experiments.olx_offline.test_pipeline import NOW

# Reconstructed minimal markup using observed attributes; synthetic card values.
CARD='''<div data-testid="l-card" id="123"><a data-testid="card-title-link" href="/d/uk/obyavlenie/example-IDfixture.html?tracking=test">Example</a><p data-testid="ad-price">5 750 $ Договірна</p><p data-testid="location-date">Київ - Сьогодні о 12:00</p><span>2012  192 тис.км.</span><span>1.40 л.</span><span>Бензин</span><span>Механічна</span><img src="data:image/png;base64,abc"></div>'''

class HtmlSnapshotTests(unittest.TestCase):
    def parse(self,body):return parse_search_snapshot(body.encode(),fetched_at=NOW,truncated=False)
    def test_observed_markup_contract(self):
        r=self.parse('<html>'+CARD+'</html>');c=r['listings'][0]
        self.assertEqual(c['year'],2012);self.assertEqual(c['mileage_km'],192000);self.assertEqual(c['engine_cc'],1400)
        self.assertEqual(c['fuel'],'petrol');self.assertEqual(c['transmission'],'manual')
        self.assertEqual(c['currency'],'USD');self.assertEqual(c['price'],'5750');self.assertEqual(c['photos'],[])
        self.assertIsNone(c['published_at']);self.assertFalse(c['publication_verified']);self.assertFalse(r['summary']['ready_for_delivery'])
    def test_truncation_keeps_complete_cards_only(self):
        r=self.parse('<html>'+CARD+'<div data-testid="l-card" id="456">')
        self.assertEqual(r['summary']['unique_cards'],1);self.assertTrue(r['summary']['download_truncated'])
    def test_jsonld_subset_not_full_coverage(self):
        ld={'@type':'Product','offers':{'offers':[]}}
        r=self.parse('<html>'+CARD+'<script type="application/ld+json">'+json.dumps(ld)+'</script></html>')
        self.assertEqual(r['summary']['cards_absent_from_jsonld'],1);self.assertFalse(r['summary']['collection_complete'])
    def test_photo_association_by_url(self):
        ld={'@type':'Product','offers':{'offers':[{'url':'https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html','image':['https://ireland.apollo.olxcdn.com/example.jpg']} ]}}
        r=self.parse('<html>'+CARD+'<script type="application/ld+json">'+json.dumps(ld)+'</script></html>')
        self.assertEqual(r['summary']['cards_with_photo_urls'],1)
    def test_duplicates_not_double_counted(self):
        r=self.parse('<html>'+CARD+CARD+'</html>');self.assertEqual(r['summary']['unique_cards'],1);self.assertEqual(r['summary']['duplicate_cards'],1)
    def test_display_currency_not_guessed(self):
        self.assertEqual(money('258 685.54 грн.Договірна'),(None,None))
        self.assertEqual(money('258 685.54 грн. Договірна'),('258685.54','UAH'))
        self.assertEqual(money('5 750 €'),('5750','EUR'));self.assertEqual(money('5000'),(None,None))
    def test_saved_real_facts_remain_unconfirmed(self):
        rows=json.loads((Path(__file__).parent/'fixtures/observed-detail-facts-20261002.json').read_text())['records']
        cards=[canonical(row,NOW) for row in rows]
        self.assertEqual([(c['price'],c['currency']) for c in cards],[('68000','UAH'),('5750','USD')])
        for c in cards:
            self.assertFalse(c['publication_verified']);self.assertEqual(estimate(c,[],NOW)['status'],'profitability_unconfirmed')
    def test_unsafe_link_excluded(self):
        r=self.parse(CARD.replace('/d/uk/obyavlenie/example-IDfixture.html?tracking=test','https://evil.invalid/x'))
        self.assertEqual(r['summary']['rejected_cards'],1)
