from unittest import TestCase
from experiments.free_search.owner_price_rule import evaluate
from experiments.free_search.owner_public_mean import browser_quote
import test_owner_pilot as fixtures
from test_owner_browser_mean import capture


class PriceRuleTests(TestCase):
    def test_reported_false_positive_and_exact_boundary(self):
        result = evaluate('5776', '4950', '10')
        self.assertEqual(result['maximum_price_usd'], '4938.480')
        self.assertFalse(result['eligible'])
        self.assertTrue(evaluate('5776', '4938.48', '10')['eligible'])
        self.assertFalse(evaluate('5776', '4938.4801', '10')['eligible'])

    def test_saved_percentage_is_applied_sequentially(self):
        self.assertEqual(evaluate('5776', '1', '15')['maximum_price_usd'], '4664.1200')
        self.assertEqual(evaluate('5776', '1', '0')['maximum_price_usd'], '5487.20')
        for value in ('NaN', 'Infinity', '-1', '100'):
            with self.assertRaises(ValueError): evaluate('5776', '4950', value)

    def test_range_identity_freshness_currency_and_ambiguity(self):
        page = fixtures.page()
        good = capture(page)
        self.assertEqual(browser_quote(good, page, fixtures.NOW)['lower_usd'], '15167')
        for bad in ({'visible_listing_id':'999'}, {'listing_url':'https://example.com'},
                    {'observed_at':fixtures.NOW-301}, {'observed_at':True},
                    {'lower_text':[]}, {'lower_text':['від 15 167 грн']},
                    {'lower_text':['від 15 167 $']*2}, {'upper_text':['до 1 $']}):
            with self.assertRaises(ValueError): browser_quote({**good, **bad}, page, fixtures.NOW)
