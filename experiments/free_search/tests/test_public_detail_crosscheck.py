from copy import deepcopy
from unittest import TestCase
from experiments.free_search.tests.test_public_details import BASE,ID,html
from experiments.free_search.public_detail_crosscheck import crosscheck_details

class DetailCrosscheckTests(TestCase):
    def test_rendered_price_not_leasing_and_optional_engine_body(self):
        page=html(BASE)+'''<div id="basicInfoPrice"><strong>5 300.50 $</strong></div>
        <div id="basicInfoLeasing">від 100 $ на місяць</div>
        <div id="descCharacteristicsValue">Універсал • 5 дверей</div>
        <div id="descEngineEngine">Дизель, 2.0 л</div>'''
        r=crosscheck_details(page,ID)
        self.assertTrue(r['price_agrees']);self.assertEqual(r['engine_cc'],2000)
        self.assertEqual(r['body'],'Універсал');self.assertFalse(r['ready_for_delivery'])
    def test_conflicting_price_is_flagged(self):
        r=crosscheck_details(html(BASE)+'<div id="basicInfoPrice">100 $</div>',ID)
        self.assertFalse(r['price_agrees'])
    def test_duplicate_visible_price_not_merged(self):
        r=crosscheck_details(html(BASE)+'<div id="basicInfoPrice">5300.50 $</div>'*2,ID)
        self.assertIn('ambiguous_visible_basicInfoPrice',r['issues'])
    def test_script_and_hidden_price_cannot_pass(self):
        p=html(BASE)+'<script>"<div id=basicInfoPrice>5300.50 $</div>"</script><div hidden><div id="basicInfoPrice">5300.50 $</div></div>'
        self.assertFalse(crosscheck_details(p,ID)['price_agrees'])
    def test_hyphenated_identity_is_supported(self):
        p=html(BASE).replace('example_model','example_model-name')+'<div id="basicInfoPrice">5300.50 $</div>'
        self.assertTrue(crosscheck_details(p,ID)['price_agrees'])
    def test_electric_does_not_invent_displacement(self):
        p=html(BASE)+'<div id="descEngineEngine">Електро, 75 кВт</div>'
        self.assertIsNone(crosscheck_details(p,ID)['engine_cc'])
    def test_dom_kilometres_conflict_is_visible(self):
        p=html(BASE)+'<div id="basicInfoTableMainInfo0">180 тис. км</div>'
        self.assertIn('mileage_conflict',crosscheck_details(p,ID)['issues'])
