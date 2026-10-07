from unittest import TestCase
from experiments.free_search.public_peer_pilot import run_frozen_pilot
from experiments.free_search.tests.test_valuation import car,peers,NOW

class PeerPilotTests(TestCase):
    def test_missing_fetches_remain_in_denominator(self):
        r=run_frozen_pilot(car(),peers()[:2],['1','2','3','4','5'],NOW)
        self.assertEqual(r['planned_peers'],5)
        self.assertEqual(r['missing_peer_ids'],['3','4','5'])
        self.assertEqual(r['outcome']['status'],'unknown')
    def test_subject_cannot_enter_frozen_peer_set(self):
        with self.assertRaises(ValueError):run_frozen_pilot(car(),[],['500'],NOW)
    def test_unplanned_and_duplicate_peers_fail(self):
        for values in ([car('99')],[car('1'),car('1')]):
            with self.subTest(values=values),self.assertRaises(ValueError):run_frozen_pilot(car(),values,['1','2'],NOW)
    def test_estimate_never_authorizes_delivery(self):
        values=peers();r=run_frozen_pilot(car(),values,[p['listing_id'] for p in values],NOW)
        self.assertEqual(r['outcome']['status'],'estimated')
        self.assertFalse(r['ready_for_delivery']);self.assertFalse(r['independent_quality_validation'])
