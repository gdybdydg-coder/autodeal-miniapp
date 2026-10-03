import tempfile
import unittest
from pathlib import Path
from experiments.olx_offline.price_review import review
from experiments.olx_offline.pipeline import canonical,estimate,Pipeline
from experiments.olx_offline.test_pipeline import NOW,raw,comps,users

class PriceReviewTests(unittest.TestCase):
    def test_deposit_overrides_full_flag(self):
        for text in ('Аванс 7000 USD','Перший внесок 7000','Задаток 7000'):
            c=canonical(raw(1,price_context=text),NOW)
            self.assertEqual(estimate(c,comps(),NOW)['reason'],'price_evidence_conflict')
    def test_monthly_conditional_part(self):
        for text in ('7000 / міс','Щомісячний платіж 7000','Ціна умовна','Ціна за двигун'):
            self.assertEqual(review(raw(1,price_context=text))['status'],'needs_review')
    def test_text_cannot_confirm_full(self):
        self.assertEqual(review(raw(1,price_kind=None,price_context='Ціна за весь автомобіль'))['status'],'needs_review')
    def test_whole_car_for_parts_held_under_new_owner_rule(self):
        c=canonical(raw(1,title='Цілий автомобіль на запчастини',condition='damaged',price_context='Ціна за весь автомобіль'),NOW)
        self.assertNotEqual(c['eligibility_review']['status'],'allowed')
        self.assertEqual(estimate(c,comps(),NOW)['status'],'profitability_unconfirmed')
    def test_low_price_not_fraud(self):
        c=canonical(raw(1,price=200),NOW)
        self.assertNotIn('fraud',str(c));self.assertEqual(c['price'],'200')
    def test_bad_comparable_price_not_used(self):
        records=comps()
        for c in records:c['price_review']={'status':'needs_review'}
        self.assertEqual(estimate(canonical(raw(1),NOW),records,NOW)['sample'],0)
    def test_malformed_optional_fields_are_unknown(self):
        c=canonical(raw(1,generation={},body=5,engine_cc=True,checked_at='today'),NOW)
        for k in ('generation','body','engine_cc','checked_at'):self.assertIsNone(c[k])
    def test_context_bound_and_type(self):
        for value in ({},'x'*4001):self.assertEqual(review(raw(1,price_context=value))['status'],'needs_review')
    def test_unknown_persisted_and_change_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'offline.sqlite';p=Pipeline(path)
            def collect(r):p.collect(lambda _:dict(items=[r],next=None),NOW,page_budget=1,row_budget=10)
            collect(raw(1,price_context='Аванс 7000'))
            self.assertEqual(p.enqueue(users(),comps(),NOW,capacity=10,olx_enabled=True)['queued'],0)
            p.db.close();p=Pipeline(path)
            self.assertEqual(p.assessment_summary()['reasons'],{'price_evidence_conflict':1})
            collect(raw(1));self.assertEqual(p.assessment_summary()['changed_since_assessment'],1)
            p.enqueue(users(),comps(),NOW,capacity=10,olx_enabled=True)
            self.assertEqual(p.assessment_summary()['experimental'],1);p.db.close()
