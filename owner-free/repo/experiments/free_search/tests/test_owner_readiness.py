from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from experiments.free_search.owner_readiness import assess_asking_research as assess, audit
from experiments.free_search.owner_pilot import _page
import test_owner_pilot as fixtures


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = fixtures.NOW
        self.subject = _page(fixtures.page(), self.now, 300)
        self.peers = [_page(fixtures.page(str(20000000+i), 10000+i*100), self.now, 300)
                      for i in range(6)]
        self.scan = dict(phase='complete', baseline=0, last_at=self.now)

    def check(self, **changes):
        args = dict(subject=self.subject, peers=self.peers, scan=self.scan, now=self.now)
        args.update(changes)
        return assess(**args)

    def test_good_fixture_still_cannot_authorize_live_delivery(self):
        report = self.check()
        self.assertEqual(report['valuation']['status'], 'estimated')
        self.assertFalse(report['authorization'])
        self.assertFalse(report['ready_for_delivery'])
        self.assertIsNone(report['publication_at'])

    def test_stale_subject_recomputed_not_saved_ready_claim(self):
        result = self.check(now=self.now+301)
        self.assertIn('subject_stale_price', result['blockers'])
        self.assertEqual(result['valuation']['status'], 'unknown')

    def test_duplicate_peers_cannot_inflate_sample(self):
        result = self.check(peers=[self.peers[0]]*6)
        self.assertIn('peer_identity_invalid_or_duplicate', result['blockers'])
        self.assertEqual(result['valuation']['status'], 'unknown')

    def test_future_or_stale_peer_prices_cannot_count(self):
        for timestamp in (self.now+1, self.now-86401):
            peers = deepcopy(self.peers)
            for peer in peers:
                peer['observed_at'] = timestamp
            self.assertEqual(self.check(peers=peers)['valuation']['status'], 'unknown')

    def test_generation_and_drive_cannot_be_ignored(self):
        for field in ('generation', 'drive'):
            peers = deepcopy(self.peers)
            for peer in peers:
                peer[field] = None
            report = self.check(peers=peers)
            self.assertEqual(len(report['rejected']), 6)
            self.assertEqual(report['valuation']['status'], 'unknown')

    def test_baseline_blocked_future_and_stale_scan_rejected(self):
        for changes in (dict(baseline=1), dict(phase='blocked'),
                        dict(last_at=self.now+1), dict(last_at=self.now-301)):
            report = self.check(scan={**self.scan, **changes})
            self.assertIn('current_complete_nonbaseline_scan_missing', report['blockers'])

    def test_missing_evidence_is_unknown_and_creates_no_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            report = audit(directory, self.now)
            self.assertFalse(report['ready_for_delivery'])
            self.assertIn('evidence_missing_or_invalid', report['blockers'])
            self.assertFalse(report['real_deal_delivery_verified'])
            self.assertEqual(list(Path(directory).iterdir()), [])
