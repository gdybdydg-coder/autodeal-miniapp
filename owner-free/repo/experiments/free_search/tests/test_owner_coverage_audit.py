from unittest import TestCase
from experiments.free_search.owner_coverage_audit import audit


class CoverageAuditTests(TestCase):
    def test_missing_baseline_and_prior_pilots_not_fabricated_as_paid_pairs(self):
        paid=[{'id':str(i),'message_id':m,'first_seen':100} for i,m in
              ((1,28006),(2,30000),(3,30001),(4,30002))]
        public=[{'id':'3','baseline':True,'first_seen':80},
                {'id':'4','baseline':False,'first_seen':105}]
        result=audit(paid,public)
        self.assertEqual((result['paid_receipts'],result['public_overlap'],result['not_observed']),(3,2,1))
        self.assertEqual([r['free_minus_paid_first_observed_seconds'] for r in result['rows']],[None,None,5])
        self.assertIsNone(result['whole_market_recall'])
        self.assertFalse(result['simultaneous_coverage_proven'])
        self.assertFalse(result['send_authorized'])

    def test_duplicate_identity_is_not_counted_twice(self):
        paid=[{'id':'1','message_id':1,'first_seen':10}]*2
        with self.assertRaises(ValueError):audit(paid,[])
