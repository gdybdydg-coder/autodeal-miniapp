"""Observed calculator layout, synthetic identities for delivery; no network."""
import json
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import test_owner_pilot as fixtures
import test_owner_delivery as delivery_fixtures
from experiments.free_search.owner_public_mean import browser_quote
from experiments.free_search.owner_pilot import prepare
from experiments.free_search.owner_delivery import send_one


def mean_capture(page, now=fixtures.NOW):
    return dict(listing_id=page.listing_id, listing_url=page.url,
        calculator_url='https://auto.ria.com/uk/price/average/?currentTab=byOmniId&omniId='+page.listing_id,
        input_id=page.listing_id, identity_text='Знайдено Example Model 2016 за ID оголошення '+page.listing_id,
        mean_text=['Середня ціна сформована на основі проданих 272 авто\n15\u00a0965 $'],
        period='3 місяці', observed_at=now-10)


def capture(page, now=fixtures.NOW):
    return dict(kind='public_market_range', listing_id=page.listing_id,
        visible_listing_id=page.listing_id, listing_url=page.url,
        lower_text=['від 15 167 $'], upper_text=['до 16 764 $'], observed_at=now-10)


class BrowserMeanTests(fixtures.TestCase):
    setUp = fixtures.OwnerPilotTests.setUp

    def test_observed_layout_parses_mean_not_popup_asking_price(self):
        result = browser_quote(mean_capture(self.subject), self.subject, fixtures.NOW)
        self.assertEqual(result['average_usd'], '15965')
        self.assertEqual(result['provider_sample_count'], 272)
        self.assertEqual(result['required_peers'], 0)

    def test_no_peers_and_old_numeric_selector_disabled(self):
        with patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED', False):
            result = prepare(self.engine,self.settings,self.snapshot,self.subject,[],[],fixtures.NOW,
                             browser_mean=capture(self.subject))
        self.assertEqual(result['status'], 'local_draft')
        self.assertIn('$15167', result['draft'])
        self.assertFalse(result['ready_for_delivery'])

    def test_all_ukraine_session_preserves_saved_percent_and_snapshot(self):
        page = fixtures.page(brand={'name':'Other'})
        args = (self.engine,self.settings,self.snapshot,page,[],[],fixtures.NOW)
        self.assertEqual(prepare(*args,browser_mean=capture(page))['reason'], 'owner_filter_mismatch')
        result = prepare(*args,browser_mean=capture(page),session_all_ukraine=True)
        self.assertEqual(result['status'], 'local_draft')
        self.assertEqual(result['valuation']['required_discount_percent'], '15')
        self.assertEqual(self.snapshot.filters['brand'], 'Example')

    def test_wrong_id_url_input_period_or_duplicate_cannot_enter(self):
        original = mean_capture(self.subject)
        changes = [{'listing_id':'999'}, {'input_id':'999'}, {'identity_text':'Знайдено інше авто'},
            {'listing_url':'https://example.org/'}, {'calculator_url':original['calculator_url']+'&x=1'},
            {'period':'Останній рік'}, {'mean_text':original['mean_text']*2}]
        for change in changes:
            with self.assertRaises(ValueError): browser_quote({**original,**change},self.subject,fixtures.NOW)

    def test_no_range_or_asking_price_substitution(self):
        for text in ('13 700 $','від 15 167 $ до 16 764 $',
                     'Середня ціна сформована на основі проданих 272 авто 15 965 грн',
                     'Середня ціна сформована на основі проданих 0 авто 15 965 $'):
            with self.assertRaises(ValueError):
                browser_quote({**mean_capture(self.subject),'mean_text':[text]},self.subject,fixtures.NOW)

    def test_future_stale_and_nonfinite_rejected(self):
        for timestamp in (fixtures.NOW+1, fixtures.NOW-301, float('nan'), True):
            with self.assertRaises(ValueError):
                browser_quote({**capture(self.subject),'observed_at':timestamp},self.subject,fixtures.NOW)


class BrowserMeanDeliveryTests(delivery_fixtures.OwnerDeliveryTests):
    # Re-run all inherited sender scenarios with browser evidence and no peers.
    def send(self, transport=None, **options):
        original = send_one
        def adapted(*args, **kwargs):
            kwargs['browser_mean'] = capture(self.subject)
            return original(*args, **kwargs)
        with patch.object(delivery_fixtures,'send_one',adapted):
            return super().send(transport, **options)

    def test_unverified_real_layout_never_claims_or_transports(self):
        # The browser source is independent of the obsolete HTML button layout.
        with patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED',False):
            self.assertEqual(self.send()['state'],'sent')
