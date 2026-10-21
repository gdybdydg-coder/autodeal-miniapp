import unittest
from experiments.free_search.owner_scope_links import inspect_links

BASE = 'https://auto.ria.com/uk/car/audi/a6/year/2015/'


class ScopeLinksTests(unittest.TestCase):
    def test_explicit_first_page_is_observed_not_synthesized(self):
        from experiments.free_search.owner_budget import public_url, Denied
        url='https://auto.ria.com/uk/last/hour/?page=1'
        self.assertIn(url, inspect_links('<a href="?page=1">1</a>',
                      'https://auto.ria.com/uk/last/hour/?page=2')['observed_catalogue_links'])
        public_url(url)
        for suffix in ('page=01','page=1&custom=1','page=1#x'):
            with self.assertRaises(Denied):
                public_url('https://auto.ria.com/uk/last/hour/?'+suffix)

    def test_source_links_only_and_no_query_credentials(self):
        html = '<a href="/uk/car/audi/a6/body/universal/">wagon</a>'
        html += '<a href="https://evil.invalid/uk/car/audi/a6/">bad</a>'
        html += '<a href="/uk/car/audi/a6/?api_key=secret">bad</a>'
        result = inspect_links(html, BASE)
        self.assertEqual(result['observed_catalogue_links'],
                         ['https://auto.ria.com/uk/car/audi/a6/body/universal/'])
        self.assertFalse(result['automatic_fetch_authorized'])

    def test_missing_defaults_remain_missing_and_sensitive_fields_omitted(self):
        result = inspect_links('<script data-search-query="page=0&top=1&api_key=secret"></script>',BASE)
        fields = result['search_metadata'][0]
        self.assertEqual(fields['fields'], {'page':'0','top':'1'})
        self.assertEqual(fields['omitted_field_count'],1)
        self.assertNotIn('secret',str(result))

    def test_duplicate_parameters_flagged(self):
        result = inspect_links('<script data-search_query="page=0&page=1"></script>',BASE)
        self.assertTrue(result['search_metadata'][0]['duplicate_fields'])
