import unittest
from experiments.free_search.control_audit import audit_controls

class ControlAuditTests(unittest.TestCase):
    def test_lookup_does_not_repair_discovery(self):
        result=audit_controls(['1','2'],[dict(listing_id='1',method='collector_scan',observed_at=10),dict(listing_id='2',method='known_id_lookup',observed_at=12)],[],[])
        self.assertEqual(result['discovered_count'],1)
        self.assertEqual(result['missing_ids'],['2'])
        self.assertEqual(result['valuation_not_attempted_count'],2)
        self.assertIsNone(result['publication_to_ready_seconds'])

    def test_failure_kept_in_denominator(self):
        result=audit_controls(['1','2'],[],[dict(listing_id='1',status='error',reason='404'),dict(listing_id='2',status='prepared')],[dict(listing_id='2',status='unknown',reason='too_few_comparable_peers')])
        self.assertEqual(result['control_count'],2)
        self.assertEqual(result['detail_ready_count'],1)
        self.assertEqual(result['unknown_count'],1)
        self.assertEqual(result['valuation_not_attempted_count'],1)

    def test_unplanned_and_duplicate_rejected(self):
        with self.assertRaises(ValueError):audit_controls(['1'],[],[dict(listing_id='2')],[])
        with self.assertRaises(ValueError):audit_controls(['1','1'],[],[],[])

    def test_future_publication_not_inferred(self):
        result=audit_controls(['1'],[dict(listing_id='1',method='collector_scan',observed_at=10)],[],[])
        self.assertIsNone(result['rows'][0]['publication_at'])
        with self.assertRaises(ValueError):audit_controls(['1'],[dict(listing_id='1',method='collector_scan',observed_at=float('nan'))],[],[])

    def test_invalid_status_cannot_hide_failure(self):
        with self.assertRaises(ValueError):audit_controls(['1'],[],[dict(listing_id='1',status='maybe')],[])
        with self.assertRaises(ValueError):audit_controls(['1'],[],[],[dict(listing_id='1',status='maybe')])

    def test_replay_detects_changed_denominator(self):
        from experiments.free_search.replay_control_audit import replay
        manifest={'candidates':[{'id':'1'},{'id':'2'}]}
        data={'discovery_observations':[],'details':[],'valuations':[],
              'audit':audit_controls(['1','2'],[],[],[])}
        self.assertEqual(replay(data,manifest)['control_count'],2)
        data['audit']['control_count']=1
        with self.assertRaises(ValueError):replay(data,manifest)
