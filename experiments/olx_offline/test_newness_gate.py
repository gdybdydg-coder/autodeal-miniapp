"""Reported old creation/refreshed today cannot bypass fresh-delivery gates.

All markup, clients and queues are synthetic isolated fixtures. Dates are parsed
through the real offline detail boundary, never supplied as an allowed decision.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.olx_offline.pipeline import Pipeline, canonical, estimate, fingerprint
from experiments.olx_offline.test_detail_ingest import detail, URL
from experiments.olx_offline.test_pipeline import NOW, raw, comps, users


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def dated_detail(*, created=NOW-1000, refreshed=NOW+10, source_id=123, **changes):
    ad = dict(id=source_id, createdTime=iso(created), lastRefreshTime=iso(refreshed),
              pushupTime=iso(refreshed), validToTime=iso(NOW+86400))
    ad.update(changes)
    script = '<script id="olx-init-config">window.__PRERENDERED_STATE__ = '
    script += json.dumps(json.dumps({'ad': {'ad': ad}}))+';</script></html>'
    return detail().replace(b'</html>', script.encode())


class NewnessGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'newness.sqlite'
        self.p = Pipeline(self.path)
        self.net = patch('socket.socket', side_effect=AssertionError('No network'))
        self.net.start()

    def tearDown(self):
        self.p.db.close()
        self.tmp.cleanup()
        self.net.stop()

    def current(self):
        return self.p.cars()[0]

    def states(self):
        return dict(self.p.db.execute('SELECT uid,status FROM deliveries'))

    def restart(self):
        self.p.db.close()
        self.p = Pipeline(self.path)

    def ingest(self, data=None, at=NOW+10):
        return self.p.apply_detail_snapshot(dated_detail() if data is None else data,
            expected_id='123', expected_url=URL, fetched_at=at, truncated=False)

    def boundary(self, at=NOW):
        self.p.collect(lambda _: dict(items=[], next=None), at,
                       page_budget=1, row_budget=1)

    def seed_queue(self):
        self.p.collect(lambda _: dict(items=[raw('123', url=URL, checked_at=NOW)], next=None),
                       NOW, page_budget=1, row_budget=1)
        self.assertEqual(self.p.enqueue(users(2), comps(), NOW, capacity=10,
                                       olx_enabled=True)['queued'], 2)

    def matching_pending_proofs(self, at=NOW+10):
        # Simulate an independent queue path with a current proof. This ensures
        # the newness gate is tested separately from ordinary fingerprint expiry.
        c = self.current()
        for uid in ('0', '1'):
            self.p.db.execute("INSERT OR REPLACE INTO deliveries VALUES(?,'olx','123','pending')", (uid,))
            self.p.db.execute("INSERT OR REPLACE INTO delivery_proof VALUES(?,'olx','123',?,?)",
                              (uid, fingerprint(c), at+300))
        self.p.db.commit()

    def test_reported_old_creation_today_refresh_holds_two_clients_and_keeps_history(self):
        self.seed_queue()
        result = self.ingest()
        car = self.current()
        self.assertEqual(result['newness_review']['status'], 'reported_preexisting')
        self.assertTrue(car['newness_review']['negative_hold'])
        self.assertFalse(car['newness_review']['publication_verified'])
        self.assertEqual(car['published_at'], NOW)
        self.assertTrue(car['publication_verified'])
        self.assertEqual(len(self.p.cars(True)), 1)
        self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})
        self.restart()
        self.assertEqual(self.current()['newness_review'], car['newness_review'])
        self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})

    def test_enqueue_gate_cannot_be_overridden_by_a_positive_valuation(self):
        self.seed_queue()
        self.ingest()
        with patch('experiments.olx_offline.pipeline.estimate', return_value={
                'status': 'experimental_estimate', 'discount_percent': '50'}), \
             patch('experiments.olx_offline.pipeline.filtered', return_value=True):
            result = self.p.enqueue(users(2), [], NOW+11, capacity=10, olx_enabled=True)
        self.assertEqual(result['queued'], 0)
        self.assertEqual(result['uncertain'], 2)
        self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})

    def test_direct_delivery_gate_holds_matching_proofs_without_enqueue(self):
        self.seed_queue()
        self.ingest()
        self.matching_pending_proofs()
        result = self.p.deliver_fake(lambda *_: self.fail('Preexisting ad sent'),
            lambda _: True, now=NOW+11, olx_enabled=True)
        self.assertEqual(result['held'], 2)
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})

    def test_accepted_history_is_not_reset_for_another_pending_client(self):
        self.seed_queue()
        self.p.deliver_fake(lambda *_: True, lambda uid: uid == '0', now=NOW,
                            olx_enabled=True)
        self.assertEqual(self.states(), {'0': 'accepted', '1': 'pending'})
        self.ingest()
        self.assertEqual(self.states(), {'0': 'accepted', '1': 'needs_revalidation'})
        self.restart()
        self.assertEqual(self.states(), {'0': 'accepted', '1': 'needs_revalidation'})

    def test_recent_source_creation_alone_cannot_create_a_verified_publication(self):
        self.boundary()
        self.ingest(dated_detail(created=NOW+1, refreshed=NOW+2))
        car = self.current()
        self.assertEqual(car['newness_review']['status'], 'publication_unconfirmed')
        self.assertFalse(car['newness_review']['negative_hold'])
        self.assertFalse(car['publication_verified'])
        self.assertIsNone(car['published_at'])
        self.assertEqual(self.p.cars(True), [])
        with patch('experiments.olx_offline.pipeline.estimate', return_value={
                'status': 'experimental_estimate', 'discount_percent': '50'}), \
             patch('experiments.olx_offline.pipeline.filtered', return_value=True):
            self.assertEqual(self.p.enqueue(users(2), [], NOW+11, capacity=10,
                                           olx_enabled=True)['queued'], 0)

    def test_recent_unknown_source_semantics_keeps_independent_prior_publication(self):
        self.seed_queue()
        self.ingest(dated_detail(created=NOW+1, refreshed=NOW+2))
        car = self.current()
        self.assertFalse(car['newness_review']['negative_hold'])
        self.assertFalse(car['newness_review']['publication_verified'])
        self.assertTrue(car['publication_verified'])
        self.assertEqual(car['published_at'], NOW)
        self.assertEqual(self.states(), {'0': 'pending', '1': 'pending'})

    def test_missing_state_or_date_is_unknown_not_reported_preexisting(self):
        self.boundary()
        for data in (detail(), dated_detail(createdTime=None)):
            self.ingest(data)
            review = self.current()['newness_review']
            self.assertEqual(review['status'], 'publication_unconfirmed')
            self.assertFalse(review['negative_hold'])
            self.assertFalse(self.current()['publication_verified'])

    def test_wrong_identity_and_inconsistent_reported_dates_are_negative_holds(self):
        self.seed_queue()
        for data in (dated_detail(source_id=456),
                     dated_detail(created=NOW-100, refreshed=NOW-200),
                     dated_detail(createdTime='invalid')):
            self.ingest(data)
            self.assertTrue(self.current()['newness_review']['negative_hold'])
            self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})
            self.assertTrue(self.current()['publication_verified'])

    def test_boundary_absence_defers_gate_until_checkpoint_exists_at_enqueue(self):
        self.ingest()
        self.assertNotIn('newness_review', self.current())
        self.assertIsNone(self.p.db.execute('SELECT boundary FROM checkpoint').fetchone()[0])
        observations = self.current()['source_date_observations']
        self.restart()
        self.boundary(at=NOW+20)
        self.p.enqueue(users(2), [], NOW+21, capacity=10, olx_enabled=True)
        car = self.current()
        self.assertEqual(car['source_date_observations'], observations)
        self.assertTrue(car['newness_review']['negative_hold'])
        self.assertEqual(car['newness_review']['boundary'], NOW+20)
        self.assertFalse(car['publication_verified'])
        self.assertEqual(self.p.cars(True), [])

    def test_boundary_absence_defers_gate_until_direct_delivery_before_enqueue(self):
        self.ingest()
        self.matching_pending_proofs(at=NOW+20)
        self.boundary(at=NOW+20)
        result = self.p.deliver_fake(lambda *_: self.fail('Deferred old ad sent'),
            lambda _: True, now=NOW+21, olx_enabled=True)
        self.assertEqual(result['held'], 2)
        self.assertTrue(self.current()['newness_review']['negative_hold'])
        self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})

    def test_preexisting_source_gate_does_not_disqualify_market_comparables(self):
        target = canonical(raw('target'), NOW)
        old = comps()
        for comparison in old:
            comparison['newness_review'] = dict(status='reported_preexisting',
                negative_hold=True, publication_verified=False,
                reasons=['source_created_before_boundary'])
        result = estimate(target, old, NOW)
        self.assertEqual(result['status'], 'experimental_estimate')
        self.assertEqual(result['sample'], 12)
        target['newness_review'] = old[0]['newness_review']
        self.assertEqual(estimate(target, old, NOW)['status'], 'experimental_estimate')

    def test_generic_fixtures_without_source_date_state_still_deliver(self):
        self.seed_queue()
        self.assertNotIn('newness_review', self.current())
        self.assertEqual(self.p.deliver_fake(lambda *_: True, lambda _: True, now=NOW,
                                           olx_enabled=True)['accepted'], 2)

    def test_unknown_or_recent_detail_cannot_clear_prior_negative_hold_or_bypass_queues(self):
        self.seed_queue()
        self.ingest()
        first_evidence = self.current()['newness_negative_evidence']
        first_created = first_evidence['source_date_observations']['values']['createdTime']['epoch']
        self.assertEqual(first_created, NOW-1000)
        for data in (detail(), dated_detail(createdTime=None),
                     dated_detail(created=NOW+14, refreshed=NOW+15)):
            self.ingest(data, at=NOW+20)
            car = self.current()
            self.assertTrue(car['newness_review']['negative_hold'])
            self.assertIn('previous_negative_evidence_unresolved', car['newness_review']['reasons'])
            self.assertEqual(car['newness_negative_evidence'], first_evidence)
            self.assertTrue(car['publication_verified'])
            self.assertEqual(car['published_at'], NOW)
            with patch('experiments.olx_offline.pipeline.estimate', return_value={
                    'status': 'experimental_estimate', 'discount_percent': '50'}), \
                 patch('experiments.olx_offline.pipeline.filtered', return_value=True):
                result = self.p.enqueue(users(2), [], NOW+21, capacity=10, olx_enabled=True)
            self.assertEqual(result['queued'], 0)
            self.assertEqual(result['uncertain'], 2)
            self.matching_pending_proofs(at=NOW+20)
            result = self.p.deliver_fake(lambda *_: self.fail('Unresolved old ad sent'),
                lambda _: True, now=NOW+21, olx_enabled=True)
            self.assertEqual(result['held'], 2)
            self.assertEqual(self.states(), {'0': 'needs_revalidation', '1': 'needs_revalidation'})
            self.restart()
            self.assertEqual(self.current()['newness_negative_evidence'], first_evidence)


if __name__ == '__main__':
    unittest.main()
