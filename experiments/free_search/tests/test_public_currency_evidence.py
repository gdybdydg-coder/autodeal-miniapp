from copy import deepcopy
import unittest
from experiments.free_search.public_currency_evidence import currency_evidence
from experiments.free_search.tests.test_public_details import BASE,ID,html

class CurrencyEvidenceTests(unittest.TestCase):
    def page(self, currency, price, visible):
        v=deepcopy(BASE);v['offers'].update(priceCurrency=currency,price=str(price))
        return html(v)+'<div id="basicInfoPrice">'+visible+'</div>'

    def test_uah_does_not_become_visible_dollar_amount(self):
        r=currency_evidence(self.page('UAH',22000,'489 $ • 22 000 грн •'),ID)
        self.assertTrue(r['same_currency_agrees']);self.assertEqual(r['same_currency_visible_amount'],'22000')
        self.assertFalse(r['fx_inferred']);self.assertFalse(r['default_detail_parser_changed'])

    def test_only_converted_usd_cannot_prove_uah(self):
        r=currency_evidence(self.page('UAH',22000,'489 $'),ID)
        self.assertFalse(r['same_currency_agrees']);self.assertIsNone(r['same_currency_visible_amount'])

    def test_eur_decimal_comma_matches_exactly(self):
        r=currency_evidence(self.page('EUR','5300.50','5 300,50 €'),ID)
        self.assertTrue(r['same_currency_agrees'])

    def test_conflicting_repeated_currency_is_unknown(self):
        r=currency_evidence(self.page('UAH',22000,'22 000 грн 23 000 грн'),ID)
        self.assertFalse(r['same_currency_agrees'])

    def test_hidden_price_or_duplicate_price_box_not_evidence(self):
        h=self.page('UAH',22000,'22 000 грн')
        for variant in [h.replace('<div id=', '<div hidden id='),h+'<div id="basicInfoPrice">22 000 грн</div>']:
            self.assertFalse(currency_evidence(variant,ID)['same_currency_agrees'])

    def test_wrong_listing_identity_fails(self):
        with self.assertRaises(ValueError):currency_evidence(self.page('UAH',22000,'22 000 грн'),'99999')
