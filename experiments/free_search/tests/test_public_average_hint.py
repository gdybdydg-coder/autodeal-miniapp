import json
from unittest import TestCase
from experiments.free_search.public_average_hint import extract_average_hint
from experiments.free_search.embedded_state import parse_embedded_state, EmbeddedStateError

ID='12345678'
def fixture(value=1234,auto_id=12345678,hidden=False):
    path='/uk/auto___12345678.html/'
    node={'id':'basicInfoPriceGhost','isHide':hidden,'action':'showBottomPopUp','actionData':{'autoId':auto_id,'blockId':'averagePrice','params':{'averagePrice':value},'data':[['averagePrice',str(value)]]}}
    d={'page':{'path':path,'structures':{path:{'templates':[node]}}}}
    return '<script>window.__PINIA__ = '+json.dumps(d)+';</script>'
class AverageHintTests(TestCase):
    def test_average_is_never_promoted_to_market_quote(self):
        r=extract_average_hint(fixture(),ID)
        self.assertEqual(r['average_raw'],1234)
        for k in ['market','currency','lower_bound','upper_bound','period_hours','sample_count']:
            self.assertIsNone(r[k])
        self.assertFalse(r['ready_for_delivery']);self.assertFalse(r['formula_equivalence_verified'])
    def test_wrong_action_id_fails(self):
        with self.assertRaises(ValueError):extract_average_hint(fixture(auto_id=999),ID)
    def test_wrong_page_id_fails(self):
        with self.assertRaises(ValueError):extract_average_hint(fixture(),'999')
    def test_hidden_hint_is_unavailable(self):
        self.assertEqual(extract_average_hint(fixture(hidden=True),ID)['status'],'unavailable')
    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(EmbeddedStateError):parse_embedded_state('<script>window.__PINIA__ = {"page":1,"page":2};</script>')
    def test_invalid_average_values(self):
        for value in [True,-1,0,float('nan'),float('inf')]:
            with self.subTest(value=value),self.assertRaises(ValueError):extract_average_hint(fixture(value),ID)
    def test_conflicting_embedded_copy_fails(self):
        with self.assertRaises(ValueError):extract_average_hint(fixture().replace('"1234"','"5678"'),ID)
