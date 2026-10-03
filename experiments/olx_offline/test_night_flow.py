"""Integrated owner requirements; isolated SQLite, fictional FX, no network."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from experiments.olx_offline.fx import FXQuote, instant, KYIV, nbu_url
from experiments.olx_offline.pipeline import Pipeline, canonical, estimate, filtered, message
from experiments.olx_offline.test_pipeline import NOW, raw, comps, users
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.test_detail_snapshot import sample


def quote(now=NOW, rate='42'):
    day=instant(now).astimezone(KYIV).date()
    return FXQuote(Decimal(rate),day,instant(now),nbu_url(day))


class NightFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'flow.sqlite'
        self.p=Pipeline(self.path)
        self.net=patch('socket.socket',side_effect=AssertionError('Network forbidden'))
        self.net.start()

    def tearDown(self):
        self.p.db.close();self.tmp.cleanup();self.net.stop()

    def collect(self, rows, fx=None):
        return self.p.collect(lambda _:dict(items=rows,next=None),NOW,page_budget=1,row_budget=100,fx_quote=fx)

    def test_forbidden_and_missing_description_never_reach_delivery(self):
        rows=[raw('ok'),raw('uncleared',customs_status='uncleared',description='Нерозмитнений, ціна без мита.'),
              raw('donor',sale_mode='donor',description='Продається під розбір / по запчастинах.'),
              raw('missing',description=None,description_available=False)]
        self.collect(rows)
        self.assertEqual(len(self.p.cars()),4)
        self.assertEqual(self.p.enqueue(users(2),comps(),NOW,capacity=20,olx_enabled=True)['queued'],2)
        sent=[]
        def sender(uid,msg):sent.append((uid,msg));return True
        self.assertEqual(self.p.deliver_fake(sender,lambda _:True,now=NOW,olx_enabled=True)['accepted'],2)
        self.assertEqual({r[0] for r in self.p.db.execute("SELECT id FROM deliveries WHERE status='accepted'")},{'ok'})
        self.assertEqual(len({uid for uid,_ in sent}),2)

    def test_changed_description_invalidates_queued_message(self):
        self.collect([raw('x')]);self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)
        self.collect([raw('x',description='Продаж під розбір, тільки по запчастинах.')])
        result=self.p.deliver_fake(lambda *_:self.fail('Forbidden update sent'),lambda _:True,now=NOW,olx_enabled=True)
        self.assertEqual(result['held'],1)
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],0)

    def test_uah_exact_normalization_before_budget_and_market(self):
        c=canonical(raw('x',price='210 000',currency='UAH'),NOW,fx_quote=quote())
        self.assertEqual(c['usd_price']['usd_amount'],'5000')
        self.assertEqual((c['price'],c['currency']),('210000','UAH'))
        self.assertTrue(filtered(c,{'price_max':5000,'currency':'USD'}))
        self.assertFalse(filtered(c,{'price_max':'4999.999999','currency':'USD'}))
        self.assertEqual(estimate(c,comps(),NOW)['status'],'experimental_estimate')
        self.assertIn('≈ 5 000 $',message(c)['text'])

    def test_fx_missing_is_retained_and_cannot_be_delivered(self):
        self.collect([raw('x',price=210000,currency='UAH')])
        self.assertEqual(self.p.cars()[0]['usd_price']['reason'],'fx_missing')
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],0)
        self.collect([raw('x',price=210000,currency='UAH')],quote())
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],1)

    def test_fx_change_is_not_a_second_publication_or_delivery(self):
        self.collect([raw('x',price=210000,currency='UAH')],quote())
        self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)
        self.p.deliver_fake(lambda *_:True,lambda _:True,now=NOW,olx_enabled=True)
        self.collect([raw('x',price=210000,currency='UAH')],quote(rate='43'))
        self.assertEqual(len(self.p.cars(True)),1)
        self.assertEqual(self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)['queued'],0)

    def test_currency_unknown_not_guessed(self):
        c=canonical(raw('x',currency='BTC'),NOW)
        self.assertEqual(c['usd_price']['reason'],'currency_unsupported')
        self.assertEqual(estimate(c,comps(),NOW)['status'],'profitability_unconfirmed')

    def test_legacy_proof_cannot_bypass_new_review(self):
        self.collect([raw('x')]);self.p.enqueue(users(),comps(),NOW,capacity=5,olx_enabled=True)
        c=self.p.cars()[0];c.pop('eligibility_review')
        self.p.db.execute('UPDATE listings SET payload=?',(json.dumps(c),));self.p.db.commit()
        self.assertEqual(self.p.deliver_fake(lambda *_:self.fail(),lambda _:True,now=NOW,olx_enabled=True)['held'],1)

    def test_full_description_review_omits_private_content(self):
        html=sample().replace('</html>','<div data-testid="ad_description">Нерозмитнений. Телефон +380671234567</div></html>')
        result=parse_detail_snapshot(html.encode(),fetched_at=NOW,truncated=False)
        self.assertEqual(result['listing']['eligibility_review']['status'],'excluded')
        self.assertNotIn('380671234567',json.dumps(result))

    def test_incomplete_description_cannot_be_used(self):
        html=sample().replace('</html>','<div data-testid="ad_description">Розмитнений, цілий автомобіль')
        result=parse_detail_snapshot(html.encode(),fetched_at=NOW,truncated=True)
        self.assertEqual(result['listing']['eligibility_review']['status'],'needs_review')
        self.assertFalse(result['listing']['eligibility_review']['description_complete'])
