import json
from unittest import TestCase
from experiments.free_search.public_search_state import parse_search_state, SearchStateError


def fixture(cards=None, page='0'):
    card={'type':'AdvertisementCardTemplate','id':'Auto12345678','component':{'advertisementCard':{'data':{'id':12345678,'type':'Auto','price':{'USD':5000},'link':'https://auto.ria.com/uk/auto_test_car_12345678.html'}}}}
    d={'list':{'lists':{'items':{'page':page,'items': cards if cards is not None else [card]}}},'searchPage':{'searchResultData':{'searchString':'page=0&limit=20&republished_last=1','count':100}},'page':{'templates':{'copy':card}}}
    return '<script>window.__PINIA__ = '+json.dumps(d)+';</script>'

class SearchStateTests(TestCase):
    def test_primary_list_only_no_template_duplicates(self):
        r=parse_search_state(fixture());self.assertEqual(len(r['cards']),1)
        self.assertIsNone(r['cards'][0]['published_at']);self.assertFalse(r['coverage_proven'])
    def test_page_mismatch_fails(self):
        with self.assertRaises(SearchStateError):parse_search_state(fixture(page='1'))
    def test_ambiguous_state_fails(self):
        with self.assertRaises(SearchStateError):parse_search_state(fixture()*2)
    def test_javascript_is_not_evaluated(self):
        with self.assertRaises(SearchStateError):parse_search_state('<script>window.__PINIA__ = alert(1);</script>')
    def test_wrong_identity_is_rejected_not_successful_empty_coverage(self):
        r=parse_search_state(fixture().replace('auto_test_car_12345678','auto_test_car_87654321'))
        self.assertEqual(r['cards'],[]);self.assertEqual(len(r['rejected']),1)
    def test_empty_primary_list_ignores_template_clone(self):
        self.assertEqual(parse_search_state(fixture(cards=[]))['cards'],[])
    def test_oversize_rejected(self):
        with self.assertRaises(SearchStateError):parse_search_state('x'*2500001)

    def test_observed_hyphenated_model_slug_is_supported(self):
        r=parse_search_state(fixture().replace('auto_test_car_', 'auto_bmw_5-series_'))
        self.assertEqual(len(r['cards']),1);self.assertEqual(r['rejected'],[])
