import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from experiments.olx_offline.pipeline import Pipeline
from experiments.olx_offline.test_pipeline import raw, users, comps, NOW
from experiments.olx_offline.test_detail_snapshot import sample

URL='https://www.olx.ua/d/uk/obyavlenie/example-IDfixture.html'


def detail(description='Розмитнений, продаю цілим, не на розбір.'):
    html=sample().replace('"productionDate": "2012"', '"productionDate": "2012", "category": "https://www.olx.ua/uk/transport/legkovye-avtomobili/"')
    return html.replace('</html>', '<div data-testid="ad_description">'+description+'</div></html>').encode()


class DetailIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'details.sqlite'
        self.p=Pipeline(self.path)
        self.socket=patch('socket.socket',side_effect=AssertionError('No network'))
        self.socket.start()

    def tearDown(self):
        self.p.db.close();self.tmp.cleanup();self.socket.stop()

    def ingest(self, data=None, **kw):
        args=dict(expected_id='123',expected_url=URL,fetched_at=NOW,truncated=False)
        args.update(kw)
        return self.p.apply_detail_snapshot(detail() if data is None else data,**args)

    def test_provenance_and_decision_survive_restart_without_description(self):
        result=self.ingest(detail('Розмитнений. Продаю цілим. Контакт +380671234567.'))
        self.assertEqual(result['offer_review'],'allowed')
        self.p.db.close();self.p=Pipeline(self.path)
        c=self.p.cars()[0]
        self.assertEqual(c['eligibility_review']['status'],'allowed')
        self.assertTrue(c['detail_provenance']['description_complete'])
        self.assertNotIn('380671234567',json.dumps(c))
        self.assertNotIn('description',c)
        self.assertEqual(c['usd_price']['usd_amount'],'5750')

    def test_detail_does_not_establish_new_publication(self):
        self.assertEqual(self.ingest()['eligibility'],'unverified')
        self.assertEqual(self.p.cars(True),[])
        self.assertFalse(self.p.cars()[0]['publication_verified'])

    def test_existing_discovery_and_new_publication_are_preserved(self):
        self.p.collect(lambda _:dict(items=[raw('123',url=URL)],next=None),NOW,page_budget=1,row_budget=1)
        self.ingest(fetched_at=NOW+20)
        c=self.p.cars(True)[0]
        self.assertEqual(c['first_seen_at'],NOW)
        self.assertEqual(c['published_at'],NOW)
        self.assertTrue(c['publication_verified'])

    def test_old_baseline_stays_old_after_detail(self):
        self.p.collect(lambda _:dict(items=[raw('123',url=URL,published_at=NOW-100)],next=None),NOW,page_budget=1,row_budget=1)
        self.assertEqual(self.ingest()['eligibility'],'baseline')
        self.assertEqual(self.p.cars(True),[])

    def test_identity_mismatch_and_old_response_do_not_overwrite(self):
        self.ingest()
        before=self.p.cars()
        for kw in ({'expected_id':'456'},{'expected_url':URL.replace('example','other')},{'fetched_at':NOW-1}):
            with self.assertRaises(ValueError):self.ingest(**kw)
            self.assertEqual(self.p.cars(),before)

    def test_updated_forbidden_description_holds_pending_delivery(self):
        self.p.collect(lambda _:dict(items=[raw('123',url=URL)],next=None),NOW,page_budget=1,row_budget=1)
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],1)
        self.assertEqual(self.ingest(detail('Продається під розбір, по запчастинах.'))['offer_review'],'excluded')
        result=self.p.deliver_fake(lambda *_:self.fail('Forbidden car sent'),lambda _:True,now=NOW,olx_enabled=True)
        self.assertEqual(result['held'],1)
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],0)

    def test_arbitrary_precomputed_review_is_not_accepted(self):
        with self.assertRaises(ValueError):self.ingest({'eligibility_review':{'status':'allowed'}})
        self.assertEqual(self.p.cars(),[])

    def test_observed_customs_field_is_used_without_independent_verification(self):
        self.ingest(detail().replace(b'</html>', '<p>Розмитнена: Так</p></html>'.encode()))
        review=self.p.cars()[0]['eligibility_review']
        self.assertEqual(review['customs_status'],'cleared_declared')
        self.assertIn('customs_cleared',review['used_fields'])
        self.assertFalse(review['customs_independently_verified'])

    def test_uncleared_structured_field_and_conflicting_fields_cannot_pass(self):
        html=detail('Продаю цілий автомобіль.').replace(b'</html>', '<p>Розмитнена: Ні</p></html>'.encode())
        self.assertEqual(self.ingest(html)['offer_review'],'excluded')
        html=html.replace(b'</html>', '<p>Розмитнена: Так</p></html>'.encode())
        self.assertEqual(self.ingest(html)['offer_review'],'needs_review')
