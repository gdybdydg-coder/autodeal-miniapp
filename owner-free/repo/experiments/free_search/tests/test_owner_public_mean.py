"""All positive numeric layouts are synthetic, not proof of live UI access."""
from dataclasses import replace
from unittest.mock import patch
import test_owner_pilot as fixtures
from experiments.free_search.owner_public_mean import quote
from experiments.free_search.owner_pilot import prepare
from experiments.free_search.owner_readiness import assess


class PublicMeanTests(fixtures.TestCase):
    setUp = fixtures.OwnerPilotTests.setUp

    def result(self, subject=None, now=fixtures.NOW):
        return prepare(self.engine, self.settings, self.snapshot,
                       subject or self.subject, [], [], now)

    def test_live_label_only_is_not_numeric_price(self):
        page = replace(self.subject, html=self.subject.html.replace(
            'Середня ціна: 10 500 $', 'Середня ціна'))
        self.assertEqual(self.result(page)['reason'], 'public_mean_numeric_value_unavailable')

    def test_unverified_layout_cannot_authorize_send(self):
        self.assertEqual(self.result()['reason'], 'public_mean_numeric_layout_unverified')

    def test_synthetic_mean_works_without_any_peers_and_without_adjustment(self):
        with patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED', True):
            r = self.result()
        self.assertEqual(r['reason'], 'public_lower_bound_unverified')

    def test_other_price_nodes_never_substituted(self):
        for text in ('', 'Нижче ринку', 'Середня ціна: 10 500 грн',
                     'Середня ціна: 9 000–11 000 $', 'Середня ціна: 0 $'):
            page = replace(self.subject, html=self.subject.html.replace('Середня ціна: 10 500 $', text))
            with self.assertRaises(ValueError): quote(page, fixtures.NOW)

    def test_hidden_or_duplicate_quote_rejected(self):
        html = self.subject.html
        pages = [html.replace('id="basicInfoPriceGhost"', 'hidden id="basicInfoPriceGhost"'),
            html + '<div id="basicInfoPriceGhost">Середня ціна: 10500 $</div>']
        for content in pages:
            with self.assertRaises(ValueError): quote(replace(self.subject, html=content), fixtures.NOW)

    def test_stale_future_wrong_listing_and_domain_rejected(self):
        for page in (replace(self.subject, received_at=fixtures.NOW-301),
                     replace(self.subject, received_at=fixtures.NOW+1),
                     replace(self.subject, listing_id='87654321'),
                     replace(self.subject, url=self.subject.url.replace('auto.ria.com','example.com'))):
            with self.assertRaises(ValueError): quote(page, fixtures.NOW)

    def test_owner_stop_and_threshold_still_enforced(self):
        from sqlalchemy.orm import Session
        from backend.models import User
        with Session(self.engine) as db, db.begin(): db.get(User, fixtures.OWNER).ready = False
        self.assertEqual(self.result()['reason'], 'owner_stopped')

    def test_no_peer_blocker_in_current_readiness(self):
        r = assess({}, [], {}, fixtures.NOW)
        self.assertEqual(r['required_peers'], 0)
        self.assertFalse(any('peer' in x for x in r['blockers']))
        self.assertFalse(r['authorization'])

    def test_direct_mean_does_not_pass_below_saved_discount(self):
        page = replace(self.subject, html=self.subject.html.replace('10 500 $', '8 500 $'))
        with patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED', True):
            self.assertEqual(self.result(page)['reason'], 'public_lower_bound_unverified')
