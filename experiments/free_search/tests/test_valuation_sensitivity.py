import unittest
from experiments.free_search.valuation_sensitivity import audit_sensitivity

class SensitivityTests(unittest.TestCase):
    def rows(self, n=6):
        subject=dict(listing_id='100',brand='A',model='B',year=2018,price=10000,currency='USD',observed_at=1000,region='R')
        peers=[dict(subject,listing_id=str(i+1),price=12000+i*1000,region='R' if i<3 else 'S') for i in range(n)]
        return subject,peers

    def test_minimum_failure_is_reported(self):
        subject,peers=self.rows(5)
        out=audit_sensitivity(subject,peers,1000)
        self.assertEqual(out['baseline']['status'],'estimated')
        self.assertEqual(out['listing_summary']['unknown'],5)
        self.assertIsNone(out['listing_summary']['reference_min'])
        self.assertFalse(out['ready_for_delivery'])

    def test_regions_removed_and_inputs_unchanged(self):
        subject,peers=self.rows()
        out=audit_sensitivity(subject,peers,1000)
        self.assertEqual(out['region_summary']['unknown'],2)
        self.assertEqual([r['removed_count'] for r in out['leave_one_region_out']],[3,3])
        self.assertEqual(len(peers),6)
        self.assertEqual(out['listing_summary']['scenarios'],6)

    def test_duplicate_ids_rejected(self):
        subject,peers=self.rows()
        with self.assertRaises(ValueError):audit_sensitivity(subject,peers+[peers[0]],1000)

    def test_nonfinite_threshold_rejected(self):
        subject,peers=self.rows()
        with self.assertRaises(ValueError):audit_sensitivity(subject,peers,1000,[float('nan')])
