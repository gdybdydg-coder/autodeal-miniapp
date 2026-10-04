"""Detail/search cache ordering with isolated SQLite and no network.

HTML fixtures reconstruct observed selectors. The explicitly enriched valuation
fixture is synthetic; no real full-price proof or live recommendation is claimed.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.olx_offline.pipeline import Pipeline, canonical, estimate, fingerprint
from experiments.olx_offline.test_detail_ingest import detail, URL
from experiments.olx_offline.test_pipeline import NOW, raw, users


class SearchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'research.sqlite'
        self.p = Pipeline(self.path)
        self.net = patch('socket.socket', side_effect=AssertionError('No network'))
        self.net.start()

    def tearDown(self):
        self.p.db.close()
        self.tmp.cleanup()
        self.net.stop()

    def ingest(self, at=NOW, data=None):
        return self.p.apply_detail_snapshot(detail() if data is None else data,
            expected_id='123', expected_url=URL, fetched_at=at, truncated=False)

    def thin(self, at=NOW+10, **changes):
        row = dict(id='123', source='olx', url=URL, title='Example',
                   price='5750', currency='USD', price_kind=None, category=None,
                   publication_verified=False, checked_at=at,
                   evidence='observed_search_html', photos=[])
        row.update(changes)
        return row

    def collect(self, row, now=None):
        return self.p.collect(lambda _: dict(items=[row], next=None),
                              max(NOW, row['checked_at']) if now is None else now,
                              page_budget=1, row_budget=1)

    def current(self):
        return self.p.cars()[0]

    def restart(self):
        self.p.db.close()
        self.p = Pipeline(self.path)

    def seed_new_and_valued(self):
        self.collect(raw('123', url=URL, checked_at=NOW), NOW)
        self.ingest()
        car = self.current()
        # Synthetic explicit proof solely to test pending-delivery invalidation.
        # The HTML parser cannot produce these full-price/comparison claims.
        car.update(price_kind='full', generation='II', condition=None)
        car['price_review'] = dict(status='structured_full_price', reasons=[],
                                   version='synthetic-explicit-fixture')
        car['issues'] = [x for x in car['issues'] if x != 'full_price_unconfirmed']
        self.p.db.execute('UPDATE listings SET payload=? WHERE source=? AND id=?',
                          (json.dumps(car), 'olx', '123'))
        self.p.db.commit()
        self.comps = [canonical(raw('c'+str(i), price=9800+i*50, brand='Skoda',
            model='Fabia', generation='II', year=2012, mileage_km=192000,
            condition=None), NOW) for i in range(12)]
        self.assertEqual(self.p.enqueue(users(), self.comps, NOW, capacity=5,
                                      olx_enabled=True)['queued'], 1)

    def test_unchanged_thin_refresh_preserves_detail_and_restart(self):
        self.ingest()
        before = self.current()
        self.collect(self.thin())
        after = self.current()
        for key in ('brand', 'model', 'body', 'engine_cc', 'mileage_km', 'fuel',
                    'transmission', 'photos', 'first_seen_at', 'checked_at',
                    'eligibility_review', 'price_review', 'detail_provenance',
                    'price_observations', 'publication_verified', 'published_at'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['search_provenance']['observed_at'], NOW+10)
        self.assertNotIn('detail_refresh_required', after)
        self.assertEqual(fingerprint(after), fingerprint(before))
        self.restart()
        self.assertEqual(self.current(), after)
        self.assertEqual(self.p.cars(True), [])

    def test_unchanged_refresh_keeps_pending_proof_and_does_not_refresh_detail_clock(self):
        self.seed_new_and_valued()
        before = self.current()
        self.collect(self.thin())
        self.assertEqual(self.current()['checked_at'], NOW)
        self.assertEqual(fingerprint(self.current()), fingerprint(before))
        self.assertEqual(self.p.assessment_summary()['changed_since_assessment'], 0)
        self.assertEqual(self.p.deliver_fake(lambda *_: True, lambda _: True,
            now=NOW+10, olx_enabled=True)['accepted'], 1)

    def test_search_does_not_extend_expired_detail_valuation(self):
        self.seed_new_and_valued()
        self.collect(self.thin(at=NOW+31*86400))
        self.assertEqual(self.current()['checked_at'], NOW)
        result = estimate(self.current(), self.comps, NOW+31*86400)
        self.assertEqual(result['reason'], 'stale_or_future_observation')

    def test_same_currency_price_change_invalidates_pending_and_assessment(self):
        self.seed_new_and_valued()
        old = self.current()
        self.collect(self.thin(price='6000'))
        car = self.current()
        self.assertEqual((car['price'], car['currency']), ('5750', 'USD'))
        self.assertEqual(car['usd_price'], old['usd_price'])
        self.assertEqual(car['search_provenance']['observation']['price'], '6000')
        self.assertIn('search_price_changed', car['detail_refresh_reasons'])
        self.assertEqual(estimate(car, self.comps, NOW+10)['reason'], 'detail_refresh_required')
        self.assertEqual(self.p.assessment_summary()['changed_since_assessment'], 1)
        self.assertEqual(self.p.db.execute('SELECT status FROM deliveries').fetchone()[0],
                         'needs_revalidation')
        self.assertEqual(self.p.enqueue(users(), self.comps, NOW+10, capacity=5,
                                       olx_enabled=True)['queued'], 0)
        self.assertEqual(self.p.deliver_fake(lambda *_: self.fail('Stale price sent'),
            lambda _: True, now=NOW+10, olx_enabled=True)['accepted'], 0)
        self.restart()
        self.assertTrue(self.current()['detail_refresh_required'])

    def test_cross_currency_display_neither_overwrites_nor_infers_fx(self):
        self.ingest()
        before = self.current()
        self.collect(self.thin(price='258685', currency='UAH'))
        car = self.current()
        self.assertEqual((car['price'], car['currency']), ('5750', 'USD'))
        self.assertEqual(car['usd_price'], before['usd_price'])
        self.assertIsNone(car['usd_price']['fx'])
        self.assertIn('search_display_currency_differs', car['detail_refresh_reasons'])
        self.assertEqual(car['price_review']['status'], 'needs_review')
        self.assertEqual(car['search_provenance']['observation']['currency'], 'UAH')

    def test_unknown_and_baseline_publications_are_not_promoted_by_search_or_bump(self):
        self.ingest()
        self.collect(self.thin(published_at=NOW+10, publication_verified=True,
                               bumped_at=NOW+10))
        self.assertFalse(self.current()['publication_verified'])
        self.assertIsNone(self.current()['published_at'])
        self.assertEqual(self.p.cars(True), [])
        self.collect(raw('123', url=URL, published_at=NOW-100, checked_at=NOW+20), NOW+20)
        self.ingest(at=NOW+20)
        self.collect(self.thin(at=NOW+30, published_at=NOW+30,
                               publication_verified=True, bumped_at=NOW+30))
        self.assertEqual(self.current()['published_at'], NOW-100)
        self.assertEqual(self.p.cars(True), [])

    def test_out_of_order_search_cannot_replace_newer_detail_or_search(self):
        self.ingest(at=NOW+10)
        before = self.current()
        self.collect(self.thin(at=NOW+5, price='1'), now=NOW+20)
        self.assertEqual(self.current(), before)
        self.collect(self.thin(at=NOW+20))
        before = self.current()
        self.collect(self.thin(at=NOW+15, price='1'), now=NOW+25)
        self.assertEqual(self.current(), before)

    def test_older_detail_cannot_clear_newer_search_refresh_requirement(self):
        self.ingest()
        self.collect(self.thin(price='6000'))
        before = self.current()
        with self.assertRaises(ValueError):
            self.ingest(at=NOW+5)
        self.assertEqual(self.current(), before)
        self.ingest(at=NOW+11)
        car = self.current()
        self.assertNotIn('detail_refresh_required', car)
        self.assertEqual(car['search_provenance'], before['search_provenance'])
        self.assertEqual(car['price_review']['reasons'], ['full_price_unconfirmed'])

    def test_another_matching_thin_search_cannot_clear_a_required_detail_refresh(self):
        self.ingest()
        self.collect(self.thin(price='6000'))
        self.collect(self.thin(at=NOW+20))
        self.assertTrue(self.current()['detail_refresh_required'])
        self.assertIn('search_price_changed', self.current()['detail_refresh_reasons'])
        self.assertEqual(self.current()['detail_provenance']['fetched_at'], NOW)

    def test_unknown_price_and_conflicting_known_characteristic_require_refresh(self):
        self.ingest()
        self.collect(self.thin(price=None, currency=None, year=2013))
        self.assertTrue(self.current()['detail_refresh_required'])
        self.assertIn('search_price_unavailable', self.current()['detail_refresh_reasons'])
        self.assertIn('search_year_changed', self.current()['detail_refresh_reasons'])
        self.assertEqual(self.current()['year'], 2012)

    def test_search_cannot_heal_incomplete_detail_or_conflicting_same_time_observations(self):
        self.ingest(data=detail().replace(b'</div></html>', b''))
        self.assertEqual(self.current()['eligibility_review']['status'], 'needs_review')
        self.collect(self.thin())
        self.assertEqual(self.current()['eligibility_review']['status'], 'needs_review')
        self.collect(self.thin(price='6000'))
        self.assertIn('same_time_conflicting_search_observations',
                      self.current()['detail_refresh_reasons'])

    def test_future_search_is_rejected_and_accepted_deliveries_remain_accepted(self):
        self.seed_new_and_valued()
        self.p.deliver_fake(lambda *_: True, lambda _: True, now=NOW, olx_enabled=True)
        before = self.current()
        result = self.collect(self.thin(at=NOW+20), now=NOW+10)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(self.current(), before)
        self.collect(self.thin(at=NOW+100, price='6000'), now=NOW+100)
        self.assertEqual(self.p.db.execute('SELECT status FROM deliveries').fetchone()[0], 'accepted')

    def test_generic_fixture_refresh_still_updates_without_html_merge(self):
        self.ingest()
        self.collect(raw('123', url=URL, price=1234, checked_at=NOW+10), NOW+10)
        self.assertEqual(self.current()['price'], '1234')
        self.assertEqual(self.current()['evidence'], 'synthetic')
        self.assertNotIn('detail_refresh_required', self.current())

    def test_observed_placement_and_location_date_do_not_change_valuation_proof(self):
        self.ingest()
        before = fingerprint(self.current())
        self.collect(self.thin(observed_search_reason='promoted',
                               observed_location_date='Київ — Сьогодні о 12:00'))
        observation = self.current()['search_provenance']['observation']
        self.assertEqual(observation['observed_search_reason'], 'promoted')
        self.assertEqual(observation['observed_location_date'], 'Київ — Сьогодні о 12:00')
        # An advertising placement change in the same second is not a conflict
        # in asking price or vehicle attributes and cannot create a publication.
        self.collect(self.thin(observed_search_reason='organic',
                               observed_location_date='Київ — Сьогодні о 12:01'))
        self.assertNotIn('detail_refresh_required', self.current())
        self.assertEqual(fingerprint(self.current()), before)
        self.assertFalse(self.current()['publication_verified'])

    def test_canonical_keeps_allowlisted_observations_without_interpreting_ui_dates(self):
        car = canonical(self.thin(observed_search_reason='promoted',
            observed_location_date='Київ — Сьогодні о 12:00'), NOW+10)
        self.assertEqual(car['observed_search_reason'], 'promoted')
        self.assertEqual(car['observed_location_date'], 'Київ — Сьогодні о 12:00')
        self.assertIsNone(car['published_at'])
        self.assertFalse(car['publication_verified'])
        car = canonical(self.thin(observed_search_reason='unverified-ad-label'), NOW+10)
        self.assertIsNone(car['observed_search_reason'])
