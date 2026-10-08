import unittest
from experiments.free_search.scoped_control_audit import scoped_controls,explicit_scope

class ScopedControlTests(unittest.TestCase):
    def audit(self,**updates):
        kw=dict(control_query='category=1&page=0&limit=100',collector_queries=['category=1&page=0&limit=20'],frozen_at=10,collector_started_at=11,independent_selection=True,control_source='legacy',collector_source='search',expected_pages=[0],successful_pages=[0]);kw.update(updates)
        return scoped_controls(['1','2'],[{'listing_id':'1','method':'collector_scan','observed_at':12},{'listing_id':'2','method':'known_id_lookup','observed_at':13}],**kw)

    def test_pagination_only_differences_allowed(self):
        r=self.audit();self.assertEqual(r['bounded_control_detection_fraction'],.5)
        self.assertIsNone(r['whole_market_recall']);self.assertEqual(r['not_observed_control_ids'],['2'])

    def test_top_scope_mismatch_never_recall(self):
        r=self.audit(control_query='lang_id=4&page=0&countpage=100&top=1')
        self.assertFalse(r['scope_equivalent']);self.assertIsNone(r['bounded_control_detection_fraction'])
        self.assertEqual(r['observed_controls'],1)

    def test_missing_scope_is_not_wildcard(self):
        self.assertIsNone(self.audit(control_query=None)['bounded_control_detection_fraction'])

    def test_self_validation_and_posthoc_freeze_rejected(self):
        for kw in [dict(independent_selection=False),dict(control_source='search'),dict(frozen_at=12)]:
            self.assertIsNone(self.audit(**kw)['bounded_control_detection_fraction'])

    def test_missing_page_stays_incomplete(self):
        r=self.audit(expected_pages=[0,1]);self.assertIn('collector_cycle_incomplete',r['comparison_blockers'])
        self.assertIsNone(r['bounded_control_detection_fraction'])

    def test_duplicate_parameters_not_silently_overwritten(self):
        with self.assertRaises(ValueError):explicit_scope('category=1&category=2')

    def test_future_relabelled_observation_before_scan_fails(self):
        with self.assertRaises(ValueError):self.audit(collector_started_at=15)

    def test_pagination_only_or_blank_scope_is_not_proof(self):
        for query in ['page=0&limit=20','category=&page=0']:
            with self.assertRaises(ValueError):explicit_scope(query)
